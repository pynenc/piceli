"""0.86.0: a run recorded before ``current_for`` existed reads with it."""

from __future__ import annotations

from typing import Any

from piceli.gitops import history

NEW = "5" * 40
OLD = "f" * 40


def _event(components: dict[str, Any]) -> dict[str, Any]:
    return {
        "started_at": "2026-11-26T10:04:58Z",
        "revision": {"product": NEW, "workspace": "a" * 40},
        "components": components,
    }


def _components(entry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(item["name"]): item for item in entry["components"]}


def test_an_older_record_reads_as_current_for_the_runs_commit() -> None:
    old = {"source": "product", "commit": OLD, "digest": "sha256:" + "d" * 64}
    entry = history._entry("staging", _event({"api": old}), None)
    api = _components(entry)["api"]
    assert api["commit"] == OLD
    assert api["current_for"] == {"product": NEW}


def test_a_recorded_current_for_and_a_component_without_image_are_kept() -> None:
    recorded = {
        "source": "product",
        "commit": OLD,
        "digest": "sha256:" + "d" * 64,
        "current_for": {"product": NEW, "workspace": "a" * 40},
    }
    waiting = {"source": "product", "commit": NEW, "digest": None}
    entry = history._entry("staging", _event({"api": recorded, "web": waiting}), None)
    found = _components(entry)
    assert found["api"]["current_for"] == recorded["current_for"]
    assert "current_for" not in found["web"]
