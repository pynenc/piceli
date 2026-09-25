"""Human output stays readable: no cut-off values, no runaway lines (B9, B11, B13)."""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

from piceli.k8s.cli import deploy_pipeline
from piceli.k8s.ops.field_diff import CHANGE_LINE_WIDTH, describe_change, short_value
from piceli.k8s.release_runner import ReleaseRunner

REDIS = (
    "docker.io/library/redis:7.2.3"
    "@sha256:a7cee7c8178ff9b5297cb109e6240f5072cdaaafd775ce6b586c3c704b06458e"
)
LOCAL = (
    "127.0.0.1:5000/shop/rust-hello"
    "@sha256:9b2fbdad15e1fcb1c9ea1d56178cf7611186a052636746e54e41cbd05360302a"
)


def test_short_values_keep_the_digest_tail_and_the_closing_quote() -> None:
    assert short_value("short") == '"short"'
    value = short_value(REDIS)
    assert value == '"docker.io/library/redis:7.2.3@sha256:a7cee7c8178f…06458e"'
    assert value.endswith('"') and len(value) <= 60
    long = short_value("x" * 80 + "TAIL-END")
    assert long.endswith('TAIL-END"') and "…" in long and len(long) <= 60
    listed = short_value(list(range(40)))
    assert listed.startswith("[0, ") and listed.endswith("39]")
    assert "..." not in short_value(REDIS)


def test_long_changes_put_values_on_their_own_lines() -> None:
    path = "/spec/template/spec/containers/0/image"
    text = describe_change(
        {"op": "replace", "path": path, "before": REDIS, "after": LOCAL},
        indent=" " * 12,
    )
    lines = text.splitlines()
    assert lines[0] == f"~ {path}:"
    assert lines[1].strip().startswith('"docker.io/library/redis:7.2.3@sha256:')
    assert lines[2].startswith(" " * 12 + " -> ")
    assert all(len(" " * 12 + line.lstrip()) <= 120 for line in lines)
    one = describe_change({"op": "add", "path": path, "after": REDIS})
    assert len(one) <= CHANGE_LINE_WIDTH and "\n" not in one
    assert describe_change({"op": "remove", "path": "/n", "before": 1}) == "- /n: 1"


def test_deploy_plan_lines_wrap_at_item_boundaries(
    capsys: pytest.CaptureFixture[str],
) -> None:
    changes = ", ".join(
        [
            "create ConfigMap/registry-config",
            "create PersistentVolumeClaim/registry-storage",
            "create Deployment/registry",
            "create Service/registry",
        ]
    )
    deploy_pipeline._say_wrapped(
        "  deliver  registry shop-registry-76a76adcc468: ", changes
    )
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) >= 2
    assert all(len(line) <= deploy_pipeline.HUMAN_WIDTH for line in lines)
    assert all(line.startswith(" " * 11) for line in lines[1:])
    joined = " ".join(line.strip() for line in lines)
    for item in changes.split(", "):
        assert item in joined
    deploy_pipeline._say_wrapped("  plan     release x (create): ", "no changes")
    assert capsys.readouterr().err == "  plan     release x (create): no changes\n"


def test_release_runner_passes_progress_to_its_executor() -> None:
    seen: list[str] = []
    runner = ReleaseRunner(cast(Any, _Spec()), progress=seen.append)
    assert runner.progress is not None
    runner.progress("applying 1/1: Deployment/web")
    assert seen == ["applying 1/1: Deployment/web"]


class _Spec:
    """Just enough of a ReleaseSpec for the constructor (no files touched)."""

    def __init__(self) -> None:
        from pathlib import Path

        self.state_dir = Path("/nonexistent-piceli-state")


def test_dashboard_clips_images_and_marks_derived_objects() -> None:
    from piceli.k8s.observe_server import _PAGE_HTML

    assert '<table class="fixed">' in _PAGE_HTML
    assert "const shortImage" in _PAGE_HTML
    assert "badge-derived" in _PAGE_HTML
    assert "created by ' + derived" in _PAGE_HTML
    # The unmanaged rows read the observation, not missing top-level fields.
    assert "detail: r.phase ||" not in _PAGE_HTML
    json.dumps(_PAGE_HTML)  # still one plain string
