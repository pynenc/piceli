"""Acceptance: the shared-state store (Lease lock + chunked Secrets) on the fake API.

The store talks to the in-process fake API server over loopback HTTP exactly
as it talks to a cluster: create, merge patch with ``uid``/``resourceVersion``
preconditions, delete with preconditions. Clocks are injected, so lease
expiry and takeover need no sleeping.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from typing import Any

import pytest
from kubernetes.client import ApiClient, Configuration

from piceli.k8s.ops.discovery import ResourceListRequest, ResourceType
from piceli.state import cluster
from piceli.state.cluster import Api, ClusterStore
from piceli.state.errors import StateError
from piceli.testing import TARGET, FakeAPI, fake_cluster


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def served() -> Iterator[tuple[FakeAPI, str, Any]]:
    with fake_cluster() as running:
        yield running.api, running.url, running.provider


def store(
    url: str, clock: Clock, *, holder: str, layout: str = "pipeline", **kwargs: Any
) -> ClusterStore:
    configuration = Configuration()
    configuration.host = url
    configuration.proxy = None
    return ClusterStore(
        Api(ApiClient(configuration), 5),
        namespace=TARGET.namespace,
        name="shop",
        layout=layout,
        lease_seconds=kwargs.pop("lease_seconds", 30),
        holder=holder,
        clock=clock,
        **kwargs,
    )


def test_lock_is_exclusive_and_released(served) -> None:
    api, url, _ = served
    clock = Clock()
    first = store(url, clock, holder="runner-a/1/aa")
    second = store(url, clock, holder="runner-b/2/bb")
    fence = first.acquire()
    assert fence.transitions == 1
    lease = api.objects[("Lease", "piceli-lock-shop")]
    assert lease["spec"]["holderIdentity"] == "runner-a/1/aa"
    assert lease["metadata"]["labels"]["piceli.io/state"] == "shop"
    with pytest.raises(StateError) as raised:
        second.acquire()
    assert raised.value.code == "pipeline-locked"
    assert raised.value.details == {"holder": "runner-a/1/aa", "expires_in": 30}
    assert second.describe_lock() == {
        "holder": "runner-a/1/aa",
        "expires_in": 30,
        "transitions": 1,
    }
    first.release()
    assert "holderIdentity" not in api.objects[("Lease", "piceli-lock-shop")]["spec"]
    assert second.acquire().transitions == 2
    assert second.took_over is None  # a freed lock is not a takeover
    second.close()
    first.close()


def test_stale_lock_is_taken_over_and_the_old_holder_is_fenced(served) -> None:
    _, url, _ = served
    clock = Clock()
    dead = store(url, clock, holder="runner-a/1/aa")
    dead.acquire()
    dead.push(b"generation one")
    clock.now += 29
    with pytest.raises(StateError):
        store(url, clock, holder="runner-b/2/bb").acquire()
    clock.now += 2  # the holder stopped renewing more than 30s ago
    alive = store(url, clock, holder="runner-b/2/bb")
    assert alive.acquire().transitions == 2
    assert alive.took_over == {"holder": "runner-a/1/aa"}
    alive.push(b"generation two")
    # The previous holder wakes up: every write is refused (fencing).
    for write in (dead.renew, lambda: dead.push(b"stale")):
        with pytest.raises(StateError) as raised:
            write()
        assert raised.value.code == "state-lock-lost"
    data, manifest = alive.pull()
    assert data == b"generation two" and manifest is not None
    assert manifest["fence"] == {"holder": "runner-b/2/bb", "transitions": 2}


def test_snapshot_round_trip_in_chunks_and_old_chunks_are_removed(
    served, monkeypatch
) -> None:
    api, url, _ = served
    monkeypatch.setattr(cluster, "CHUNK_BYTES", 10)
    clock = Clock()
    writer = store(url, clock, holder="runner-a/1/aa")
    assert writer.pull() == (None, None)
    writer.acquire()
    first = writer.push(b"0123456789" * 3 + b"xy", files=3)
    assert first["generation"] == 1 and len(first["chunks"]) == 4
    head = api.objects[("Secret", "piceli-state-shop")]
    assert head["type"] == "piceli.io/state"
    manifest = json.loads(base64.b64decode(head["data"]["manifest"]))
    assert manifest["files"] == 3 and manifest["layout"] == "pipeline"
    # Unchanged content is not written again.
    assert writer.push(b"0123456789" * 3 + b"xy")["generation"] == 1
    second = writer.push(b"short")
    assert second["generation"] == 2 and second["chunks"] == ["piceli-state-shop-g2-0"]
    secrets = sorted(name for kind, name in api.objects if kind == "Secret")
    assert secrets == ["piceli-state-shop", "piceli-state-shop-g2-0"]
    reader = store(url, clock, holder="reader/3/cc")  # no lock needed to read
    assert reader.pull()[0] == b"short"


def test_a_writer_that_died_before_its_head_update_leaves_garbage_only(
    served, monkeypatch
) -> None:
    api, url, _ = served
    monkeypatch.setattr(cluster, "CHUNK_BYTES", 4)
    clock = Clock()
    writer = store(url, clock, holder="runner-a/1/aa")
    writer.acquire()
    writer.push(b"good")
    # A writer of generation 2 created three chunks, then died.
    for index in range(3):
        writer._create_chunk(f"piceli-state-shop-g2-{index}", b"junk", 2)
    assert writer.pull()[0] == b"good"  # the previous generation is intact
    writer.push(b"newer")  # 2 chunks: g2-0, g2-1; the stray g2-2 goes away
    secrets = sorted(name for kind, name in api.objects if kind == "Secret")
    assert secrets == [
        "piceli-state-shop",
        "piceli-state-shop-g2-0",
        "piceli-state-shop-g2-1",
    ]
    assert writer.pull()[0] == b"newer"


def test_altered_chunks_and_other_layouts_are_refused(served) -> None:
    api, url, _ = served
    clock = Clock()
    writer = store(url, clock, holder="runner-a/1/aa")
    writer.acquire()
    writer.push(b"content")
    chunk = api.objects[("Secret", "piceli-state-shop-g1-0")]
    chunk["data"]["chunk"] = base64.b64encode(b"altered").decode()
    with pytest.raises(StateError) as raised:
        writer.pull()
    assert raised.value.code == "state-corrupt"
    other = store(url, clock, holder="x/1/aa", layout="release")
    with pytest.raises(StateError) as raised:
        other.pull()
    assert raised.value.code == "state-layout-mismatch"


def test_state_objects_are_invisible_to_discovery(served) -> None:
    api, url, provider = served
    clock = Clock()
    writer = store(url, clock, holder="runner-a/1/aa")
    writer.acquire()
    writer.push(b"content")
    from piceli.testing import manifest

    api.put(manifest("Secret", "app-secret"))
    secrets = ResourceType("v1", "Secret")
    discovered = provider.discover_api_resource(provider.target, secrets)
    page = provider.list_resources(
        ResourceListRequest(provider.target, discovered, 100)
    )
    assert page.failure is None
    assert [item.identity.name for item in page.resources] == ["app-secret"]


def test_forbidden_state_access_names_the_missing_verbs(served) -> None:
    api, url, _ = served
    api.inject("GET", "/leases/piceli-lock-shop", status=403)
    with pytest.raises(StateError) as raised:
        store(url, Clock(), holder="runner-a/1/aa").acquire()
    assert raised.value.code == "state-access-denied"
    assert "server-password-do-not-publish" not in str(raised.value)
