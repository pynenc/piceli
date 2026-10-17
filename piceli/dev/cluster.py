"""The Kubernetes side of development runs (0.18.0): Jobs, uploads, logs.

:class:`DevCluster` is the port :mod:`piceli.dev.client` drives; tests use a
local stand-in that runs the wrapper on directories. Uploads and artifact
downloads go over ``pods/exec`` (stdin is bounded with ``head -c``: the v4
exec protocol cannot close stdin), logs over ``pods/log?follow=true``.

Importing this module is side-effect free.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

from piceli.dev.jobs import RUN_LABEL
from piceli.dev.model import (
    CONFIG_MAP,
    NAMESPACE,
    STATUS_CONFIG_MAP,
    DevBuilds,
    DevError,
)

_CHUNK = 256 * 1024


def _chunks(path: Path) -> Iterator[bytes]:
    with path.open("rb") as stream:
        yield from iter(lambda: stream.read(_CHUNK), b"")


class DevCluster:
    """Runs in ``piceli-dev`` through an :class:`piceli.gitops.install.Api`."""

    def __init__(self, api: Any, *, poll_seconds: float = 1.0) -> None:
        self.api = api
        self.poll_seconds = poll_seconds

    # ------------------------------------------------------------ reads
    def config(self) -> DevBuilds | None:
        """The installed declaration (``piceli-dev-config``), else ``None``."""
        found = self.api.call(
            f"/api/v1/namespaces/{NAMESPACE}/configmaps/{CONFIG_MAP}", "GET"
        )
        data = (found or {}).get("data") if isinstance(found, dict) else None
        if not isinstance(data, dict) or "config.json" not in data:
            return None
        try:
            return DevBuilds.from_dict(json.loads(data["config.json"]))
        except Exception:
            return None

    def jobs(self, selector: str | None = None) -> list[dict[str, Any]]:
        query = f"?labelSelector={quote(selector or RUN_LABEL)}"
        found = self.api.call(
            f"/apis/batch/v1/namespaces/{NAMESPACE}/jobs{query}", "GET"
        )
        return [i for i in (found or {}).get("items") or () if isinstance(i, dict)]

    def job(self, run: str) -> dict[str, Any] | None:
        found = self.jobs(f"{RUN_LABEL}={run}")
        return found[0] if found else None

    def pod(self, run: str) -> dict[str, Any] | None:
        query = f"?labelSelector={quote(f'{RUN_LABEL}={run}')}"
        found = self.api.call(f"/api/v1/namespaces/{NAMESPACE}/pods{query}", "GET")
        items = [i for i in (found or {}).get("items") or () if isinstance(i, dict)]
        return items[-1] if items else None

    # ------------------------------------------------------------ writes
    def create(self, job: dict[str, Any]) -> None:
        self.api.call(
            f"/apis/batch/v1/namespaces/{NAMESPACE}/jobs", "POST", job, missing_ok=False
        )

    def delete(self, run: str) -> None:
        from piceli.dev.jobs import run_name

        self.api.call(
            f"/apis/batch/v1/namespaces/{NAMESPACE}/jobs/{run_name(run)}",
            "DELETE",
            {"propagationPolicy": "Background"},
        )

    def unsuspend(self, run: str) -> None:
        from piceli.dev.jobs import run_name

        self.api.call(
            f"/apis/batch/v1/namespaces/{NAMESPACE}/jobs/{run_name(run)}",
            "PATCH",
            {"spec": {"suspend": False}},
            "application/merge-patch+json",
        )

    def node(self, name: str) -> dict[str, Any] | None:
        found = self.api.call(f"/api/v1/nodes/{quote(name)}", "GET")
        return found if isinstance(found, dict) else None

    def result(self, run: str) -> dict[str, Any] | None:
        """The wrapper's result line from the run's log, if it printed one."""
        from piceli.dev.pod import RESULT_MARKER

        pod = self.pod(run)
        if pod is None:
            return None
        text = self.api.text(
            f"/api/v1/namespaces/{NAMESPACE}/pods/{pod['metadata']['name']}/log",
            [("container", "run"), ("tailLines", "5")],
        )
        for line in reversed((text or "").splitlines()):
            if line.startswith(RESULT_MARKER):
                try:
                    value = json.loads(line[len(RESULT_MARKER) :])
                except ValueError:
                    return None
                return value if isinstance(value, dict) else None
        return None

    def logs(self, run: str, tail: int | None = None) -> str | None:
        pod = self.pod(run)
        if pod is None:
            return None
        query = [("container", "run")] + ([("tailLines", str(tail))] if tail else [])
        return self.api.text(
            f"/api/v1/namespaces/{NAMESPACE}/pods/{pod['metadata']['name']}/log", query
        )

    def status(self) -> dict[str, Any] | None:
        """The scheduler's published ``piceli-dev-status``, if any."""
        from piceli.infra.cluster_init import NAMESPACE as SYSTEM

        found = self.api.call(
            f"/api/v1/namespaces/{SYSTEM}/configmaps/{STATUS_CONFIG_MAP}", "GET"
        )
        data = (found or {}).get("data") if isinstance(found, dict) else None
        try:
            value = json.loads((data or {}).get("status.json") or "null")
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    def publish(self, document: dict[str, Any]) -> None:
        """Write ``piceli-dev-status`` in ``piceli-system``."""
        from piceli.infra.cluster_init import NAMESPACE as SYSTEM

        path = f"/api/v1/namespaces/{SYSTEM}/configmaps"
        body = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": STATUS_CONFIG_MAP,
                "namespace": SYSTEM,
                "labels": {"app.kubernetes.io/managed-by": "piceli"},
            },
            "data": {"status.json": json.dumps(document, sort_keys=True)},
        }
        if self.api.call(f"{path}/{STATUS_CONFIG_MAP}", "GET") is None:
            self.api.call(path, "POST", body, missing_ok=False)
        else:
            self.api.call(f"{path}/{STATUS_CONFIG_MAP}", "PUT", body, missing_ok=False)

    def put_git_secret(self, username: str, password: str, allowed: list[str]) -> bool:
        """Create or update ``piceli-dev-git``; ``True`` when it changed.

        The fetch init container of each run mounts it (the run's own
        container never does) and fetches only the ``allowed`` URLs.
        """
        import base64

        from piceli.dev.jobs import GIT_SECRET

        def b64(text: str) -> str:
            return base64.b64encode(text.encode()).decode()

        data = {
            "username": b64(username),
            "password": b64(password),
            "allowed": b64("\n".join(sorted(set(allowed))) + "\n"),
        }
        path = f"/api/v1/namespaces/{NAMESPACE}/secrets"
        found = self.api.call(f"{path}/{GIT_SECRET}", "GET")
        body = {
            "apiVersion": "v1",
            "kind": "Secret",
            "type": "Opaque",
            "metadata": {
                "name": GIT_SECRET,
                "namespace": NAMESPACE,
                "labels": {"app.kubernetes.io/managed-by": "piceli"},
            },
            "data": data,
        }
        if found is None:
            self.api.call(path, "POST", body, missing_ok=False)
            return True
        if (found or {}).get("data") == data:
            return False
        self.api.call(f"{path}/{GIT_SECRET}", "PUT", body, missing_ok=False)
        return True

    # ------------------------------------------------------------ the pod
    def wait_started(
        self,
        run: str,
        timeout: float,
        on_wait: Callable[[str], None] = lambda _w: None,
    ) -> str:
        """The run's pod name once its container runs.

        :raises DevError: ``dev-run-cancelled`` (the Job went away),
            ``dev-run-failed`` (the pod ended or cannot start),
            ``dev-queue-timeout``.
        """
        deadline = time.monotonic() + timeout
        last = None
        position_at, position = 0.0, ""
        while True:
            job = self.job(run)
            if job is None:
                raise DevError(
                    "dev-run-cancelled", f"run {run} was cancelled", failed=True
                )
            pod = self.pod(run)
            phase = ((pod or {}).get("status") or {}).get("phase")
            why = _waiting(pod)
            if pod is None and (job.get("spec") or {}).get("suspend"):
                if time.monotonic() - position_at >= 5:
                    position_at = time.monotonic()
                    queued = (self.status() or {}).get("queued") or ()
                    found = next((q for q in queued if q.get("run") == run), None)
                    position = (
                        f"queued #{found['position']} of {len(queued)}"
                        if found
                        else "queued"
                    )
                why = position or "queued"
            if phase == "Running":
                return str(pod["metadata"]["name"])  # type: ignore[index]
            if phase in {"Succeeded", "Failed"}:
                raise DevError(
                    "dev-run-failed",
                    f"the run's pod ended before its upload ({phase})",
                    failed=True,
                )
            if why in {
                "ErrImagePull",
                "ImagePullBackOff",
                "InvalidImageName",
                "CreateContainerConfigError",
            }:
                raise DevError(
                    "dev-run-failed", f"the run's pod cannot start ({why})", failed=True
                )
            state = why or phase or "queued"
            if state != last:
                on_wait(state)
                last = state
            if time.monotonic() >= deadline:
                raise DevError(
                    "dev-queue-timeout", f"run {run} did not start in time", failed=True
                )
            time.sleep(self.poll_seconds)

    def _exec(self, pod: str, command: list[str], **kwargs: Any) -> int:
        from piceli.checks.context import open_exec_socket
        from piceli.restore.cluster import stream_exec
        from piceli.restore.model import RestorePointError

        try:
            socket_ = open_exec_socket(
                self.api.client, NAMESPACE, pod, command, "run", 600,
                stdin=kwargs.get("stdin") is not None,
            )  # fmt: skip
            return stream_exec(
                socket_,
                stdout=kwargs.get("stdout") or (lambda _chunk: None),
                stdin=kwargs.get("stdin"),
                timeout=float(kwargs.get("timeout") or 600),
            )
        except (RestorePointError, OSError, ValueError) as error:
            raise DevError(
                "dev-upload-failed",
                f"exec in the run's pod failed: {type(error).__name__}: "
                f"{str(error)[:200]}",
                failed=True,
            ) from None

    def upload(
        self,
        pod: str,
        archive: Path,
        sha256: str | None = None,
        progress: Callable[[int, int, float], None] = lambda _s, _t, _e: None,
    ) -> dict[str, Any]:
        """Upload ``archive`` in resumable chunks (see :func:`upload_chunks`)."""
        return upload_chunks(
            lambda command, stdin: self._exec(pod, command, stdin=stdin, timeout=300),
            archive,
            sha256,
            progress,
        )

    def have(self, pod: str) -> dict[str, Any] | None:
        """The fetch init container's report (``/work/have.json``), if any."""
        output = bytearray()
        try:
            code = self._exec(
                pod, ["cat", "/work/have.json"], stdout=output.extend, timeout=60
            )
        except DevError:
            return None
        if code != 0:
            return None
        try:
            value = json.loads(bytes(output).decode() or "null")
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    def download(self, pod: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as stream:
            code = self._exec(
                pod,
                ["cat", "/work/out/artifacts.tar.gz"],
                stdout=stream.write,
                timeout=1800,
            )
        self._exec(pod, ["touch", "/work/out/collected"])
        if code:
            raise DevError(
                "dev-upload-failed", "the artifacts could not be copied", failed=True
            )

    def follow(self, pod: str, on_line: Callable[[str], None]) -> None:
        """Every line of the run's log until the container ends.

        A dropped stream (0.20: ``Response ended prematurely`` from a proxy or
        a flaky link) is reopened from the start and the lines already seen
        are skipped, while the pod still runs; the run is never orphaned.
        """
        seen = 0
        for attempt in range(FOLLOW_ATTEMPTS):
            count = 0
            try:
                for line in self._stream(pod):
                    count += 1
                    if count > seen:
                        seen = count
                        on_line(line)
                return
            except Exception as error:  # urllib3 ProtocolError, OSError, ...
                if not self._running(pod) or attempt + 1 == FOLLOW_ATTEMPTS:
                    if self._running(pod):
                        raise DevError(
                            "dev-run-failed",
                            f"the run's log stream kept dropping: "
                            f"{type(error).__name__}",
                        ) from None
                    return
                time.sleep(min(2.0 * (attempt + 1), 10.0))

    def _running(self, pod: str) -> bool:
        found = self.api.call(
            f"/api/v1/namespaces/{NAMESPACE}/pods/{quote(pod)}", "GET"
        )
        phase = ((found or {}).get("status") or {}).get("phase")
        return phase in {"Running", "Pending"}

    def _stream(self, pod: str) -> Iterator[str]:
        response = self.api.client.call_api(
            f"/api/v1/namespaces/{NAMESPACE}/pods/{quote(pod)}/log",
            "GET",
            query_params=[("container", "run"), ("follow", "true")],
            header_params={"Accept": "*/*"},
            auth_settings=["BearerToken"],
            _preload_content=False,
            _request_timeout=None,
        )
        raw = response[0] if isinstance(response, tuple) else response
        pending = b""
        try:
            for block in raw.stream(64 * 1024):
                pending += block
                *lines, pending = pending.split(b"\n")
                for line in lines:
                    yield line.decode("utf-8", "replace")
            if pending:
                yield pending.decode("utf-8", "replace")
        finally:
            raw.release_conn()

    def clear_cache(
        self, job: dict[str, Any], timeout: float = 900.0
    ) -> dict[str, Any] | None:
        """Run a cache-clear Job to its end; its result, then delete it."""
        from piceli.dev.jobs import CLEAR_LABEL
        from piceli.dev.pod import RESULT_MARKER

        name = job["metadata"]["labels"][CLEAR_LABEL]
        self.api.call(
            f"/apis/batch/v1/namespaces/{NAMESPACE}/jobs", "POST", job, missing_ok=False
        )
        query = f"?labelSelector={quote(f'{CLEAR_LABEL}={name}')}"
        deadline = time.monotonic() + timeout
        result = None
        try:
            while time.monotonic() < deadline:
                found = self.api.call(
                    f"/api/v1/namespaces/{NAMESPACE}/pods{query}", "GET"
                )
                pods = [
                    i for i in (found or {}).get("items") or () if isinstance(i, dict)
                ]
                phase = ((pods[-1].get("status") or {}).get("phase")) if pods else None
                if phase in {"Succeeded", "Failed"}:
                    text = self.api.text(
                        f"/api/v1/namespaces/{NAMESPACE}/pods/"
                        f"{pods[-1]['metadata']['name']}/log",
                        [("container", "run"), ("tailLines", "5")],
                    )
                    for line in reversed((text or "").splitlines()):
                        if line.startswith(RESULT_MARKER):
                            value = json.loads(line[len(RESULT_MARKER) :])
                            result = value if isinstance(value, dict) else None
                            break
                    break
                time.sleep(self.poll_seconds)
        finally:
            self.api.call(
                f"/apis/batch/v1/namespaces/{NAMESPACE}/jobs/{job['metadata']['name']}",
                "DELETE",
                {"propagationPolicy": "Background"},
            )
        return result

    def ended(self, run: str) -> str | None:
        """Why the run's pod ended without a result: a registered reason."""
        if self.job(run) is None:
            return "dev-run-cancelled"
        pod = self.pod(run) or {}
        for status in (pod.get("status") or {}).get("containerStatuses") or ():
            terminated = (status.get("state") or {}).get("terminated") or {}
            if terminated.get("reason") == "OOMKilled":
                return "dev-run-out-of-memory"
        if ((pod.get("status") or {}).get("reason")) == "DeadlineExceeded":
            return "dev-run-timed-out"
        return "dev-run-failed"


#: Reopenings of a dropped log stream before the client gives up.
FOLLOW_ATTEMPTS = 30
#: Bytes per upload chunk, and the waits before each retry of a failed one.
CHUNK_BYTES = 4 * 1024 * 1024
RETRY_SECONDS = (1, 2, 4, 8, 16)


def upload_chunks(
    run_exec: Callable[[list[str], Iterator[bytes] | None], int],
    archive: Path,
    sha256: str | None,
    progress: Callable[[int, int, float], None],
    *,
    chunk_bytes: int = CHUNK_BYTES,
    sleep: Callable[[float], None] = time.sleep,
    root: str = "/work",
) -> dict[str, Any]:
    """Send ``archive`` as numbered parts, each retried on a dropped
    connection, then assemble it in the pod and mark it ready.

    ``run_exec(command, stdin)`` runs one exec in the run's pod and returns
    its exit code. Returns ``{"bytes", "chunks", "retries", "seconds"}``.

    :raises DevError: ``dev-upload-failed`` naming the chunk and the cause.
    """
    total = archive.stat().st_size
    count = max(1, -(-total // chunk_bytes))
    began = time.monotonic()
    retries = 0
    sent = 0
    with archive.open("rb") as stream:
        for index in range(count):
            data = stream.read(chunk_bytes)
            name = f"part-{index:05d}"
            script = (
                f"set -e; mkdir -p {root}/upload; "
                f"head -c {len(data)} > {root}/upload/.{name}; "
                f"mv {root}/upload/.{name} {root}/upload/{name}"
            )
            cause = ""
            for attempt in range(len(RETRY_SECONDS) + 1):
                try:
                    if run_exec(["sh", "-c", script], iter((data,))) == 0:
                        cause = ""
                        break
                    cause = "the pod could not write the part"
                except DevError as error:
                    cause = str(error)
                if attempt < len(RETRY_SECONDS):
                    retries += 1
                    sleep(RETRY_SECONDS[attempt])
            if cause:
                raise DevError(
                    "dev-upload-failed",
                    f"upload chunk {index + 1} of {count} failed "
                    f"{len(RETRY_SECONDS) + 1} times ({sent} of {total} bytes sent): {cause}",
                    failed=True,
                )
            sent += len(data)
            progress(sent, total, time.monotonic() - began)
    digest = f"printf %s {sha256} > tree.sha256; " if sha256 else ""
    finish = (
        f"set -e; cd {root}/upload; cat part-* > tree.tar.gz; rm -f part-*; "
        f"{digest}touch ready"
    )
    if run_exec(["sh", "-c", finish], None) != 0:
        raise DevError(
            "dev-upload-failed", "the pod could not assemble the upload", failed=True
        )
    return {
        "bytes": total,
        "chunks": count,
        "retries": retries,
        "seconds": round(time.monotonic() - began, 3),
    }


def _waiting(pod: dict[str, Any] | None) -> str | None:
    for status in ((pod or {}).get("status") or {}).get("initContainerStatuses") or ():
        state = status.get("state") or {}
        if "running" in state:
            return "fetching sources"
        reason = (state.get("waiting") or {}).get("reason")
        if reason:
            return str(reason)
    for status in ((pod or {}).get("status") or {}).get("containerStatuses") or ():
        reason = ((status.get("state") or {}).get("waiting") or {}).get("reason")
        if reason:
            return str(reason)
    for condition in ((pod or {}).get("status") or {}).get("conditions") or ():
        if condition.get("reason") == "Unschedulable":
            return "Unschedulable"
    return None
