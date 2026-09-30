"""Disposable synthetic UI service for browser-side performance measurements."""

from __future__ import annotations

import argparse
import asyncio
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Request
from starlette.responses import Response

from piceli.k8s.ops.provider_factory import KubeconfigTarget
from piceli.server.app import create_app
from piceli.services.logs import LogService
from piceli.services.query import QueryService
from piceli.services.registration import Registration
from tests.performance.ui_observation_fixture import KINDS, Reader


class LogBurstReader(Reader):
    """One observed Pod emits 1,000 timestamped lines per second on demand."""

    _lock = threading.Lock()
    _starts: dict[str, float] = {}

    def list(self, version: str, kind: str, namespace: str) -> list[dict[str, Any]]:
        rows = super().list(version, kind, namespace)
        if kind == "Pod" and rows:
            rows[0]["spec"] = {
                "containers": [{"name": "app", "image": "busybox:stable"}]
            }
            rows[0]["status"] = {"phase": "Running"}
        return rows

    def logs(
        self,
        pod: str,
        namespace: str,
        container: str,
        *,
        previous: bool,
        tail_lines: int,
    ) -> str:
        if pod != "object-0" or container != "app" or previous:
            raise AssertionError("synthetic burst is scoped to one current container")
        with self._lock:
            started = self._starts.setdefault(namespace, time.monotonic())
            count = min(10000, int((time.monotonic() - started) * 1000))
        return "\n".join(
            f"2026-09-30T00:00:{index // 1000:02d}Z burst-{index:05d}"
            for index in range(max(0, count - tail_lines), count)
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=4177)
    options = parser.parse_args()
    registrations = [
        Registration(
            f"app-{index}",
            f"App {index}",
            KubeconfigTarget(
                Path(f"/synthetic/target-{index % 3}"),
                f"target-{index % 3}",
                f"shop-{index % 3}",
            ),
            kinds=(("v1", "Pod"),) if index in {0, 1} else KINDS,
        )
        for index in range(50)
    ]
    query = QueryService(registrations, reader_factory=LogBurstReader)
    app = create_app(
        query,
        origin=f"http://127.0.0.1:{options.port}",
        logs=LogService(query),
    )

    @app.middleware("http")
    async def add_api_latency(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path.startswith("/api/v1/"):
            await asyncio.sleep(0.1)
        return await call_next(request)

    uvicorn.run(app, host="127.0.0.1", port=options.port, log_level="warning")


if __name__ == "__main__":
    main()
