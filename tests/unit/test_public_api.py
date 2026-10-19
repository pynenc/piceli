"""The documented API (piceli/public_api.py) covers what the docs teach.

A guide, an example or the agent skill that imports a module outside
``PUBLIC_MODULES`` teaches users an internal module: add it to the list (it
is then documented and its exports checked by the evals) or import from a
public module instead.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

from piceli.public_api import PUBLIC_MODULES

ROOT = Path(__file__).resolve().parents[2]
IMPORT = re.compile(
    r"^\s*(?:from\s+(piceli[\w.]*)\s+import|import\s+(piceli[\w.]*))", re.M
)


def _taught() -> dict[str, str]:
    found: dict[str, str] = {}
    paths = [
        *(ROOT / "docs").rglob("*.md"),
        *(ROOT / "examples").rglob("*.py"),
        *(ROOT / "examples").rglob("*.md"),
        *(ROOT / "skills").rglob("*.md"),
        ROOT / "README.md",
    ]
    for path in paths:
        if {"_build", "apidocs"} & set(path.parts):
            continue
        for first, second in IMPORT.findall(path.read_text(errors="ignore")):
            found.setdefault(first or second, str(path.relative_to(ROOT)))
    return found


def test_every_module_the_docs_import_is_public() -> None:
    missing = {
        name: where for name, where in _taught().items() if name not in PUBLIC_MODULES
    }
    assert not missing, f"internal modules taught by the docs: {missing}"


def test_every_public_module_imports() -> None:
    for name in PUBLIC_MODULES:
        importlib.import_module(name)
    assert list(PUBLIC_MODULES) == sorted(PUBLIC_MODULES)


def test_every_exported_name_resolves() -> None:
    """Also the facades' lazy names (piceli/_lazy.py)."""
    for name in PUBLIC_MODULES:
        module = importlib.import_module(name)
        for export in getattr(module, "__all__", ()):
            assert getattr(module, export) is not None, f"{name}.{export}"
            assert export in dir(module), f"{name}.{export} missing from dir()"
