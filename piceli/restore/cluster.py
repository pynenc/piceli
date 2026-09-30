"""The cluster side of restore points: writers, helper Jobs and byte streams.

:class:`RestoreCluster` wraps one Kubernetes API client bound to an explicit
kubeconfig and context (never the ambient one) and one namespace. It stops
and starts writers through the ``scale`` subresource (field manager
``piceli-restore-point``; any ``managedFields`` entry it leaves is removed,
so the release keeps sole ownership of ``spec.replicas``), runs quiesce
hooks, waits until no pod mounts a claim writably, and runs one helper Job
per claim whose pod the operator's machine streams bytes from and to over
``pods/exec``. Commands run in the helper print digests and archives only;
nothing a claim holds is ever printed or logged.

Importing this module is side-effect free.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from piceli.restore.model import FIELD_MANAGER, RestorePointError
from piceli.restore.plan import writer_pods

#: Where the helper pod mounts the claim.
MOUNT = "/piceli/claim"
#: A gzip tarball of the claim on stdout.
BACKUP = ["sh", "-c", f"cd {MOUNT} && tar -czf - ."]
#: The content digest (see :mod:`piceli.restore.store`), 64 hex on stdout.
DIGEST = [
    "sh",
    "-c",
    f"cd {MOUNT} && find . -type f | LC_ALL=C sort | "
    'while IFS= read -r f; do sha256sum "$f"; done | sha256sum | cut -c1-64',
]
LABEL = "piceli.io/restore-point"
_CAPABILITIES = ["CHOWN", "DAC_OVERRIDE", "DAC_READ_SEARCH", "FOWNER", "FSETID"]
_STDIN, _STDOUT, _STDERR, _STATUS = 0, 1, 2, 3
_CHUNK = 256 * 1024


def restore_command(size: int) -> list[str]:
    """Empty the claim, then extract exactly ``size`` bytes of stdin into it."""
    return [
        "sh",
        "-c",
        f"set -e; cd {MOUNT}; "
        "find . -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +; "
        f"head -c {int(size)} | tar -xzf - -C {MOUNT}",
    ]


def helper_job(
    name: str,
    *,
    point: str,
    claim: str,
    image: str,
    read_only: bool,
    seconds: int,
    run_as_user: int,
    template: Mapping[str, Any] | None = None,
    node_affinity: Mapping[str, Any] | None = None,
    label: str = LABEL,
) -> dict[str, Any]:
    """The helper Job: one pod that mounts ``claim`` and sleeps until deleted.

    Placement follows the claim's volume (its node affinity) and the writer's
    ``nodeSelector``, tolerations and pull secrets; ``node_affinity`` (a
    volume's ``nodeAffinity.required``) pins it to the nodes that hold a
    node-local volume. ``activeDeadlineSeconds`` ends it even if Piceli is
    killed; a finished Job is removed after a minute.
    """
    pod = dict((template or {}).get("spec") or {})
    labels = {"app.kubernetes.io/managed-by": "piceli", label: point}
    spec: dict[str, Any] = {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "enableServiceLinks": False,
        "terminationGracePeriodSeconds": 1,
        "securityContext": {"runAsUser": run_as_user},
        "containers": [
            {
                "name": "copy",
                "image": image,
                "imagePullPolicy": "IfNotPresent",
                "command": ["sleep", str(seconds)],
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"], "add": list(_CAPABILITIES)},
                },
                "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}},
                "volumeMounts": [
                    {"name": "claim", "mountPath": MOUNT, "readOnly": read_only}
                ],
            }
        ],
        "volumes": [
            {
                "name": "claim",
                "persistentVolumeClaim": {"claimName": claim, "readOnly": read_only},
            }
        ],
    }
    for key in ("nodeSelector", "tolerations", "imagePullSecrets"):
        if pod.get(key):
            spec[key] = pod[key]
    if node_affinity:
        spec["affinity"] = pin(node_affinity)
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "labels": labels},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": seconds,
            "ttlSecondsAfterFinished": 60,
            "template": {"metadata": {"labels": labels}, "spec": spec},
        },
    }


#: Labels of the scratch claims and Jobs of ``piceli restore --to-new-claim``.
VERIFY_LABEL = "piceli.io/restore-verify"
SCRATCH_LABEL = "piceli.io/scratch"
SOURCE_ANNOTATION = "piceli.io/source-claim"


def pin(node_affinity: Mapping[str, Any]) -> dict[str, Any]:
    """A pod ``affinity`` that requires the nodes a volume's affinity names."""
    return {
        "nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": json.loads(
                json.dumps(dict(node_affinity))
            )
        }
    }


