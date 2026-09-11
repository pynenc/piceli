"""Fault-injecting loopback Kubernetes API used by the actual SDK transport."""

from __future__ import annotations

import copy
import json
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator
from urllib.parse import parse_qs, urlsplit

from kubernetes.client import ApiClient, Configuration

from piceli.k8s.ops.discovery import EvidenceSource, PlanTarget
from piceli.k8s.ops.kubernetes_provider import KubernetesProvider

TARGET = PlanTarget("acceptance-cluster", "piceli-test")
TYPES = {
    "configmaps": ("v1", "ConfigMap", True),
    "secrets": ("v1", "Secret", True),
    "persistentvolumeclaims": ("v1", "PersistentVolumeClaim", True),
    "persistentvolumes": ("v1", "PersistentVolume", False),
    "namespaces": ("v1", "Namespace", False),
    "deployments": ("apps/v1", "Deployment", True),
    "widgets": ("example.test/v1", "Widget", False),
}


def manifest(
    kind: str = "ConfigMap", name: str = "settings", *, value: str = "one"
) -> dict[str, Any]:
    api, _, namespaced = next(entry for entry in TYPES.values() if entry[1] == kind)
    result: dict[str, Any] = {
        "apiVersion": api,
        "kind": kind,
        "metadata": {"name": name},
    }
    if namespaced:
        result["metadata"]["namespace"] = TARGET.namespace
    if kind == "ConfigMap":
        result["data"] = {"mode": value}
    elif kind == "Secret":
        result["data"] = {"password": value}
    elif kind == "Deployment":
        result["spec"] = {
            "replicas": 1,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": {"app": name}},
                "spec": {
                    "containers": [
                        {"name": "worker", "image": "example.invalid/worker:test"}
                    ]
                },
            },
        }
    return result


