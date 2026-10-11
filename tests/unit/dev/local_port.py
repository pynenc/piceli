"""A local stand-in for :class:`piceli.dev.cluster.DevCluster`.

``create`` starts the real wrapper (:func:`piceli.dev.pod.main`) in a thread
on directories: the pod's scratch and the shared cache.
"""

from __future__ import annotations

import json
import queue
import shutil
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from piceli.dev import pod as wrapper
from piceli.dev.jobs import RUN_LABEL
from piceli.dev.model import DevError


class LocalPort:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.jobs: dict[str, dict[str, Any]] = {}
        self.deleted: list[str] = []
        self.lines: dict[str, queue.Queue[str | None]] = {}
        self.threads: dict[str, threading.Thread] = {}
        self.cancel_before_start: set[str] = set()

    def _work(self, run: str) -> Path:
        return self.root / "pods" / run

    def create(self, job: dict[str, Any]) -> None:
        run = job["metadata"]["labels"][RUN_LABEL]
        self.jobs[run] = job
        env = {
            e["name"]: e["value"]
            for e in job["spec"]["template"]["spec"]["containers"][0]["env"]
        }
        spec = json.loads(env["PICELI_DEV_SPEC"])
        spec["poll_seconds"] = 0.01
        spec["isolation_check"] = False  # no API server here to cut off
        lines: queue.Queue[str | None] = queue.Queue()
        self.lines[run] = lines

        def go() -> None:
            try:
                if spec.get("sources"):  # the fetch init container, no Secret here
                    wrapper.fetch_main(spec, self._work(run), self.root / "cache", None)
                wrapper.main(spec, self._work(run), self.root / "cache", emit=lines.put)
            finally:
                lines.put(None)

        self.threads[run] = threading.Thread(target=go, daemon=True)
        if not job["spec"].get("suspend"):
            self.threads[run].start()

    def wait_started(
        self, run: str, timeout: float, on_wait: Callable[[str], None]
    ) -> str:
        if run in self.cancel_before_start:
            raise DevError("dev-run-cancelled", "cancelled", failed=True)
        on_wait("queued")
        if not self.threads[run].is_alive():
            self.threads[run].start()
        return run

    def have(self, pod: str) -> dict[str, Any] | None:
        import time

        path = self._work(pod) / "have.json"
        for _ in range(500):  # the init step runs in the pod's thread
            if path.exists():
                return dict(json.loads(path.read_text()))
            time.sleep(0.01)
        return None

    def upload(
        self, pod: str, archive: Path, sha256: str | None = None, progress: Any = None
    ) -> dict[str, Any]:
        upload = self._work(pod) / "upload"
        upload.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(archive, upload / "tree.tar.gz")
        if sha256:
            (upload / "tree.sha256").write_text(sha256)
        (upload / "ready").touch()
        self.uploaded = archive.stat().st_size
        return {"bytes": self.uploaded, "chunks": 1, "retries": 0, "seconds": 0.0}

    def follow(self, pod: str, on_line: Callable[[str], None]) -> None:
        while (line := self.lines[pod].get(timeout=60)) is not None:
            on_line(line)

    def download(self, pod: str, dest: Path) -> None:
        shutil.copyfile(self._work(pod) / "out" / "artifacts.tar.gz", dest)
        (self._work(pod) / "out" / "collected").touch()

    def ended(self, run: str) -> str:
        return "dev-run-failed"

    def delete(self, run: str) -> None:
        self.deleted.append(run)
