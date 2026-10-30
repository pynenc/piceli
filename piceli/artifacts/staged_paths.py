"""Refuse a host-build command that names a file its context does not stage (0.41.0).

Contexts are staged as ``<stage>/<into or name>/`` with exactly the files
their ``include`` globs select (minus ``exclude``). A command such as
``["bash", "app/scripts/build.sh"]`` whose script is not in ``include``
fails only on the build host, with ``No such file or directory``. This
check refuses it when the spec is loaded (``host-build-path-not-staged``).

It is conservative: a false refusal is worse than a miss. It looks only at

* the arguments of a command (relative paths with a ``/``, no shell
  syntax, no placeholder), except after an output option (``-o``,
  ``--out-dir`` …) and in commands that write files (``mkdir``, ``cp`` …);
* in an ``sh -c`` / ``bash -c`` script, only the program each simple
  command runs, and the script a shell, ``source`` or ``.`` runs.

A path is checked only when its first directory is a context's staging
directory; it passes when an ``include`` glob selects it (and no
``exclude`` does), when a glob may select files below it (a directory), or
when it is an image's ``files`` path (an output the build writes there).

Importing this module runs nothing.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterator, Sequence
from typing import Any

from piceli.artifacts.build_context import Glob
from piceli.artifacts.build_spec import BuildSpecError

_SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ash"})
#: Programs whose path arguments are (often) outputs: never checked.
_WRITERS = frozenset(
    {"mkdir", "touch", "cp", "mv", "rm", "ln", "tee", "install", "rsync", "tar"}
)
#: Programs that run their first path argument (in a shell script).
_RUNNERS = frozenset({*_SHELLS, "source", ".", "exec", "python", "python3", "node"})
_OUTPUT_OPTION = re.compile(r"-o|--?(?:out|output|dest|target)[\w-]*")
_SEPARATORS = re.compile(r"\s*(?:;|&&|\|\||\||\n)\s*")
_PLAIN = re.compile(r"[A-Za-z0-9._@+/-]+")


def check(spec: Any) -> None:
    """Raise :class:`BuildSpecError` (``host-build-path-not-staged``) for the first command
    argument that names a path its context does not stage."""
    contexts = {(item.target or item.name): item for item in spec.contexts}
    outputs = [
        posixpath.normpath(source)
        for image in spec.images
        for source, _ in image.files
        if "{" not in source
    ]
    groups = (
        ("build.shared_commands", spec.shared_commands),
        ("build.commands", spec.commands),
    )
    for where, commands in groups:
        for index, command in enumerate(commands):
            for token in _candidates(command):
                found = _unstaged(token, spec.workdir, contexts, outputs)
                if found is None:
                    continue
                context, rest = found
                selection = context.selection
                raise BuildSpecError(
                    "host-build-path-not-staged",
                    f"{where}[{index}] {list(command)!r} names {token!r}, but "
                    f"context {context.name!r} stages only include "
                    f"{list(selection.include)!r}"
                    + (
                        f" exclude {list(selection.exclude)!r}"
                        if selection.exclude
                        else ""
                    )
                    + f": {rest!r} is not staged; add it to "
                    f"context.{context.name}.include or fix the path",
                )


def _candidates(command: Sequence[str]) -> Iterator[str]:
    program = posixpath.basename(command[0])
    if program in _SHELLS and "-c" in command[1:]:
        at = command.index("-c", 1)
        if at + 1 < len(command):
            yield from _script(command[at + 1])
        return
    if program in _WRITERS:
        return
    skip = False
    for arg in command[1:]:
        if skip:
            skip = False
            continue
        if arg.startswith("-"):
            skip = _OUTPUT_OPTION.fullmatch(arg) is not None
            continue
        yield arg


def _script(text: str) -> Iterator[str]:
    """The programs (and scripts run) of a shell script's simple commands."""
    for part in _SEPARATORS.split(text):
        words = part.split()
        while words and "=" in words[0] and not words[0].startswith("="):
            words = words[1:]  # VAR=value prefixes
        if not words:
            continue
        first = words[0]
        if posixpath.basename(first) in _RUNNERS:
            rest = [word for word in words[1:] if not word.startswith("-")]
            if rest and "-c" not in words[1:]:
                yield rest[0]
            continue
        yield first


def _unstaged(
    token: str, workdir: str, contexts: dict[str, Any], outputs: Sequence[str]
) -> tuple[Any, str] | None:
    if (
        "/" not in token
        or token.startswith(("/", "~"))
        or "://" in token
        or "{" in token  # a placeholder: its value is not known yet
        or _PLAIN.fullmatch(token) is None
    ):
        return None
    path = posixpath.normpath(posixpath.join(workdir, token))
    if path.startswith("..") or path.startswith("/"):
        return None
    head, _, rest = path.partition("/")
    context = contexts.get(head)
    if context is None or not rest:
        return None
    if any(path == item or path.startswith(item + "/") for item in outputs):
        return None
    if any(item == path or item.startswith(path + "/") for item in outputs):
        return None
    selection = context.selection
    includes = [Glob(item) for item in selection.include]
    if any(item.matches(rest) for item in includes):
        if not any(Glob(item).matches(rest) for item in selection.exclude):
            return None
        return context, rest
    parts = tuple(rest.split("/"))
    if any(item.may_contain(parts) for item in includes):
        return None  # a directory holding staged files
    return context, rest
