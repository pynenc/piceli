"""Long-running shared-watch retention fixture; no persistent test artifacts.

This exercises the server observation boundary, not browser paint or a real
Kubernetes watch. The release run uses the default 1,800 seconds.
"""

from __future__ import annotations

import argparse
import gc
import json
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
from tests.performance.ui_observation_fixture import Reader


class ReplacingReader(Reader):
    generation = 1
    lock = threading.Lock()

    def list(self, version: str, kind: str, namespace: str) -> list[dict[str, Any]]:
        rows = super().list(version, kind, namespace)
        with self.lock:
            generation = self.generation
        self.last_versions[(version, kind, namespace)] = str(generation)
        if kind == "Pod" and rows:
            rows[0]["metadata"]["uid"] = f"replacement-{generation}"
            rows[0]["metadata"]["resourceVersion"] = str(generation)
        return rows

    def watch(
        self, version: str, kind: str, namespace: str, resource_version: str
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        time.sleep(0.25)
        with self.lock:
            generation = self.generation
        if kind != "Pod" or resource_version == str(generation):
            return iter(())
        target_index = int(self.target.context.removeprefix("target-"))
        return iter(
            (
                (
                    "MODIFIED",
                    {
                        "apiVersion": version,
                        "kind": kind,
                        "metadata": {
                            "name": "object-0",
                            "namespace": namespace,
                            "uid": f"replacement-{generation}",
                            "resourceVersion": str(generation),
                        },
                        "spec": {"target": target_index},
                    },
                ),
            )
        )


def _reader_client(query: QueryService, ids: list[str], stop: threading.Event) -> None:
    cursor = query.observation.cursor()
    index = 0
    while not stop.is_set():
        application_id = ids[index % len(ids)]
        page = query.resources(application_id)
        assert len(page.items) == 100
        if page.next_page:
            assert len(query.resources(application_id, page.next_page).items) <= 100
        _, cursor, _ = query.observation.events(application_id, cursor)
        index += 1
        stop.wait(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=1800)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 3600:
        parser.error("--seconds must be 1..3600")
    registrations = [
        Registration(
            f"app-{index}",
            f"App {index}",
            KubeconfigTarget(
                Path(f"/synthetic/target-{index % 3}"),
                f"target-{index % 3}",
                f"shop-{index % 3}",
            ),
        )
        for index in range(50)
    ]
    before_threads = {
        thread.ident
        for thread in threading.enumerate()
        if thread.name.startswith("piceli-observe-")
    }
    ReplacingReader.generation = 1
    query = QueryService(registrations, reader_factory=ReplacingReader)
    stop = threading.Event()
    tracemalloc.start()
    try:
        # Reach the bounded 32-snapshot steady state before measuring growth.
        for registration in registrations:
            query.resources(registration.id)
        gc.collect()
        initial, _ = tracemalloc.get_traced_memory()
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=10) as clients:
            futures = [
                clients.submit(
                    _reader_client,
                    query,
                    [item.id for item in registrations[worker::10]],
                    stop,
                )
                for worker in range(10)
            ]
            next_replacement = time.monotonic() + 5
            while time.monotonic() - started < args.seconds:
                time.sleep(0.25)
                if time.monotonic() >= next_replacement:
                    with ReplacingReader.lock:
                        ReplacingReader.generation += 1
                    next_replacement += 5
                for future in futures:
                    if future.done():
                        future.result()
            stop.set()
            for future in futures:
                future.result()
        gc.collect()
        retained, peak = tracemalloc.get_traced_memory()
        events, _, reset = query.observation.events("app-0", "0")
        result = {
            "seconds": round(time.monotonic() - started, 2),
            "clients": 10,
            "applications": 50,
            "objects": 5000,
            "replacements": ReplacingReader.generation - 1,
            "shared_watches": len(query.observation._entries),
            "replay_reset": reset,
            "recent_events": len(events),
            "initial_mib": round(initial / 2**20, 2),
            "retained_mib": round(retained / 2**20, 2),
            "peak_mib": round(peak / 2**20, 2),
        }
        print(json.dumps(result, sort_keys=True), flush=True)
        assert len(query.observation._entries) <= 33
        assert retained - initial < 64 * 2**20
    finally:
        stop.set()
        query.close()
        tracemalloc.stop()
        after_threads = {
            thread.ident
            for thread in threading.enumerate()
            if thread.name.startswith("piceli-observe-")
        }
        assert after_threads <= before_threads


if __name__ == "__main__":
    main()
