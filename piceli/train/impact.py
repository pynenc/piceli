"""Impact selection: the cargo packages a change can affect.

:func:`select` is pure: it takes the changed paths and the output of
``cargo metadata --format-version 1 --no-deps`` (read in the merged
checkout) and returns the selected package names, or ``None`` for every
package. A package is selected when one of its files changed, or when it
depends (normal or build dependencies, transitively) on a selected package;
a member with a dev-dependency on a selected package is selected too (its
tests link it). A changed path matching a root glob selects everything.

With ``dev_targets=True`` a member reached only through a dev-dependency
is narrowed to its integration test targets (``kind == ["test"]``) whose
source names a selected library (``\bname\b``, the library's name in
code): those are returned as ``"member:target"``. When any other Rust file
of the member names it (its unit tests, a module its tests share), the
whole member stays selected: a doubt tests more.

Importing this module is side-effect free.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any


def select(
    files: Iterable[str],
    metadata: Mapping[str, Any],
    roots: Sequence[str],
    why: Callable[[str], None] = lambda _line: None,
    *,
    dev_targets: bool = False,
) -> list[str] | None:
    """The selected workspace package names, sorted; ``None`` for all.

    ``why`` receives one line per reason (for the step's log). With
    ``dev_targets`` an entry may be ``"package:test-target"``.
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
    by_name = {p["name"]: p for p in packages.values()}
    dev_only: dict[str, set[str]] = {}
    for dependency in sorted(selected):
        for user in sorted(dev_users.get(dependency, ())):
            if user not in selected:
                dev_only.setdefault(user, set()).add(dependency)
    chosen = set(selected)
    for user, used in sorted(dev_only.items()):
        if not dev_targets:
            for dependency in sorted(used):
                why(f"impact: {user}: its tests use {dependency}")
            chosen.add(user)
            continue
        libraries = {library_name(by_name[name]) for name in used}
        tests = tests_naming(by_name[user], libraries)
        if tests is None:
            why(
                f"impact: {user}: its unit tests or shared test code use "
                + ", ".join(sorted(libraries))
            )
            chosen.add(user)
            continue
        for test in tests:
            why(
                f"impact: {user} --test {test}: uses {', '.join(sorted(libraries))} "
                "(dev-dependency)"
            )
            chosen.add(f"{user}:{test}")
    return sorted(chosen)


_LIBRARY_KINDS = {"lib", "rlib", "dylib", "proc-macro", "cdylib", "staticlib"}


def library_name(package: Mapping[str, Any]) -> str:
    """The name code uses for a package's library (``my_core``)."""
    for target in package.get("targets") or ():
        if _LIBRARY_KINDS & set(target.get("kind") or ()):
            return str(target["name"]).replace("-", "_")
    return str(package["name"]).replace("-", "_")


def tests_naming(package: Mapping[str, Any], names: set[str]) -> list[str] | None:
    """The integration test targets of ``package`` whose source names one of
    ``names``; ``None`` when any other Rust file of the package does."""
    if not names:
        return []
    pattern = re.compile(r"\b(" + "|".join(sorted(map(re.escape, names))) + r")\b")
    root = Path(package["manifest_path"]).parent
    tests = {
        Path(target["src_path"]).resolve(): str(target["name"])
        for target in package.get("targets") or ()
        if list(target.get("kind") or ()) == ["test"]
    }
    found: list[str] = []
    for path in sorted(root.rglob("*.rs")):
        if "target" in path.relative_to(root).parts:
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError:
            return None
        if not pattern.search(text):
            continue
        test = tests.get(path.resolve())
        if test is None:
            return None
        found.append(test)
    return sorted(found)


def _relative(path: Path, root: Path) -> PurePosixPath:
    try:
        return PurePosixPath(path.resolve().relative_to(root.resolve()).as_posix())
    except ValueError:
        return PurePosixPath(path.as_posix())


def packages_of(selection: list[str] | None) -> list[str] | None:
    """The packages of a selection (``"member:test"`` counts as its member)."""
    if selection is None:
        return None
    return sorted({item.partition(":")[0] for item in selection})


def cargo_arguments(selection: list[str] | None) -> list[str]:
    """``--workspace`` for every package, else ``-p NAME`` for each package
    (a ``"member:test"`` entry checks its whole member)."""
    if selection is None:
        return ["--workspace"]
    return [arg for name in packages_of(selection) or () for arg in ("-p", name)]


def cargo_test_arguments(selection: list[str] | None) -> list[list[str]]:
    """The cargo arguments of each ``cargo test`` a selection needs: one for
    the whole packages, then one per member narrowed to some test targets
    (``--test`` applies to every ``-p`` of one invocation)."""
    if selection is None:
        return [["--workspace"]]
    whole = sorted(item for item in selection if ":" not in item)
    narrowed: dict[str, list[str]] = {}
    for item in selection:
        member, sep, test = item.partition(":")
        if sep and member not in whole:
            narrowed.setdefault(member, []).append(test)
    found = [[arg for name in whole for arg in ("-p", name)]] if whole else []
    for member, tests in sorted(narrowed.items()):
        found.append(["-p", member, *(a for t in sorted(tests) for a in ("--test", t))])
    return found
