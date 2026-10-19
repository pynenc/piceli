"""The toolchains a host build spec pins (``[build.toolchains]``, 0.22.0).

A ``host-build.toml`` may declare, in ``[build]``::

    toolchains = { rust = "app/rust-toolchain.toml", node = "app/.nvmrc",
                   wasm-bindgen = "app/Cargo.lock", binaryen = "123" }

Every key is optional. A value is a path relative to the stage root (the
first part names a context's staging directory, the rest a file that
context includes) or, for ``node``, ``wasm-bindgen`` and ``binaryen``, a
version. Parsing and installing are shared with development runs
(:mod:`piceli.dev.toolchains`); this module reads the pins from the scanned
contexts at plan time and maps failures to fixed error codes:

* ``host-toolchain-invalid``: a pin that cannot be read;
* ``host-toolchain-checksum``: a download that does not match its published
  checksum;
* ``host-toolchain-failed``: an install that failed.

Importing this module runs nothing.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from piceli.artifacts.build_spec import BuildSpecError, _fail
from piceli.dev import toolchains

#: Variables a spec that pins Rust may not set in ``build.env``.
RUST_ENV = frozenset({"RUSTUP_HOME", "RUSTUP_TOOLCHAIN"})


def _error(error: toolchains.ToolchainError) -> BuildSpecError:
    if error.kind == "checksum":
        return BuildSpecError("host-toolchain-checksum", str(error))
    if error.kind == "failed":
        return BuildSpecError("host-toolchain-failed", str(error))
    return BuildSpecError("host-toolchain-invalid", str(error))


def parse(value: Any) -> tuple[tuple[str, str], ...]:
    """``build.toolchains`` as sorted ``(tool, path or version)`` pairs.

    :raises BuildSpecError: ``invalid-spec``.
    """
    try:
        found = toolchains.check_declared(value, "build.toolchains")
    except toolchains.ToolchainError as error:
        raise _fail(str(error)) from None
    return tuple(found.items())


def locate(contexts: Any, path: str) -> list[tuple[str, str]]:
    """The ``(context name, path inside it)`` a stage path may be, longest
    staging directory first."""
    found = []
    for context in contexts:
        into = str(context.target or context.name).strip("/")
        if path.startswith(into + "/"):
            found.append((len(into), context.name, path[len(into) + 1 :]))
    return [(name, inner) for _, name, inner in sorted(found, reverse=True)]


def resolve(
    spec: Any, manifests: Mapping[str, Any], roots: Mapping[str, Path]
) -> dict[str, dict[str, Any]]:
    """Each pinned tool's version, read from the scanned contexts (plan time).

    :raises BuildSpecError: ``host-toolchain-invalid`` (no such file in a
        context, or an unreadable pin), ``context-changed``.
    """
    if not spec.toolchains:
        return {}

    def read(path: str) -> bytes:
        for name, inner in locate(spec.contexts, path):
            entry = next(
                (item for item in manifests[name].files if item.path == inner), None
            )
            if entry is None:
                continue
            try:
                data = roots[name].joinpath(*PurePosixPath(inner).parts).read_bytes()
            except OSError:
                data = b""
            if "sha256:" + hashlib.sha256(data).hexdigest() != entry.sha256:
                raise BuildSpecError(
                    "context-changed", "context file changed while planning"
                )
            return data
        raise toolchains.ToolchainError(
            "invalid",
            f"{path}: not a file of a declared context (paths are relative to "
            "the stage root: <context>/<file>, and the context must include it)",
        )

    try:
        return toolchains.resolve(dict(spec.toolchains), read)
    except toolchains.ToolchainError as error:
        raise _error(error) from None


def key_files(spec: Any, files: Mapping[str, Any]) -> dict[str, str]:
    """For a composition's change key: each pin's Git object id (a file) or
    its version (a literal). ``files`` is ``{context: {"files": [[path,
    mode, oid], ...]}}``."""
    found: dict[str, str] = {}
    for tool, value in spec.toolchains:
        if toolchains.is_literal(tool, value):
            found[tool] = value
            continue
        for name, inner in locate(spec.contexts, value):
            listed = (files.get(name) or {}).get("files") or ()
            oid = next((item[2] for item in listed if item[0] == inner), None)
            if oid is not None:
                found[tool] = oid
                break
        else:
            found[tool] = "missing:" + value
    return found


def provision(
    cache: Path,
    pins: Mapping[str, Mapping[str, Any]],
    environment: dict[str, str],
    say: Callable[[str], None],
) -> dict[str, dict[str, Any]]:
    """Install the pinned tools once into ``<cache>/toolchains`` and select
    them in ``environment`` (``RUSTUP_HOME``, ``RUSTUP_TOOLCHAIN``, the tools'
    ``bin`` directories first on ``PATH``); returns the report per tool.

    ``rustup`` and ``cargo`` are looked up on the commands' ``PATH``, then on
    the caller's.

    :raises BuildSpecError: ``host-toolchain-checksum``, ``host-toolchain-failed``.
    """
    if not pins:
        return {}
    search = os.pathsep.join(
        item for item in (environment.get("PATH"), os.environ.get("PATH")) if item
    )
    try:
        selected, paths, report = toolchains.provision(
            cache,
            pins,
            lambda line: say(f"[piceli] {line}"),
            env={**environment, "PATH": search},
        )
    except toolchains.ToolchainError as error:
        raise _error(error) from None
    environment.update(selected)
    environment["PATH"] = os.pathsep.join([*paths, environment.get("PATH") or ""])
    for tool, item in report.items():
        say(
            f"[piceli] toolchain {tool} {toolchains.versions({tool: item})[tool]}: "
            + (f"installed in {item['seconds']}s" if item["installed"] else "cached")
        )
    return report


def provenance(
    pins: Mapping[str, Mapping[str, Any]], report: Mapping[str, Mapping[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The pins for the provenance's parameters and the downloads as
    resolved dependencies (URL and sha256)."""
    dependencies = []
    for tool, item in sorted(report.items()):
        if item.get("url") and item.get("sha256"):
            dependencies.append(
                {
                    "name": f"toolchain:{tool}",
                    "uri": item["url"],
                    "digest": {"sha256": item["sha256"]},
                }
            )
    return {tool: dict(pin) for tool, pin in sorted(pins.items())}, dependencies
