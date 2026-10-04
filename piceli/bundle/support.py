"""``piceli support-bundle``: a read-only, redacted snapshot of one namespace.

A client who installed a bundle sends this file instead of giving anyone
access to their cluster. :class:`Collector` reads, through an explicit
kubeconfig and context, only with ``GET`` (any other method is refused before
it is sent, and the manifest records every method used):

- ``versions.json``: the API server's version, each node's kubelet,
  container runtime, OS and architecture (when the caller may list nodes),
  and Piceli's version;
- ``pods.json``: each pod's phase, conditions, container states, restarts,
  images and image IDs, requests and limits, probes, and the *names* of its
  environment variables (never a value);
- ``workloads.json``: Deployments, StatefulSets, DaemonSets and Jobs with
  their replica counts, conditions and the bundle they came from
  (``piceli.io/bundle``);
- ``health.json``: what the app declares as its health (each workload's
  readiness probe) and whether it holds (ready replicas);
- ``events.json``: the namespace's events;
- ``services.json`` and ``configmaps.json``: Service ports; ConfigMap names
  and *keys* (never values);
- ``logs/<pod>.<container>.log``: the last lines of every container (and of
  its previous run after a restart), each line redacted
  (:func:`piceli.services.logs.redact_line`: passwords, tokens, bearer
  credentials, URL credentials, JWTs and private keys become ``[REDACTED]``).

Secrets are never requested at all. Event and condition messages are
redacted like log lines. ``manifest.json`` lists every file with its sha256,
what was redacted and every request method. Importing this module runs
nothing.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import tarfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

SCHEMA = "piceli.support-bundle.v1"
#: The only HTTP methods the collector sends.
READ_METHODS = frozenset({"GET"})
_WORKLOADS = (
    ("apps/v1", "deployments"),
    ("apps/v1", "statefulsets"),
    ("apps/v1", "daemonsets"),
    ("batch/v1", "jobs"),
)


class SupportError(ValueError):
    """The support bundle cannot be collected. ``code`` is registered."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class Forbidden(Exception):
    """The caller may not read this (recorded, not fatal)."""


#: ``(method, path, query) -> (status, body bytes)``; the transport seam.
Transport = Callable[[str, str, Mapping[str, str]], tuple[int, bytes]]


def api_transport(client: Any, timeout: float = 30.0) -> Transport:
    """A transport over a ``kubernetes`` ``ApiClient`` (explicit kubeconfig)."""

    def send(method: str, path: str, query: Mapping[str, str]) -> tuple[int, bytes]:
        from kubernetes.client.exceptions import ApiException

        try:
            response = client.call_api(
                path,
                method,
                query_params=list(query.items()),
                header_params={"Accept": "application/json, text/plain"},
                auth_settings=["BearerToken"],
                _preload_content=False,
                _request_timeout=timeout,
            )
        except ApiException as error:
            return int(error.status or 0), b""
        except OSError:
            raise SupportError(
                "support-bundle-unreachable", "the API server did not answer"
            ) from None
        raw = response[0] if isinstance(response, tuple) else response
        return int(getattr(raw, "status", 200)), bytes(raw.data or b"")

    return send


def _redact(text: Any) -> Any:
    from piceli.services.logs import redact_line

    if not isinstance(text, str):
        return text
    return "\n".join(redact_line(line) for line in text.splitlines())


