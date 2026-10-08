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
from piceli.dev.model import CONFIG_MAP, NAMESPACE, DevBuilds, DevError

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
        while True:
            if self.job(run) is None:
                raise DevError(
                    "dev-run-cancelled", f"run {run} was cancelled", failed=True
                )
            pod = self.pod(run)
            phase = ((pod or {}).get("status") or {}).get("phase")
            why = _waiting(pod)
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
                f"exec in the run's pod failed ({type(error).__name__})",
                failed=True,
            ) from None

    def upload(self, pod: str, archive: Path) -> None:
        size = archive.stat().st_size
        script = (
            "set -e; mkdir -p /work/upload; "
            f"head -c {int(size)} > /work/upload/tree.tar.gz.part; "
            "mv /work/upload/tree.tar.gz.part /work/upload/tree.tar.gz; "
            "touch /work/upload/ready"
        )
        if self._exec(pod, ["sh", "-c", script], stdin=_chunks(archive), timeout=1800):
            raise DevError(
                "dev-upload-failed", "the upload did not complete", failed=True
            )

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
        """Every line of the run's log until the container ends."""
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
                    on_line(line.decode("utf-8", "replace"))
            if pending:
                on_line(pending.decode("utf-8", "replace"))
        finally:
            raw.release_conn()

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


def _waiting(pod: dict[str, Any] | None) -> str | None:
    for status in ((pod or {}).get("status") or {}).get("containerStatuses") or ():
        reason = ((status.get("state") or {}).get("waiting") or {}).get("reason")
        if reason:
            return str(reason)
    for condition in ((pod or {}).get("status") or {}).get("conditions") or ():
        if condition.get("reason") == "Unschedulable":
            return "Unschedulable"
    return None