def scratch_claim(
    name: str, *, point: str, source: str, spec: Mapping[str, Any]
) -> dict[str, Any]:
    """A Piceli-owned scratch claim shaped like ``source``.

    ``spec`` holds ``storage_class``, ``size`` and ``access_modes`` (and
    ``volume_mode``) from the planned source claim. It is labelled
    ``app.kubernetes.io/managed-by=piceli``, ``piceli.io/scratch=true`` and
    ``piceli.io/restore-verify=<point>``, and names its source in an
    annotation, so a leftover is easy to find and never mistaken for data.
    """
    body: dict[str, Any] = {
        "accessModes": list(spec.get("access_modes") or ["ReadWriteOnce"]),
        "resources": {"requests": {"storage": str(spec["size"])}},
    }
    if spec.get("storage_class") is not None:
        body["storageClassName"] = spec["storage_class"]
    if spec.get("volume_mode"):
        body["volumeMode"] = spec["volume_mode"]
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": name,
            "labels": {
                "app.kubernetes.io/managed-by": "piceli",
                SCRATCH_LABEL: "true",
                VERIFY_LABEL: point,
            },
            "annotations": {SOURCE_ANNOTATION: source},
        },
        "spec": body,
    }


def _status_code(raw: bytes) -> int | None:
    """The exit code from a ``v4`` exec status frame (``None``: did not run)."""
    try:
        status = json.loads(raw or b"{}") or {}
    except ValueError:
        return None
    if not isinstance(status, dict):
        return None
    if status.get("status") == "Success":
        return 0
    for cause in (status.get("details") or {}).get("causes") or []:
        if isinstance(cause, dict) and cause.get("reason") == "ExitCode":
            try:
                return int(str(cause.get("message")))
            except ValueError:
                return None
    return None


def stream_exec(
    socket_: Any,
    *,
    stdout: Callable[[bytes], None],
    stdin: Iterator[bytes] | None = None,
    timeout: float,
) -> int:
    """Drive one ``v4.channel.k8s.io`` exec: send ``stdin``, pass stdout on.

    stderr is counted, never kept or printed (it may name files). Returns the
    exit code.

    :raises RestorePointError: ``restore-point-copy-failed`` on a timeout, a
        closed stream or an exec that did not run.
    """
    import websocket  # websocket-client, a kubernetes dependency

    deadline = time.monotonic() + timeout
    status = b""
    try:
        if stdin is not None:
            for chunk in stdin:
                if time.monotonic() > deadline:
                    raise RestorePointError(
                        "restore-point-copy-failed",
                        f"the copy ran longer than {int(timeout)}s",
                        failed=True,
                    )
                for start in range(0, len(chunk), _CHUNK):
                    socket_.send_binary(bytes([_STDIN]) + chunk[start : start + _CHUNK])
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RestorePointError(
                    "restore-point-copy-failed",
                    f"the copy ran longer than {int(timeout)}s",
                    failed=True,
                )
            socket_.settimeout(remaining)
            try:
                opcode, data = socket_.recv_data()
            except websocket.WebSocketTimeoutException:
                raise RestorePointError(
                    "restore-point-copy-failed",
                    f"the copy ran longer than {int(timeout)}s",
                    failed=True,
                ) from None
            except websocket.WebSocketConnectionClosedException:
                break
            if opcode == websocket.ABNF.OPCODE_CLOSE:
                break
            if opcode not in (websocket.ABNF.OPCODE_BINARY, websocket.ABNF.OPCODE_TEXT):
                continue
            if isinstance(data, str):
                data = data.encode()
            if not data:
                continue
            channel, payload = data[0], data[1:]
            if channel == _STDOUT:
                stdout(payload)
            elif channel == _STATUS and len(status) < 65536:
                status += payload
    finally:
        socket_.close()
    code = _status_code(status)
    if code is None:
        raise RestorePointError(
            "restore-point-copy-failed",
            "the helper command did not run (exec unavailable)",
            failed=True,
        )
    return code