@dataclass
class Collector:
    """Reads one namespace with ``GET`` only (see the module docstring)."""

    transport: Transport
    namespace: str
    log_lines: int = 200
    requests: list[tuple[str, str]] = field(default_factory=list)
    problems: list[dict[str, str]] = field(default_factory=list)

    def _send(
        self, method: str, path: str, query: Mapping[str, str]
    ) -> tuple[int, bytes]:
        if method not in READ_METHODS:
            raise SupportError(
                "support-bundle-write-refused",
                f"the support bundle only reads; {method} was refused",
            )
        self.requests.append((method, path))
        return self.transport(method, path, query)

    def get(self, path: str, query: Mapping[str, str] | None = None) -> Any:
        status, body = self._send("GET", path, query or {})
        if status in {401, 403}:
            raise Forbidden(path)
        if status == 404:
            return None
        if status >= 400 or status == 0:
            raise SupportError(
                "support-bundle-unreachable", f"the API server answered {status}"
            )
        return json.loads(body or b"null")

    def text(self, path: str, query: Mapping[str, str]) -> str | None:
        status, body = self._send("GET", path, query)
        if status in {400, 401, 403, 404}:
            return None
        if status >= 400 or status == 0:
            raise SupportError(
                "support-bundle-unreachable", f"the API server answered {status}"
            )
        return body.decode("utf-8", "replace")

    def _optional(self, what: str, path: str) -> Any:
        try:
            return self.get(path)
        except Forbidden:
            self.problems.append({"what": what, "problem": "forbidden"})
            return None

    # ------------------------------------------------------------ collect

    def collect(self) -> dict[str, Any]:
        """``{file name: JSON value or text}`` of the support bundle."""
        ns = quote(self.namespace, safe="")
        base = f"/api/v1/namespaces/{ns}"
        if self._optional("namespace", f"/api/v1/namespaces/{ns}") is None:
            raise SupportError(
                "support-bundle-namespace-missing",
                f"namespace {self.namespace} not found (or not readable)",
            )
        files: dict[str, Any] = {}
        files["versions.json"] = self._versions()
        pods = (self._optional("pods", f"{base}/pods") or {}).get("items") or []
        files["pods.json"] = [_pod(item) for item in pods]
        workloads = []
        for group, plural in _WORKLOADS:
            found = self._optional(plural, f"/apis/{group}/namespaces/{ns}/{plural}")
            workloads += [_workload(item) for item in (found or {}).get("items") or ()]
        files["workloads.json"] = workloads
        files["health.json"] = [_health(item) for item in workloads]
        events = (self._optional("events", f"{base}/events") or {}).get("items") or []
        files["events.json"] = [_event(item) for item in events]
        services = (self._optional("services", f"{base}/services") or {}).get(
            "items"
        ) or []
        files["services.json"] = [_service(item) for item in services]
        configs = (self._optional("configmaps", f"{base}/configmaps") or {}).get(
            "items"
        ) or []
        files["configmaps.json"] = [
            {
                "name": (item.get("metadata") or {}).get("name"),
                "keys": sorted(
                    {*(item.get("data") or {}), *(item.get("binaryData") or {})}
                ),
            }
            for item in configs
        ]
        for pod in pods:
            files.update(self._logs(pod))
        return files

    def _versions(self) -> dict[str, Any]:
        import importlib.metadata

        server = self._optional("version", "/version") or {}
        nodes = self._optional("nodes", "/api/v1/nodes")
        try:
            piceli = importlib.metadata.version("piceli")
        except importlib.metadata.PackageNotFoundError:  # pragma: no cover
            piceli = "unknown"
        return {
            "piceli": piceli,
            "server": {
                key: server.get(key)
                for key in ("gitVersion", "platform", "goVersion")
                if key in server
            },
            "nodes": None
            if nodes is None
            else [
                {
                    "name": (item.get("metadata") or {}).get("name"),
                    **{
                        key: (item.get("status") or {}).get("nodeInfo", {}).get(key)
                        for key in (
                            "kubeletVersion",
                            "containerRuntimeVersion",
                            "osImage",
                            "architecture",
                            "kernelVersion",
                        )
                    },
                }
                for item in nodes.get("items") or ()
            ],
        }

    def _logs(self, pod: Mapping[str, Any]) -> dict[str, str]:
        metadata = pod.get("metadata") or {}
        name = str(metadata.get("name"))
        status = pod.get("status") or {}
        statuses = [
            *(status.get("initContainerStatuses") or ()),
            *(status.get("containerStatuses") or ()),
        ]
        found: dict[str, str] = {}
        path = f"/api/v1/namespaces/{quote(self.namespace, safe='')}/pods/{quote(name, safe='')}/log"
        for item in statuses:
            container = str(item.get("name"))
            runs: list[tuple[str, dict[str, str]]] = [("", {})]
            if int(item.get("restartCount") or 0) > 0:
                runs.append((".previous", {"previous": "true"}))
            for suffix, extra in runs:
                text = self.text(
                    path,
                    {"container": container, "tailLines": str(self.log_lines), **extra},
                )
                if text is not None:
                    found[f"logs/{name}.{container}{suffix}.log"] = _redact(text) + "\n"
        return found