class FakeAPI:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict[str, Any]] = {}
        self.requests: list[dict[str, Any]] = []
        self.faults: list[dict[str, Any]] = []
        self.lock = threading.RLock()
        self.ready = True
        self.wait_for_first_consumer: set[str] = set()
        self.version = 1
        self.put(manifest("Namespace", "kube-system"), uid="cluster-uid")
        self.put(manifest("Namespace", TARGET.namespace), uid="namespace-uid")

    def put(
        self, value: dict[str, Any], *, uid: str | None = None, owned: bool = False
    ) -> dict[str, Any]:
        with self.lock:
            value = copy.deepcopy(value)
            self.version += 1
            metadata = value["metadata"]
            metadata.update(
                {
                    "uid": uid or uuid.uuid4().hex,
                    "resourceVersion": str(self.version),
                    "generation": 1,
                }
            )
            if owned:
                metadata.setdefault("annotations", {})[
                    "piceli.io/owner"
                ] = "acceptance-owner"
            metadata["managedFields"] = [
                {
                    "manager": "piceli-acceptance" if owned else "other",
                    "operation": "Apply",
                    "apiVersion": value["apiVersion"],
                    "fieldsType": "FieldsV1",
                    "fieldsV1": {"f:spec": {}},
                }
            ]
            self.objects[(value["kind"], metadata["name"])] = value
            self._readiness(value)
            return copy.deepcopy(value)

    def _readiness(self, value: dict[str, Any]) -> None:
        if value["kind"] == "Deployment":
            count = value.get("spec", {}).get("replicas", 1)
            value["status"] = {
                "observedGeneration": value["metadata"]["generation"],
                "readyReplicas": count if self.ready else 0,
                "updatedReplicas": count,
                "replicas": count,
            }
        elif value["kind"] == "Namespace":
            value["status"] = {"phase": "Active"}
        elif value["kind"] == "PersistentVolumeClaim":
            name = value["metadata"]["name"]
            consumed = any(
                name
                in {
                    volume.get("persistentVolumeClaim", {}).get("claimName")
                    for volume in deployment.get("spec", {})
                    .get("template", {})
                    .get("spec", {})
                    .get("volumes", [])
                    if isinstance(volume, dict)
                }
                for (kind, _), deployment in self.objects.items()
                if kind in {"Deployment", "StatefulSet", "DaemonSet", "Job", "Pod"}
            )
            value["status"] = {
                "phase": "Bound"
                if name not in self.wait_for_first_consumer or consumed
                else "Pending"
            }
        elif value["kind"] == "PersistentVolume":
            value["status"] = {"phase": "Bound"}

    def inject(self, method: str, path_suffix: str, **fault: Any) -> None:
        self.faults.append({"method": method, "suffix": path_suffix, **fault})

    def handler(self) -> type[BaseHTTPRequestHandler]:
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: Any) -> None:
                pass

            def do_GET(self) -> None:
                self.handle_request()

            def do_POST(self) -> None:
                self.handle_request()

            def do_PATCH(self) -> None:
                self.handle_request()

            def do_DELETE(self) -> None:
                self.handle_request()

            def handle_request(self) -> None:
                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query)
                body = json.loads(
                    self.rfile.read(int(self.headers.get("Content-Length", "0")))
                    or b"null"
                )
                request = {
                    "method": self.command,
                    "path": parsed.path,
                    "query": query,
                    "body": body,
                    "content_type": self.headers.get("Content-Type"),
                }
                with api.lock:
                    api.requests.append(request)
                    fault = next(
                        (
                            item
                            for item in api.faults
                            if item["method"] == self.command
                            and parsed.path.endswith(item["suffix"])
                            and (
                                "dry_run" not in item
                                or item["dry_run"] == (query.get("dryRun") == ["All"])
                            )
                        ),
                        {},
                    )
                    if fault:
                        api.faults.remove(fault)
                if fault.get("delay"):
                    time.sleep(fault["delay"])
                if "status" in fault:
                    self.respond(
                        fault["status"], {"message": "server-password-do-not-publish"}
                    )
                    return
                if "raw" in fault:
                    self.respond(200, None, raw=fault["raw"])
                    return
                if fault.get("disconnect_before"):
                    self.connection.close()
                    return
                with api.lock:
                    status, response = api.route(request)
                if fault.get("after_commit_delay"):
                    time.sleep(fault["after_commit_delay"])
                if fault.get("disconnect_after"):
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                self.respond(status, response)

            def respond(
                self, status: int, value: Any, *, raw: bytes | None = None
            ) -> None:
                encoded = raw if raw is not None else json.dumps(value).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        return Handler

    def route(self, request: dict[str, Any]) -> tuple[int, Any]:
        path = request["path"]
        method = request["method"]
        query = request["query"]
        for version in {item[0] for item in TYPES.values()}:
            root = ("/apis/" if "/" in version else "/api/") + version
            if path == root:
                return 200, {
                    "kind": "APIResourceList",
                    "groupVersion": version,
                    "resources": [
                        {
                            "name": plural,
                            "kind": kind,
                            "namespaced": namespaced,
                            "verbs": ["get", "list", "create", "patch", "delete"],
                        }
                        for plural, (api, kind, namespaced) in TYPES.items()
                        if api == version
                    ],
                }
        parts = path.split("/")
        found = [
            (index, TYPES[part]) for index, part in enumerate(parts) if part in TYPES
        ]
        if not found:
            return 404, {}
        index, (api_version, kind, namespaced) = found[-1]
        if namespaced and parts[index - 2 : index] != ["namespaces", TARGET.namespace]:
            return 403, {}
        name = parts[index + 1] if len(parts) > index + 1 else None
        current = self.objects.get((kind, name or ""))
        if method == "GET":
            if name:
                if current is None:
                    return 404, {}
                self._readiness(current)
                return 200, copy.deepcopy(current)
            values = sorted(
                [
                    copy.deepcopy(item)
                    for (item_kind, _), item in self.objects.items()
                    if item_kind == kind
                ],
                key=lambda value: value["metadata"]["name"],
            )
            limit = int(query.get("limit", [100])[0])
            start = int(query.get("continue", [0])[0])
            continuation = str(start + limit) if len(values) > start + limit else ""
            return 200, {
                "apiVersion": api_version,
                "kind": kind + "List",
                "metadata": {
                    "resourceVersion": str(self.version),
                    "continue": continuation,
                },
                "items": values[start : start + limit],
            }
        body = copy.deepcopy(request["body"])
        if method == "DELETE":
            if current is None:
                return 404, {}
            if (
                body.get("preconditions")
                != {
                    "uid": current["metadata"]["uid"],
                    "resourceVersion": current["metadata"]["resourceVersion"],
                }
                or body.get("propagationPolicy") != "Orphan"
            ):
                return 409, {}
            del self.objects[(kind, name)]
            self.version += 1
            return 200, {"kind": "Status", "status": "Success"}
        metadata = body["metadata"]
        name = metadata["name"]
        current = self.objects.get((kind, name))
        if method == "POST" and current is not None:
            return 409, {}
        if method == "PATCH":
            if (
                current is None
                or metadata.get("uid") != current["metadata"]["uid"]
                or metadata.get("resourceVersion")
                != current["metadata"]["resourceVersion"]
            ):
                return 409, {}
            if (
                query.get("force") != ["false"]
                or request["content_type"] != "application/apply-patch+yaml"
            ):
                return 422, {}
        metadata.update(
            {
                "uid": current["metadata"]["uid"] if current else uuid.uuid4().hex,
                "resourceVersion": str(self.version + 1),
                "generation": current["metadata"].get("generation", 0) + 1
                if current
                else 1,
                "managedFields": [
                    {
                        "manager": query["fieldManager"][0],
                        "operation": "Apply",
                        "apiVersion": api_version,
                        "fieldsType": "FieldsV1",
                        "fieldsV1": {"f:spec": {}},
                    }
                ],
            }
        )
        self._readiness(body)
        if query.get("dryRun") != ["All"]:
            self.version += 1
            self.objects[(kind, name)] = body
            for (existing_kind, _), existing in self.objects.items():
                if existing_kind == "PersistentVolumeClaim":
                    self._readiness(existing)
        return 201 if method == "POST" else 200, copy.deepcopy(body)


@contextmanager
def serve() -> Iterator[tuple[FakeAPI, str]]:
    api = FakeAPI()
    server = ThreadingHTTPServer(("127.0.0.1", 0), api.handler())
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield api, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def provider_at(url: str, **kwargs: Any) -> KubernetesProvider:
    configuration = Configuration()
    configuration.host = url
    configuration.proxy = None
    configuration.retries = 0
    client = ApiClient(configuration)
    return KubernetesProvider(
        client,
        target=TARGET,
        field_manager="piceli-acceptance",
        owner_id="acceptance-owner",
        source=EvidenceSource.LOOPBACK,
        cluster_uid="cluster-uid",
        namespace_uid="namespace-uid",
        **kwargs,
    )
