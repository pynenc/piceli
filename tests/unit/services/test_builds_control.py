"""The Builds page (0.26.0): the records of ``piceli gitops builds`` and the
requests the CLI writes (build now, approve, retry, cancel)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from piceli.services.builds import BuildsControl
from piceli.services.query import QueryError
from tests.unit.services.test_composition_control import Channel, _service

DIGEST = "sha256:" + "d" * 64


def _status() -> dict[str, Any]:
    return {
        "schema": "piceli.gitops-status.v1",
        "controller": {"state": "running", "builds_wanted": []},
        "sources": {},
        "envs": {"main": {"namespace": "shop", "state": "deployed",
                          "revision": {"shop": "a" * 40}}},
        "nix_releases": {"web-1": {
            "state": "approval-required", "tag": "web-v1.1.0", "commit": "b" * 40,
            "trigger": "cli web-v1.1.0", "trigger_kind": "cli", "requested_by": "ana",
            "builder": "mini", "plan_digest": DIGEST, "plan": {"tag": "web-v1.1.0"},
            "requested_at": "2026-10-24T10:00:00Z",
        }},
    }  # fmt: skip


def test_the_page_lists_builds_and_writes_the_cli_requests(tmp_path: Path) -> None:
    channel = Channel(_status())
    composition, _ = _service(tmp_path, channel)
    control = BuildsControl(composition)
    page = control.builds()
    assert page["configured"] and page["targets"] == ["main", "web-1"]
    (item,) = page["builds"]
    assert item["target"] == "web-1" and item["stage"] == "approval-required"
    assert item["trigger"] == {"kind": "cli", "by": "ana", "detail": "cli web-v1.1.0"}
    assert item["plan_digest"] == DIGEST and item["actions"]["approve"]
    assert control.builds(state="published")["builds"] == []
    with pytest.raises(QueryError):
        control.builds(state="nope")

    started = control.start("web-1", "web-v1.0.0")
    assert started["state"] == "requested" and started["ref"] == "web-v1.0.0"
    control.approve("web-1", DIGEST)
    control.retry("web-1")
    control.cancel("main")
    kinds = sorted(body["kind"] for body in channel.requests.values())
    assert kinds == ["approve", "build", "build-cancel", "sync"]
    built = next(b for b in channel.requests.values() if b["kind"] == "build")
    assert built["via"] == "ui" and built["ref"] == "web-v1.0.0"
    with pytest.raises(QueryError) as unknown:
        control.start("api", None)
    assert unknown.value.code == "ui-sync-target-unknown"
    with pytest.raises(QueryError) as bad:
        control.approve("web-1", "sha256:nope")
    assert bad.value.code == "ui-invalid-request"


def test_without_a_controller_the_page_is_empty(tmp_path: Path) -> None:
    composition, _ = _service(tmp_path, Channel(None))
    page = BuildsControl(composition).builds()
    assert page == {"configured": False, "schema": "piceli.builds.v1", "builds": [],
                    "stages": page["stages"]}  # fmt: skip
