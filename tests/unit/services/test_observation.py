"""Shared watches keep scoped snapshots and bounded replay across clients."""

from __future__ import annotations

import queue
import time
from pathlib import Path
from typing import Any

from piceli.k8s.ops.provider_factory import KubeconfigTarget
from piceli.services.observation import ObservationHub
from piceli.services.registration import Registration


class Reader:
    queues: dict[str, queue.Queue[tuple[str, dict[str, Any]]]] = {}
    lists = 0

    def __init__(self, registration: Registration) -> None:
        self.scope = registration.id
        self.last_versions = {("v1", "Pod", "shop"): "1"}

    def list(self, _api: str, _kind: str, _namespace: str) -> list[dict[str, Any]]:
        Reader.lists += 1
        return [
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {
                    "name": "api",
                    "namespace": "shop",
                    "uid": "old",
                    "resourceVersion": "1",
                },
            }
        ]

    def watch(
        self, _api: str, _kind: str, _namespace: str, _version: str
    ) -> list[tuple[str, dict[str, Any]]]:
        try:
            return [Reader.queues[self.scope].get(timeout=0.1)]
        except queue.Empty:
            return []

    def close(self) -> None:
        pass


def registration(id: str, context: str = "local") -> Registration:
    return Registration(
        id, id, KubeconfigTarget(Path("/explicit/config"), context, "shop")
    )


def event(name: str, version: str) -> tuple[str, dict[str, Any]]:
    return (
        "MODIFIED",
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": name,
                "namespace": "shop",
                "uid": version,
                "resourceVersion": version,
            },
        },
    )


def test_shared_watch_scoped_replay_and_expiry() -> None:
    Reader.queues = {"one": queue.Queue(), "two": queue.Queue()}
    Reader.lists = 0
    one, two = registration("one"), registration("two", "local-two")
    hub = ObservationHub(Reader, max_watches=2, replay_limit=2, idle_seconds=5)
    try:
        first = Reader(one)
        assert hub.observe(one, first, "v1", "Pod")[0][0]["metadata"]["uid"] == "old"
        assert hub.observe(one, first, "v1", "Pod")[0][0]["metadata"]["uid"] == "old"
        assert Reader.lists == 1
        hub.observe(two, Reader(two), "v1", "Pod")
        assert Reader.lists == 2
        Reader.queues["one"].put(event("api", "2"))
        deadline = time.monotonic() + 2
        while not hub.events("one", "0")[0] and time.monotonic() < deadline:
            time.sleep(0.01)
        events, cursor, reset = hub.events("one", "0")
        assert not reset and len(events) == 1
        assert events[0].scope == "one" and events[0].kind == "upsert"
        assert hub.events("two", "0")[0] == []
        assert hub.observe(one, first, "v1", "Pod")[0][0]["metadata"]["uid"] == "2"
        Reader.queues["one"].put(event("api", "3"))
        Reader.queues["one"].put(event("api", "4"))
        while hub.cursor() != "3" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert hub.cursor() == "3"
        assert hub.events("one", "0")[2]
        assert hub.events("one", cursor)[2] is False
    finally:
        hub.close()


def test_same_credential_boundary_shares_one_watch_across_registrations() -> None:
    Reader.queues = {"one": queue.Queue(), "two": queue.Queue()}
    Reader.lists = 0
    one, two = registration("one"), registration("two")
    hub = ObservationHub(Reader, max_watches=1, idle_seconds=5)
    try:
        hub.observe(one, Reader(one), "v1", "Pod")
        hub.observe(two, Reader(two), "v1", "Pod")
        assert Reader.lists == 1
        Reader.queues["one"].put(event("api", "2"))
        deadline = time.monotonic() + 2
        while not hub.events("two", "0")[0] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(hub.events("one", "0")[0]) == 1
        assert len(hub.events("two", "0")[0]) == 1
        assert hub.events("three", "0")[0] == []
    finally:
        hub.close()


def test_watch_error_marks_snapshot_stale_then_relists_with_reset() -> None:
    class RelistingReader(Reader):
        lists = 0
        watches = 0

        def list(self, _api: str, _kind: str, _namespace: str) -> list[dict[str, Any]]:
            type(self).lists += 1
            revision = str(type(self).lists)
            self.last_versions[("v1", "Pod", "shop")] = revision
            return [event("api", revision)[1]]

        def watch(
            self, _api: str, _kind: str, _namespace: str, _version: str
        ) -> list[tuple[str, dict[str, Any]]]:
            type(self).watches += 1
            if type(self).watches == 1:
                return [("ERROR", {"metadata": {"code": 410}})]
            time.sleep(0.01)
            return []

    scope = registration("one")
    hub = ObservationHub(RelistingReader, idle_seconds=5)
    try:
        rows, stale = hub.observe(scope, RelistingReader(scope), "v1", "Pod")
        assert not stale and rows[0]["metadata"]["uid"] == "1"
        deadline = time.monotonic() + 4
        while RelistingReader.lists < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert RelistingReader.lists >= 2
        events, _, reset = hub.events("one", "0")
        assert not reset and any(item.kind == "reset" for item in events)
        latest, stale = hub.observe(scope, RelistingReader(scope), "v1", "Pod")
        assert not stale and latest[0]["metadata"]["uid"] == "2"
    finally:
        hub.close()
