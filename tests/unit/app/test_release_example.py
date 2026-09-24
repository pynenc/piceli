"""The typed ``examples/release`` renders byte-identically to the former dict example."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from tests.unit.app import dict_release_composition
from tests.unit.app.conftest import canonical, make_context

EXAMPLE = (
    Path(__file__).resolve().parents[3] / "examples" / "release" / "composition.py"
)


def _example() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_typed_release_example", EXAMPLE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_typed_example_is_at_most_60_lines():
    source = EXAMPLE.read_text()
    assert len(source.splitlines()) <= 60
    assert "apiVersion" not in source and "from_manifest" not in source


def test_typed_example_renders_byte_identical_intents(release_ctx):
    typed = _example().build(release_ctx)
    legacy = dict_release_composition.build(release_ctx)
    assert canonical(typed) == canonical(legacy)
    for a, b in zip(
        [r for c in typed.components for r in c.resources],
        [r for c in legacy.components for r in c.resources],
        strict=True,
    ):
        assert a == b  # same ref, manifest JSON bytes, dependencies and bindings


def test_typed_example_matches_with_defaults():
    ctx = make_context(
        images={"web": "docker.io/library/nginx"},
        secrets=["api-token", "web-tls.crt", "web-tls.key"],
    )
    assert canonical(_example().build(ctx)) == canonical(
        dict_release_composition.build(ctx)
    )
