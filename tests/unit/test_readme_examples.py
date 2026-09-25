"""The README quick start is the code the acceptance test runs.

``scripts/readme_examples.py check`` (also a pre-commit hook) compares every
``<!-- readme-example: PATH -->`` block of ``README.md`` with its file;
``tests/acceptance/test_readme_quickstart.py`` runs those files.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[2]


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "readme_examples", ROOT / "scripts" / "readme_examples.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_readme_blocks_match_their_files() -> None:
    script = _script()
    assert script.check() == []
    paths = [path.relative_to(ROOT).as_posix() for path, _ in script.marked_examples()]
    assert paths == ["examples/readme/app.py", "examples/readme/commands.sh"]


def test_a_drifted_block_is_reported_and_sync_repairs_it(tmp_path: Path) -> None:
    script = _script()
    readme = tmp_path / "README.md"
    readme.write_text(
        "Intro\n\n<!-- readme-example: examples/readme/app.py -->\n\n"
        "```python\nprint('stale')\n```\n\nOutro\n"
    )
    assert script.check(readme) == [
        "README.md: block differs from examples/readme/app.py; run `sync`"
    ]
    script.sync(readme)
    assert script.check(readme) == []
    text = readme.read_text()
    assert text.startswith("Intro\n") and text.endswith("```\n\nOutro\n")
    assert (tmp_path / "EMPTY.md").write_text("no markers\n")
    assert script.check(tmp_path / "EMPTY.md") == [
        "EMPTY.md: no <!-- readme-example: ... --> markers"
    ]


def test_getting_started_includes_the_same_app() -> None:
    page = (ROOT / "docs" / "getting_started" / "index.md").read_text()
    assert "```{literalinclude} ../../examples/readme/app.py" in page
