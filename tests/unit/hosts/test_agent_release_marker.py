"""The host agent's release marker (0.38.0).

Each finished release of a Nix host sends one ``piceli.release`` record to
the host's own OTLP endpoint (``NixHost(report=HostReport(...))``), with
the OpenTelemetry ``deployment.*``, ``service.version`` and ``vcs.*``
attributes, its time window, the build it released and the release that ran
before it. A rollback that ends a release sends one with
``deployment.status = rolled_back``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from piceli.hosts.agent import MARKER_EVENT, release_marker
from piceli.hosts.model import HostReport

from .test_agent import (
    OLD,
    Collector,
    FakeHost,
    Killed,
    commit_of,
    host,
    make_agent,
    publish,
)


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    path = tmp_path / "cache"
    path.mkdir()
    return path


@pytest.fixture
def fake(tmp_path: Path) -> FakeHost:
    link = tmp_path / "current-system"
    link.symlink_to(OLD)
    return FakeHost(link)


def _value(value: dict[str, Any]) -> Any:
    if "arrayValue" in value:
        return [_value(v) for v in value["arrayValue"]["values"]]
    if "intValue" in value:
        return int(value["intValue"])
    return next(iter(value.values()))


def markers(collector: Collector) -> list[dict[str, Any]]:
    found = []
    for post in collector.posts:
        for resource in post["resourceLogs"]:
            for scope in resource["scopeLogs"]:
                for record in scope["logRecords"]:
                    if record.get("eventName") != MARKER_EVENT:
                        continue
                    found.append(
                        {a["key"]: _value(a["value"]) for a in record["attributes"]}
                    )
    return found


def reported(cache_dir: Path) -> Any:
    return host(cache_dir, report=HostReport("https://otel.example.net:4318"))


def test_each_release_sends_one_marker_naming_the_previous(
    tmp_path: Path, cache_dir: Path, fake: FakeHost
) -> None:
    up = Collector(up=True)
    agent = lambda: make_agent(tmp_path, reported(cache_dir), fake, opener=up)  # noqa: E731
    publish(cache_dir, commit_of(1), tag="v1.0.0")
    agent().run_once()
    agent().run_once()  # nothing new: no marker
    publish(cache_dir, commit_of(2), tag="v1.1.0")
    agent().run_once()
    first, second = markers(up)
    assert first["deployment.environment.name"] == "web-1"
    assert first["deployment.status"] == "succeeded"
    assert first["deployment.id"].startswith("web-1/")
    assert first["service.version"] == "v1.0.0"
    assert first["vcs.ref.head.revision"] == commit_of(1)
    assert first["piceli.release.components"] == ["web-1"]
    assert first["piceli.release.started_at"] <= first["piceli.release.finished_at"]
    assert isinstance(first["piceli.release.seconds"], float)
    assert "piceli.release.previous.id" not in first  # the first one
    assert second["service.version"] == "v1.1.0"
    assert second["piceli.release.previous.id"] == first["deployment.id"]
    assert second["piceli.release.previous.version"] == "v1.0.0"
    assert second["piceli.release.previous.revision"] == commit_of(1)
    assert second["piceli.release.previous.finished_at"]
    assert second["event.name"] == MARKER_EVENT
    resource = up.posts[-1]["resourceLogs"][0]["resource"]["attributes"]
    assert {
        "key": "deployment.environment.name",
        "value": {"stringValue": "web-1"},
    } in resource


def test_a_failed_check_sends_a_rolled_back_marker(
    tmp_path: Path, cache_dir: Path, fake: FakeHost
) -> None:
    up = Collector(up=True)
    publish(cache_dir, commit_of(1), tag="v1.0.0")
    make_agent(tmp_path, reported(cache_dir), fake, opener=up).run_once()
    publish(cache_dir, commit_of(2), tag="v1.1.0")
    fake.unit_active = False
    make_agent(tmp_path, reported(cache_dir), fake, opener=up).run_once()
    first, rolled = markers(up)
    assert rolled["deployment.status"] == "rolled_back"
    assert rolled["service.version"] == "v1.1.0"
    assert rolled["piceli.reason"] == "host-check-failed"
    assert rolled["piceli.release.previous.id"] == first["deployment.id"]


def test_a_refused_build_sends_no_marker(
    tmp_path: Path, cache_dir: Path, fake: FakeHost
) -> None:
    from piceli.dev import nix

    up = Collector(up=True)
    publish(cache_dir, commit_of(1), tag="v1.0.0", secret=nix.generate_key("x-1"))
    make_agent(tmp_path, reported(cache_dir), fake, opener=up).run_once()
    assert markers(up) == []


def test_host_rollback_and_an_interrupted_run_send_rolled_back_markers(
    tmp_path: Path, cache_dir: Path, fake: FakeHost
) -> None:
    up = Collector(up=True)
    agent = lambda: make_agent(tmp_path, reported(cache_dir), fake, opener=up)  # noqa: E731
    publish(cache_dir, commit_of(1), tag="v1.0.0")
    agent().run_once()
    publish(cache_dir, commit_of(2), tag="v1.1.0")
    agent().run_once()
    assert agent().control("rollback")["state"] == "rolled-back"
    back = markers(up)[-1]
    assert back["deployment.status"] == "rolled_back"
    assert back["piceli.trigger"] == "rollback"
    assert back["service.version"] == "v1.0.0"
    assert back["piceli.release.previous.version"] == "v1.1.0"
    agent().control("unpin")
    publish(cache_dir, commit_of(3), tag="v1.2.0")
    fake.kill = "is-active"
    with pytest.raises(Killed):
        agent().run_once()
    fake.kill = None
    agent().run_once()
    interrupted = markers(up)[-1]
    assert interrupted["deployment.status"] == "rolled_back"
    assert interrupted["piceli.reason"] == "host-deploy-interrupted"
    assert interrupted["service.version"] == "v1.2.0"


def test_the_marker_attributes_are_fixed() -> None:
    found = release_marker(
        environment="main",
        deployment_id="main/20261030T100000Z",
        status="succeeded",
        version="v2.0.0",
        revision="a" * 40,
        started_at="2026-10-30T10:00:00Z",
        finished_at="2026-10-30T10:04:30Z",
        components=["web", "worker", "web"],
        previous={"id": "main/1", "version": "v1.9.0", "revision": "b" * 40},
        repository_url="https://git.example.org/shop.git",
        release_id="r-2",
        run_id="20261030T100000Z-1",
    )
    assert found == {
        "deployment.environment.name": "main",
        "deployment.id": "main/20261030T100000Z",
        "deployment.status": "succeeded",
        "service.version": "v2.0.0",
        "vcs.ref.head.revision": "a" * 40,
        "vcs.repository.url.full": "https://git.example.org/shop.git",
        "piceli.release.id": "r-2",
        "piceli.run.id": "20261030T100000Z-1",
        "piceli.release.components": ["web", "worker"],
        "piceli.release.started_at": "2026-10-30T10:00:00Z",
        "piceli.release.finished_at": "2026-10-30T10:04:30Z",
        "piceli.release.seconds": 270.0,
        "piceli.release.previous.id": "main/1",
        "piceli.release.previous.version": "v1.9.0",
        "piceli.release.previous.revision": "b" * 40,
    }
    with pytest.raises(ValueError):
        release_marker(
            environment="main", deployment_id="x", status="done", version=None,
            revision=None, started_at=None, finished_at=None,
        )  # fmt: skip
