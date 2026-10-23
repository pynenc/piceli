"""Impact selection: the cargo packages a change can affect.

:func:`select` is pure: it takes the changed paths and the output of
``cargo metadata --format-version 1 --no-deps`` (read in the merged
checkout) and returns the selected package names, or ``None`` for every
package. A package is selected when one of its files changed, or when it
depends (normal or build dependencies, transitively) on a selected package;
a member with a dev-dependency on a selected package is selected too (its
tests link it). A changed path matching a root glob selects everything.

Importing this module is side-effect free.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any


def select(
    files: Iterable[str],
    metadata: Mapping[str, Any],
    roots: Sequence[str],
    why: Callable[[str], None] = lambda _line: None,
) -> list[str] | None:
    """The selected workspace package names, sorted; ``None`` for all.

    ``why`` receives one line per reason (for the step's log).
    """
    root = Path(metadata["workspace_root"])
    members = set(metadata.get("workspace_members") or ())
    packages = {p["id"]: p for p in metadata["packages"] if p["id"] in members}
    by_dir = sorted(
        (
            (_relative(Path(p["manifest_path"]).parent, root), p["name"])
            for p in packages.values()
        ),
        key=lambda item: len(item[0].parts),
        reverse=True,
    )
    seeds: set[str] = set()
    for path in sorted(set(files)):
        if any(fnmatch.fnmatchcase(path, pattern) for pattern in roots):
            why(f"impact: every package: {path} is a root file")
            return None
        owner = next(
            (
                name
                for directory, name in by_dir
                if PurePosixPath(path).is_relative_to(directory)
            ),
            None,
        )
        if owner is not None and owner not in seeds:
            why(f"impact: {owner}: {path} changed")
            seeds.add(owner)
    names = {p["name"] for p in packages.values()}
    users: dict[str, set[str]] = {}
    dev_users: dict[str, set[str]] = {}
    for package in packages.values():
        for dependency in package.get("dependencies") or ():
            target = dependency.get("name")
            if target not in names or dependency.get("path") is None:
                continue
            kind = dependency.get("kind")
            table = dev_users if kind == "dev" else users
            table.setdefault(target, set()).add(package["name"])
    selected, stack = set(seeds), list(seeds)
    while stack:
        current = stack.pop()
        for user in sorted(users.get(current, ())):
            if user not in selected:
                why(f"impact: {user}: depends on {current}")
                selected.add(user)
                stack.append(user)
    for dependency in sorted(selected):
        for user in sorted(dev_users.get(dependency, ())):
            if user not in selected:
                why(f"impact: {user}: its tests use {dependency}")
                selected.add(user)
    return sorted(selected)


def _relative(path: Path, root: Path) -> PurePosixPath:
    try:
        return PurePosixPath(path.resolve().relative_to(root.resolve()).as_posix())
    except ValueError:
        return PurePosixPath(path.as_posix())


def cargo_arguments(selection: list[str] | None) -> list[str]:
    """``--workspace`` for every package, else ``-p NAME`` for each."""
    if selection is None:
        return ["--workspace"]
    return [arg for name in selection for arg in ("-p", name)]