# ------------------------------------------------------------ summaries


def _conditions(value: Any) -> list[dict[str, Any]]:
    return [
        {
            "type": item.get("type"),
            "status": item.get("status"),
            "reason": item.get("reason"),
            "message": _redact(item.get("message")),
        }
        for item in value or ()
        if isinstance(item, Mapping)
    ]


def _probe(probe: Any) -> dict[str, Any] | None:
    if not isinstance(probe, Mapping):
        return None
    if "httpGet" in probe:
        get = probe["httpGet"] or {}
        return {"type": "http", "path": get.get("path"), "port": get.get("port")}
    if "tcpSocket" in probe:
        return {"type": "tcp", "port": (probe["tcpSocket"] or {}).get("port")}
    if "grpc" in probe:
        return {"type": "grpc", "port": (probe["grpc"] or {}).get("port")}
    if "exec" in probe:
        return {"type": "exec"}  # the command is not collected
    return {"type": "unknown"}


def _container(spec: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "name": spec.get("name"),
        "image": spec.get("image"),
        "env_names": sorted(
            str(item.get("name"))
            for item in spec.get("env") or ()
            if isinstance(item, Mapping)
        ),
        "env_from": len(spec.get("envFrom") or ()),
        "resources": spec.get("resources") or {},
        "ports": [
            {"port": item.get("containerPort"), "protocol": item.get("protocol")}
            for item in spec.get("ports") or ()
            if isinstance(item, Mapping)
        ],
        "ready_probe": _probe(spec.get("readinessProbe")),
        "live_probe": _probe(spec.get("livenessProbe")),
    }


