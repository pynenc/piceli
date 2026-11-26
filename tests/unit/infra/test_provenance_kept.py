"""0.86.0: an image recorded under its change key keeps its build's provenance."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from piceli.gitops.state import write_json
from piceli.infra.builders import BuiltImage
from piceli.infra.controller import CompositionController as Controller

DIGEST = "sha256:" + "a" * 64
KEY = "sha256:" + "b" * 64


def _record(commit: str, host: str = "builder-1") -> dict[str, Any]:
    return {
        "builder": "host",
        "host": host,
        "commits": {"product": commit},
        "finished_at": "2026-11-25T23:22:39Z",
    }


def _rig(tmp_path: Path, rebuild: dict[str, list[str]] | None = None) -> Any:
    rig = SimpleNamespace(state={"rebuild": rebuild or {}}, state_dir=tmp_path)
    rig._cache_path = lambda component, key: Controller._cache_path(rig, component, key)
    return rig


def _remember(rig: Any, commit: str, host: str = "builder-1") -> dict[str, Any]:
    image = BuiltImage("r/x@" + DIGEST, DIGEST, {"provenance": _record(commit, host)})
    Controller._keep_provenance(rig, "x", KEY, image)
    write_json(rig._cache_path("x", KEY), image.to_dict())
    return image.extra["provenance"]


def test_the_same_image_for_a_newer_commit_is_no_new_build(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    assert _remember(rig, "f2b91c8")["commits"] == {"product": "f2b91c8"}
    assert _remember(rig, "5980aa7")["commits"] == {"product": "f2b91c8"}


def test_a_rebuild_records_its_own_provenance(tmp_path: Path) -> None:
    _remember(_rig(tmp_path), "f2b91c8")
    again = _remember(_rig(tmp_path, {"main": ["x"]}), "5980aa7", host="builder-2")
    assert again["commits"] == {"product": "5980aa7"} and again["host"] == "builder-2"


def test_another_image_under_the_key_is_recorded_as_given(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    _remember(rig, "f2b91c8")
    other = BuiltImage("r/x@sha256:" + "c" * 64, "sha256:" + "c" * 64)
    Controller._keep_provenance(rig, "x", KEY, other)
    assert "provenance" not in other.extra
