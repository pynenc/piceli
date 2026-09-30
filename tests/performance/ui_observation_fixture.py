"""Reproducible API observation fixture; prints measurements, never artifacts.

This is the server half of the UI budget: 50 applications, 5,000 objects across
three targets and ten concurrent simulated clients. Browser paint, network RTT
and log-scroll budgets need their separate browser/cluster fixtures.
"""

from __future__ import annotations

import json
import platform
import statistics
import threading
import time
import tracemalloc
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from piceli.k8s.ops.provider_factory import KubeconfigTarget
from piceli.services.query import QueryService
from piceli.services.registration import Registration

KINDS = Registration(
    "shape", "shape", KubeconfigTarget(Path("/synthetic"), "shape", "shop")
).kinds


class Reader:
    lock = threading.Lock()
    lists = 0

    def __init__(self, registration: Registration) -> None:
        self.target = registration.target
        self.last_versions: dict[tuple[str, str, str], str] = {}

    def list(self, version: str, kind: str, namespace: str) -> list[dict[str, Any]]:
        with Reader.lock:
            Reader.lists += 1
        self.last_versions[(version, kind, namespace)] = "1"
        index = int(self.target.context.removeprefix("target-"))
        offset = KINDS.index((version, kind))
        buckets = 3 * len(KINDS)
        count = 5000 // buckets + int(index * len(KINDS) + offset < 5000 % buckets)
        return [
            {
                "apiVersion": version,
                "kind": kind,
                "metadata": {
                    "name": f"object-{number}",
                    "namespace": namespace,
                    "uid": f"{index}-{offset}-{number}",
                    "resourceVersion": "1",
                },
                "spec": {},
            }
            for number in range(count)
        ]

    def watch(
        self, _version: str, _kind: str, _namespace: str, _resource_version: str
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        time.sleep(0.1)
        return iter(())

    def logs(
        self,
        pod: str,
        namespace: str,
        container: str,
        *,
        previous: bool,
        tail_lines: int,
    ) -> str:
        raise AssertionError("synthetic observation fixture does not serve logs")

    def close(self) -> None:
        pass


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


def main() -> None:
    registrations = [
        Registration(
            f"app-{number}",
            f"App {number}",
            KubeconfigTarget(
                Path(f"/synthetic/target-{number % 3}"),
                f"target-{number % 3}",
                f"shop-{number % 3}",
            ),
        )
        for number in range(50)
    ]
    query = QueryService(registrations, reader_factory=Reader)
    Reader.lists = 0
    tracemalloc.start()
    started = time.perf_counter()
    try:
        with ThreadPoolExecutor(max_workers=10) as pool:
            durations = list(
                pool.map(
                    lambda registration: _measure(query, registration.id),
                    registrations,
                )
            )
        current, peak = tracemalloc.get_traced_memory()
        result = {
            "fixture": "50 applications, 5000 objects, 3 target scopes, 10 API clients",
            "platform": platform.platform(),
            "python": platform.python_version(),
            "p50_ms": round(statistics.median(durations) * 1000, 2),
            "p95_ms": round(percentile(durations, 0.95) * 1000, 2),
            "wall_seconds": round(time.perf_counter() - started, 2),
            "kubernetes_lists": Reader.lists,
            "shared_watches": len(query.observation._entries),
            "memory_current_mib": round(current / 2**20, 2),
            "memory_peak_mib": round(peak / 2**20, 2),
        }
        print(json.dumps(result, sort_keys=True))
    finally:
        query.close()
        tracemalloc.stop()


def _measure(query: QueryService, application_id: str) -> float:
    start = time.perf_counter()
    page = query.resources(application_id)
    assert len(page.items) == 100
    assert page.next_page is not None
    return time.perf_counter() - start


if __name__ == "__main__":
    main()
