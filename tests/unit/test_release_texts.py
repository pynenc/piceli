"""Texts that name the release follow it (an agent installs what they say)."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
MINOR = ".".join(VERSION.split(".")[:2])


def test_every_install_pin_in_the_skill_is_the_current_release() -> None:
    major, minor = (int(part) for part in MINOR.split("."))
    wanted = f'"piceli>={MINOR},<{major}.{minor + 1}"'
    pins = {
        f"{path.relative_to(ROOT)}: {pin}"
        for path in (ROOT / "skills").rglob("*")
        if path.is_file() and path.suffix in {".md", ".py"}
        for pin in re.findall(r'"piceli>=[0-9.]+,<[0-9.]+"', path.read_text())
        if pin != wanted
    }
    assert not pins, f"install pins other than {wanted}: {sorted(pins)}"


def test_the_readme_says_what_is_new_in_this_minor() -> None:
    headings = re.findall(
        r"^## What's new in ([0-9.]+)", (ROOT / "README.md").read_text(), re.M
    )
    assert headings == [MINOR], headings


def test_cryptography_is_a_dependency_and_the_crypto_extra_still_installs() -> None:
    """Key generation (secrets setup, nix-cache, age) is core: no extra needed."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert any(d.startswith("cryptography>=") for d in project["dependencies"])
    assert "crypto" in project["optional-dependencies"]  # piceli[crypto] still works


def test_no_error_code_asks_for_the_crypto_extra() -> None:
    from piceli.errors import ERRORS

    assert not [code for code, e in ERRORS.items() if "piceli[crypto]" in e.fix]