class RestoreCluster:
    """Reads and the few writes restore points need, in one namespace.

    :param client: A ``kubernetes.client.ApiClient`` built from an explicit
        kubeconfig and context.
    :param namespace: The release namespace.
    :param say: Receives human progress lines (no file content, no values).
    :param poll_seconds: Interval of the waits.
    """

    def __init__(
        self,
        client: Any,
        namespace: str,
        *,
        say: Callable[[str], None] | None = None,
        poll_seconds: float = 1.0,
        request_seconds: float = 30.0,
    ) -> None:
        self.client = client
        self.namespace = namespace
        self.say = say or (lambda _line: None)
        self.poll_seconds = poll_seconds
        self.request_seconds = request_seconds

    def close(self) -> None:
        self.client.close()

    # ------------------------------------------------------------- reads
    def _list(self, api: Any, method: str, **query: Any) -> list[dict[str, Any]]:
        response = getattr(api, method)(
            self.namespace,
            _preload_content=False,
            _request_timeout=self.request_seconds,
            **query,
        )
        items = json.loads(response.data).get("items") or []
        return [item for item in items if isinstance(item, dict)]

    def workloads(self) -> list[dict[str, Any]]:
        """Live Deployments, StatefulSets and DaemonSets, with their kinds."""
        from kubernetes.client import AppsV1Api

        api = AppsV1Api(self.client)
        found = []
        for kind, method in (
            ("Deployment", "list_namespaced_deployment"),
            ("StatefulSet", "list_namespaced_stateful_set"),
            ("DaemonSet", "list_namespaced_daemon_set"),
        ):
            for item in self._list(api, method):
                item["kind"] = kind
                found.append(item)
        return found

    def claims(self) -> list[dict[str, Any]]:
        from kubernetes.client import CoreV1Api

        return self._list(
            CoreV1Api(self.client), "list_namespaced_persistent_volume_claim"
        )

    def pods(self, selector: str | None = None) -> list[dict[str, Any]]:
        from kubernetes.client import CoreV1Api

        query = {"label_selector": selector} if selector else {}
        return self._list(CoreV1Api(self.client), "list_namespaced_pod", **query)

    def _workload(self, kind: str, name: str) -> dict[str, Any]:
        from kubernetes.client import AppsV1Api

        method = {
            "Deployment": "read_namespaced_deployment",
            "StatefulSet": "read_namespaced_stateful_set",
        }[kind]
        response = getattr(AppsV1Api(self.client), method)(
            name,
            self.namespace,
            _preload_content=False,
            _request_timeout=self.request_seconds,
        )
        return dict(json.loads(response.data))

    def workload(self, kind: str, name: str) -> dict[str, Any] | None:
        """The live workload, or ``None`` when it does not exist."""
        from kubernetes.client.exceptions import ApiException

        try:
            return self._workload(kind, name)
        except ApiException as error:
            if error.status == 404:
                return None
            raise

    def _selector(self, workload: Mapping[str, Any]) -> str:
        labels = ((workload.get("spec") or {}).get("selector") or {}).get(
            "matchLabels"
        ) or {}
        return ",".join(f"{key}={value}" for key, value in sorted(labels.items()))

    def claim(self, name: str) -> dict[str, Any] | None:
        """One claim, or ``None`` when it does not exist."""
        from kubernetes.client import CoreV1Api
        from kubernetes.client.exceptions import ApiException

        try:
            response = CoreV1Api(self.client).read_namespaced_persistent_volume_claim(
                name,
                self.namespace,
                _preload_content=False,
                _request_timeout=self.request_seconds,
            )
        except ApiException as error:
            if error.status == 404:
                return None
            raise
        return dict(json.loads(response.data))

    def volume_affinity(self, volume: str) -> dict[str, Any] | None:
        """``spec.nodeAffinity.required`` of a PersistentVolume (a node-local
        volume names its node there); ``None`` when absent or unreadable."""
        from kubernetes.client import CoreV1Api
        from kubernetes.client.exceptions import ApiException

        try:
            response = CoreV1Api(self.client).read_persistent_volume(
                volume, _preload_content=False, _request_timeout=self.request_seconds
            )
        except ApiException:
            return None
        body = json.loads(response.data) or {}
        required = ((body.get("spec") or {}).get("nodeAffinity") or {}).get("required")
        return dict(required) if isinstance(required, dict) else None

    def create_claim(self, manifest: Mapping[str, Any]) -> None:
        from kubernetes.client import CoreV1Api

        CoreV1Api(self.client).create_namespaced_persistent_volume_claim(
            self.namespace,
            dict(manifest),
            field_manager=FIELD_MANAGER,
            _request_timeout=self.request_seconds,
        )

    def delete_claim(self, name: str, seconds: float) -> bool:
        """Delete a scratch claim and wait until it is gone; ``True`` when gone.

        Only a claim labelled ``piceli.io/scratch=true`` is deleted, with
        preconditions on its uid and resource version: a live claim of the
        same name is never touched.
        """
        from kubernetes.client import CoreV1Api
        from kubernetes.client.exceptions import ApiException

        current = self.claim(name)
        if current is None:
            return True
        metadata = current.get("metadata") or {}
        if (metadata.get("labels") or {}).get(SCRATCH_LABEL) != "true":
            return False
        try:
            CoreV1Api(self.client).delete_namespaced_persistent_volume_claim(
                name,
                self.namespace,
                body={
                    "preconditions": {
                        "uid": metadata.get("uid"),
                        "resourceVersion": metadata.get("resourceVersion"),
                    },
                    "propagationPolicy": "Background",
                },
                _request_timeout=self.request_seconds,
            )
        except ApiException as error:
            if error.status != 404:
                return False
        deadline = time.monotonic() + seconds
        while self.claim(name) is not None:
            if time.monotonic() >= deadline:
                return False
            time.sleep(self.poll_seconds)
        return True

    # ------------------------------------------------------------ writers
    def scale(self, kind: str, name: str, replicas: int) -> None:
        """Set ``spec.replicas`` through the scale subresource, then drop the
        ``managedFields`` entry that request may leave (see the module doc)."""
        from kubernetes.client import AppsV1Api

        api = AppsV1Api(self.client)
        method = {
            "Deployment": "patch_namespaced_deployment_scale",
            "StatefulSet": "patch_namespaced_stateful_set_scale",
        }[kind]
        getattr(api, method)(
            name,
            self.namespace,
            {"spec": {"replicas": int(replicas)}},
            field_manager=FIELD_MANAGER,
            _request_timeout=self.request_seconds,
        )
        self._drop_manager(kind, name)

    def _drop_manager(self, kind: str, name: str) -> None:
        from kubernetes.client import AppsV1Api

        current = self._workload(kind, name)
        fields = (current.get("metadata") or {}).get("managedFields") or []
        kept = [item for item in fields if item.get("manager") != FIELD_MANAGER]
        if len(kept) == len(fields) or not kept:
            return
        method = {
            "Deployment": "patch_namespaced_deployment",
            "StatefulSet": "patch_namespaced_stateful_set",
        }[kind]
        getattr(AppsV1Api(self.client), method)(
            name,
            self.namespace,
            [
                {
                    "op": "test",
                    "path": "/metadata/resourceVersion",
                    "value": current["metadata"]["resourceVersion"],
                },
                {"op": "replace", "path": "/metadata/managedFields", "value": kept},
            ],
            _request_timeout=self.request_seconds,
        )

    def run_hook(self, writer: Mapping[str, Any], hook: Mapping[str, Any]) -> None:
        """Run one quiesce hook in every running pod of ``writer``.

        :raises RestorePointError: ``restore-point-quiesce-failed``.
        """
        live = self.workload(str(writer["kind"]), str(writer["name"]))
        if live is None:
            return
        pods = [
            pod
            for pod in self.pods(self._selector(live))
            if (pod.get("status") or {}).get("phase") == "Running"
            and not (pod.get("metadata") or {}).get("deletionTimestamp")
        ]
        for pod in pods:
            name = str(pod["metadata"]["name"])
            if hook["type"] == "http":
                self._http_hook(name, hook)
            else:
                self._exec_hook(name, hook)
            self.say(f"quiesce {hook['type']} hook ran in pod {name}")

    def _http_hook(self, pod: str, hook: Mapping[str, Any]) -> None:
        from kubernetes.client import CoreV1Api
        from kubernetes.client.exceptions import ApiException

        api = CoreV1Api(self.client)
        method = getattr(
            api, f"connect_{str(hook['method']).lower()}_namespaced_pod_proxy_with_path"
        )
        try:
            response = method(
                f"{pod}:{hook['port']}",
                self.namespace,
                str(hook["path"]).lstrip("/"),
                _preload_content=False,
                _request_timeout=hook["timeout_seconds"],
            )
            status = int(response.status)
        except ApiException as error:
            status = int(error.status or 0)
        except Exception as error:
            raise RestorePointError(
                "restore-point-quiesce-failed",
                f"quiesce hook {hook['method']} {hook['path']} in pod {pod} did "
                f"not answer ({type(error).__name__})",
            ) from None
        if status != int(hook["expect"]):
            raise RestorePointError(
                "restore-point-quiesce-failed",
                f"quiesce hook {hook['method']} {hook['path']} in pod {pod} "
                f"answered {status}, expected {hook['expect']}",
            )

    def _exec_hook(self, pod: str, hook: Mapping[str, Any]) -> None:
        code = self.exec(
            pod,
            list(hook["command"]),
            container=hook.get("container"),
            timeout=float(hook["timeout_seconds"]),
            code="restore-point-quiesce-failed",
        )
        if code != 0:
            raise RestorePointError(
                "restore-point-quiesce-failed",
                f"quiesce hook command in pod {pod} exited {code}",
            )

    def wait_stopped(
        self,
        writers: Sequence[Mapping[str, Any]],
        claims: Sequence[str],
        seconds: float,
    ) -> None:
        """Wait until every writer reports no replicas and no pod mounts any of
        ``claims`` writably, terminating pods included.

        :raises RestorePointError: ``restore-point-writers-remain``.
        """
        deadline = time.monotonic() + seconds
        said = 0.0
        while True:
            remaining = writer_pods(self.pods(), claims)
            counting = [
                f"{item['kind']}/{item['name']}"
                for item in writers
                if (
                    (
                        (self.workload(str(item["kind"]), str(item["name"])) or {}).get(
                            "status"
                        )
                        or {}
                    ).get("replicas")
                    or 0
                )
                > 0
            ]
            if not remaining and not counting:
                return
            if time.monotonic() >= deadline:
                raise RestorePointError(
                    "restore-point-writers-remain",
                    f"after {int(seconds)}s pods still mount the claims writably: "
                    + ", ".join(remaining or counting),
                    failed=True,
                )
            if time.monotonic() - said > 10:
                said = time.monotonic()
                self.say(
                    "waiting for writer pods to be gone: "
                    + ", ".join(remaining or counting)
                )
            time.sleep(self.poll_seconds)

    # ------------------------------------------------------------ helpers
    @contextmanager
    def helper(
        self,
        name: str,
        manifest: Mapping[str, Any],
        seconds: float,
    ) -> Iterator[str]:
        """Create the helper Job, yield its running pod's name, delete it after
        (and wait until its pod is gone)."""
        from kubernetes.client import BatchV1Api
        from kubernetes.client.exceptions import ApiException

        api = BatchV1Api(self.client)
        api.create_namespaced_job(
            self.namespace, dict(manifest), _request_timeout=self.request_seconds
        )
        selector = f"job-name={name}"
        try:
            deadline = time.monotonic() + seconds
            while True:
                pods = self.pods(selector)
                running = [
                    pod
                    for pod in pods
                    if (pod.get("status") or {}).get("phase") == "Running"
                ]
                if running:
                    break
                if any(
                    (pod.get("status") or {}).get("phase") in {"Failed", "Succeeded"}
                    for pod in pods
                ):
                    raise RestorePointError(
                        "restore-point-helper-failed",
                        f"helper pod of Job {name} ended before the copy",
                        failed=True,
                    )
                if time.monotonic() >= deadline:
                    raise RestorePointError(
                        "restore-point-helper-failed",
                        f"helper pod of Job {name} was not running after "
                        f"{int(seconds)}s (image, scheduling or the claim's node)",
                        failed=True,
                    )
                time.sleep(self.poll_seconds)
            yield str(running[0]["metadata"]["name"])
        finally:
            try:
                api.delete_namespaced_job(
                    name,
                    self.namespace,
                    propagation_policy="Background",
                    grace_period_seconds=0,
                    _request_timeout=self.request_seconds,
                )
            except ApiException as error:
                if error.status != 404:
                    raise
            deadline = time.monotonic() + seconds
            while self.pods(selector) and time.monotonic() < deadline:
                time.sleep(self.poll_seconds)

    def exec(
        self,
        pod: str,
        command: list[str],
        *,
        container: str | None = None,
        timeout: float,
        stdout: Callable[[bytes], None] | None = None,
        stdin: Iterator[bytes] | None = None,
        code: str = "restore-point-copy-failed",
    ) -> int:
        """Run ``command`` in ``pod`` over ``pods/exec``; return its exit code."""
        from piceli.checks.context import open_exec_socket

        try:
            socket_ = open_exec_socket(
                self.client,
                self.namespace,
                pod,
                command,
                container,
                timeout,
                stdin=stdin is not None,
            )
        except Exception as error:
            raise RestorePointError(
                code,
                f"exec in pod {pod} is unavailable ({type(error).__name__})",
                failed=True,
            ) from None
        try:
            return stream_exec(
                socket_,
                stdout=stdout or (lambda _chunk: None),
                stdin=stdin,
                timeout=timeout,
            )
        except RestorePointError as error:
            raise RestorePointError(code, str(error), failed=True) from None