def _state(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    for key in ("running", "waiting", "terminated"):
        if key in value:
            detail = value[key] or {}
            return {
                "state": key,
                "reason": detail.get("reason"),
                "exit_code": detail.get("exitCode"),
                "message": _redact(detail.get("message")),
            }
    return {}


def _pod(pod: Mapping[str, Any]) -> dict[str, Any]:
    metadata = pod.get("metadata") or {}
    spec = pod.get("spec") or {}
    status = pod.get("status") or {}
    return {
        "name": metadata.get("name"),
        "labels": metadata.get("labels") or {},
        "owners": [
            f"{item.get('kind')}/{item.get('name')}"
            for item in metadata.get("ownerReferences") or ()
        ],
        "node": spec.get("nodeName"),
        "phase": status.get("phase"),
        "reason": status.get("reason"),
        "conditions": _conditions(status.get("conditions")),
        "init_containers": [
            _container(item) for item in spec.get("initContainers") or ()
        ],
        "containers": [_container(item) for item in spec.get("containers") or ()],
        "statuses": [
            {
                "name": item.get("name"),
                "ready": item.get("ready"),
                "restarts": item.get("restartCount"),
                "image": item.get("image"),
                "image_id": item.get("imageID"),
                **_state(item.get("state")),
                "last": _state(item.get("lastState")),
            }
            for item in [
                *(status.get("initContainerStatuses") or ()),
                *(status.get("containerStatuses") or ()),
            ]
        ],
    }


def _workload(item: Mapping[str, Any]) -> dict[str, Any]:
    metadata = item.get("metadata") or {}
    spec = item.get("spec") or {}
    status = item.get("status") or {}
    template = (spec.get("template") or {}).get("spec") or {}
    return {
        "kind": item.get("kind"),
        "name": metadata.get("name"),
        "bundle": (metadata.get("annotations") or {}).get("piceli.io/bundle"),
        "labels": metadata.get("labels") or {},
        "replicas": spec.get("replicas"),
        "ready": status.get("readyReplicas", status.get("numberReady")),
        "available": status.get("availableReplicas", status.get("numberAvailable")),
        "updated": status.get("updatedReplicas", status.get("updatedNumberScheduled")),
        "succeeded": status.get("succeeded"),
        "failed": status.get("failed"),
        "conditions": _conditions(status.get("conditions")),
        "ready_probes": {
            str(container.get("name")): _probe(container.get("readinessProbe"))
            for container in template.get("containers") or ()
        },
    }


def _health(workload: Mapping[str, Any]) -> dict[str, Any]:
    declared = {key: value for key, value in workload["ready_probes"].items() if value}
    wanted = workload.get("replicas")
    ready = workload.get("ready") or 0
    if workload.get("kind") == "Job":
        healthy = bool(workload.get("succeeded")) and not workload.get("failed")
    else:
        healthy = wanted is not None and ready >= wanted
    return {
        "workload": f"{workload.get('kind')}/{workload.get('name')}",
        "declared": declared or None,
        "ready": ready,
        "wanted": wanted,
        "healthy": healthy,
    }


def _event(item: Mapping[str, Any]) -> dict[str, Any]:
    involved = item.get("involvedObject") or item.get("regarding") or {}
    return {
        "type": item.get("type"),
        "reason": item.get("reason"),
        "object": f"{involved.get('kind')}/{involved.get('name')}",
        "count": item.get("count"),
        "first": item.get("firstTimestamp"),
        "last": item.get("lastTimestamp") or item.get("eventTime"),
        "message": _redact(item.get("message")),
    }


def _service(item: Mapping[str, Any]) -> dict[str, Any]:
    spec = item.get("spec") or {}
    return {
        "name": (item.get("metadata") or {}).get("name"),
        "type": spec.get("type"),
        "ports": [
            {
                "port": port.get("port"),
                "target": port.get("targetPort"),
                "protocol": port.get("protocol"),
            }
            for port in spec.get("ports") or ()
            if isinstance(port, Mapping)
        ],
    }


# ---------------------------------------------------------------- archive


def archive(
    files: Mapping[str, Any],
    *,
    namespace: str,
    context: str,
    requests: list[tuple[str, str]],
    problems: list[dict[str, str]],
    created: float | None = None,
) -> bytes:
    """The ``.tar.gz`` bytes: every file plus ``manifest.json`` (listed last)."""
    bodies: dict[str, bytes] = {}
    for name, value in sorted(files.items()):
        bodies[name] = (
            value.encode()
            if isinstance(value, str)
            else (
                json.dumps(value, indent=2, sort_keys=True, default=str) + "\n"
            ).encode()
        )
    stamp = time.time() if created is None else created
    manifest = {
        "schema": SCHEMA,
        "namespace": namespace,
        "context": context,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stamp)),
        "files": {
            name: {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}
            for name, body in bodies.items()
        },
        "requests": {
            "count": len(requests),
            "methods": sorted({method for method, _ in requests}),
        },
        "problems": problems,
        "redaction": {
            "secrets": "never requested",
            "env": "names only, never values",
            "configmaps": "keys only, never values",
            "logs_events_conditions": "secret-looking values replaced by [REDACTED]",
        },
    }
    bodies["manifest.json"] = (
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    ).encode()
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, body in bodies.items():
            info = tarfile.TarInfo(f"support-bundle/{name}")
            info.size, info.mode, info.mtime = len(body), 0o644, int(stamp)
            tar.addfile(info, io.BytesIO(body))
    return gzip.compress(raw.getvalue(), mtime=int(stamp))


def write(path: Path, data: bytes) -> None:
    """Write ``data`` to a new private file (mode 0600); never overwrite."""
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SupportError(
            "support-bundle-out-refused", f"{path} exists; name a new file"
        ) from None
    except OSError as error:
        raise SupportError(
            "support-bundle-out-refused", f"cannot write {path}: {type(error).__name__}"
        ) from None
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
