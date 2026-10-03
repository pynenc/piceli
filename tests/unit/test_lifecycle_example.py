"""The k3s lifecycle acceptance's inputs stay valid without a cluster.

``tests/acceptance_k3s/lifecycle.py`` runs ``examples/lifecycle`` and the
commands of ``lifecycle_commands.toml`` on a disposable k3s cluster (opt-in);
here: the composition imports, its build spec loads, and every command line
renders with the note's placeholders.
"""

from __future__ import annotations

import re
import string
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "lifecycle"
sys.path.insert(0, str(ROOT / "tests" / "acceptance_k3s"))

from lifecycle_support import Commands  # noqa: E402


def test_the_composition_imports_with_its_environments() -> None:
    from piceli.infra.composition import load_composition

    composition = load_composition("infra.py", EXAMPLE / "infra")
    assert [item.name for item in composition.environments] == [
        "main",
        "rc",
        "branches",
    ]
    assert {source.key for source in composition.sources} == {"infra", "web", "store"}
    assert composition.cluster is not None
    assert composition.cluster.controller is not None
    assert composition.cluster.controller.delete_volumes


def test_the_build_spec_lists_contexts_per_image() -> None:
    import tomllib

    spec = tomllib.loads((EXAMPLE / "infra" / "host-build.toml").read_text())
    contexts = {image["name"]: image["contexts"] for image in spec["output"]["image"]}
    assert contexts == {
        "web": ["web", "infra"],
        "store": ["store"],
        "watcher": ["infra"],
    }


@pytest.fixture(scope="module")
def commands() -> Commands:
    return Commands.load(ROOT / "tests" / "acceptance_k3s" / "lifecycle_commands.toml")


def test_every_group_names_known_steps(commands: Commands) -> None:
    for group, steps in commands.groups.items():
        assert steps, group
        assert set(steps) <= set(commands.steps), group


def test_every_command_renders_with_the_notes_placeholders(commands: Commands) -> None:
    for step in commands.steps:
        fields = {
            name
            for _, name, _, _ in string.Formatter().parse(commands.steps[step]["run"])
            if name
        }
        assert fields <= set(commands.note), (step, fields - set(commands.note))
        line = commands.line(step, commands.note)
        assert "{" not in line and "\n" not in line


def test_the_note_names_no_consumer(commands: Commands) -> None:
    text = commands.render_note() + "".join(
        path.read_text()
        for path in EXAMPLE.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    )
    assert not re.search(r"\bih[-_]|k-lab|haiku", text, re.IGNORECASE)
