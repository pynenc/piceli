"""``piceli.gitops-status.v1`` and ``piceli.gitops-history.v1`` change only additively.

The promise (``docs/gitops.md``, "Compatibility of the documents"): within
v1 a release adds fields and never removes or renames one. This test builds
the documents the controllers publish (a composition controller with real
run journals, a one-repository controller, a history with every kind of
entry) and checks that every field path recorded in
``gitops_v1_fields.json`` is still there. A new field needs no change here;
``PICELI_UPDATE_SHAPES=1`` adds the new paths to the file (it never drops
one). A removed or renamed field fails.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from piceli.gitops import history
from piceli.gitops.state import sync_request
from tests.gitops_history_fixture import sample_history
from tests.unit.infra.test_controller import world
from tests.unit.infra.test_controller_history import _published, journaled
from tests.unit.test_gitops_controller import FakePorts, Repo, make

__all__ = ["journaled", "world"]

GOLDEN = Path(__file__).with_name("gitops_v1_fields.json")
#: Maps keyed by names (environments, components, stages, sources, …).
NAMED = {
    "envs",
    "components",
    "sources",
    "stages",
    "builds",
    "images",
    "refs",
    "revision",
    "deployed_revision",
    "counts",
    "clusters",
}


def paths(value: Any, prefix: str = "") -> set[str]:
    """Every field path of ``value``: ``a.b``, ``a[]`` for list items, ``*``
    for a name of a map keyed by names."""
    found: set[str] = set()
    if isinstance(value, dict):
        named = prefix.rsplit(".", 1)[-1] in NAMED
        for key, item in value.items():
            path = f"{prefix}.{'*' if named else key}" if prefix else str(key)
            found.add(path)
            found |= paths(item, path)
    elif isinstance(value, list):
        for item in value:
            found |= paths(item, f"{prefix}[]")
    return found


def _union(documents: Iterable[Any]) -> list[str]:
    found: set[str] = set()
    for document in documents:
        found |= paths(document)
    return sorted(found)


def _composition_status(world: dict[str, Any]) -> tuple[Any, Any]:
    controller, channel = world["controller"], world["channel"]
    controller.poll_once()
    world["shop"].commit({"web/index.html": "<h1>login</h1>\n"}, branch="wp-login")
    controller.poll_once()
    channel.add_request(*sync_request("main"))
    controller.poll_once()
    return channel.read_status(), _published(channel)


def _repo_status(tmp_path: Path) -> Any:
    (tmp_path / "single").mkdir()
    repo = Repo(tmp_path / "single")
    controller, channel, _ = make(tmp_path / "single", repo, FakePorts())
    repo.push_branch("wp-1", "one")
    controller.poll_once()
    return channel.read_status()


def _lifecycle_history() -> dict[str, Any]:
    events = [
        history.lifecycle(
            "stop",
            at="2026-10-04T10:00:00Z",
            state="stopped",
            by="requested",
            via="cli",
            namespace="shop-rc",
        ),
        history.lifecycle(
            "start", at="2026-10-04T10:05:00Z", state="started", by="requested"
        ),
        history.lifecycle("teardown", at="2026-10-04T10:09:00Z", state="removed"),
    ]
    return {"envs": {"rc": {"runs": history.environment_runs("rc", events, [])}}}


def _documents(journaled: dict[str, Any], tmp_path: Path) -> dict[str, list[str]]:
    status, published = _composition_status(journaled)
    return {
        "status": _union([status, _repo_status(tmp_path)]),
        "history": _union(
            [published, sample_history(tmp_path / "sample"), _lifecycle_history()]
        ),
    }


def test_no_documented_field_of_the_v1_documents_disappears(
    journaled: dict[str, Any], tmp_path: Path
) -> None:
    found = _documents(journaled, tmp_path)
    golden = json.loads(GOLDEN.read_text())
    if os.environ.get("PICELI_UPDATE_SHAPES") == "1":
        merged = {
            key: sorted(set(golden.get(key, [])) | set(found[key])) for key in found
        }
        GOLDEN.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n")
        golden = merged
    for key in ("status", "history"):
        missing = sorted(set(golden[key]) - set(found[key]))
        assert not missing, (
            f"piceli.gitops-{key}.v1 lost fields (the schema changes only "
            f"additively within v1): {missing}"
        )


def test_the_0160_fields_are_part_of_the_promise() -> None:
    golden = json.loads(GOLDEN.read_text())
    for path in (
        "controller.heartbeat_at",
        "controller.heartbeat_seconds",
        "controller.last_poll",
        "envs.*.state",
        "envs.*.updated_at",
    ):
        assert path in golden["status"], path
    for path in (
        "envs.*.runs[].kind",
        "envs.*.runs[].stages.*.started_at",
        "envs.*.runs[].stages.*.finished_at",
        "envs.*.runs[].builds.*.started_at",
        "envs.*.runs[].builds.*.finished_at",
        "envs.*.runs[].approved_by.at",
        "envs.*.runs[].stop.at",
        "envs.*.runs[].at",
        "envs.*.runs[].by",
        "envs.*.runs[].via",
        "envs.*.runs[].run_state",
        "envs.*.runs[].finished_at",
    ):
        assert path in golden["history"], path


def test_paths_name_maps_by_star() -> None:
    assert paths({"envs": {"main": {"state": "x"}}, "a": [{"b": 1}]}) == {
        "envs",
        "envs.*",
        "envs.*.state",
        "a",
        "a[].b",
    }
