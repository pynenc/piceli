"""Loading a :class:`~piceli.train.model.Train` from a module, in a subprocess.

A train judges each candidate by the commit under test, so its spec module
is imported from the merged checkout. That happens in a fresh Python process
(``python -m piceli.train.spec ENTRY``, run in the checkout): no module
cached from another commit, no ``sys.path`` left behind, and a module that
crashes or prints cannot disturb the train. It writes no bytecode
(``-B``): neither the caller's checkout nor a merged tree gets a
``__pycache__``. The process prints one JSON
object: ``{"file": <module file>, "attr": <name>, "train": <Train.to_dict()>}``
or ``{"error": <code>, "message": <text>}``.

``ENTRY`` is ``MODULE``, ``MODULE:ATTR``, ``path/to/file.py`` or
``path/to/file.py:ATTR``; without ``ATTR`` the module must declare exactly
one ``Train``.

Importing this module is side-effect free.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from piceli.train.model import Train, TrainError

#: Seconds importing a spec module may take.
LOAD_TIMEOUT = 120.0
_ATTR = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*")


@dataclass(frozen=True)
class LoadedSpec:
    """A spec read from a module: the train, the module's file and the
    attribute naming it."""

    train: Train
    file: Path | None
    attr: str


def split_entry(entry: str) -> tuple[str, str | None]:
    """``(module or file, attribute or None)``."""
    target, sep, attr = entry.rpartition(":")
    if sep and target and _ATTR.fullmatch(attr):
        return target, attr
    return entry, None


def load(entry: str, cwd: Path, *, timeout: float = LOAD_TIMEOUT) -> LoadedSpec:
    """Import ``entry`` in a subprocess started in ``cwd``.

    Raises :class:`TrainError` (``train-not-found``, ``train-load-failed``,
    ``train-invalid``).
    """
    try:
        done = subprocess.run(
            [sys.executable, "-P", "-B", "-m", "piceli.train.spec", entry],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        raise TrainError(
            "train-load-failed", f"importing {entry} took over {timeout:.0f} s"
        ) from None
    lines = done.stdout.strip().splitlines()
    try:
        body = json.loads(lines[-1]) if lines else None
    except ValueError:
        body = None
    if not isinstance(body, dict):
        tail = done.stderr.strip().splitlines()[-1:] or ["no output"]
        raise TrainError("train-load-failed", f"importing {entry} failed: {tail[0]}")
    if "error" in body:
        raise TrainError(str(body["error"]), str(body.get("message", "")))
    try:
        train = Train.from_dict(body["train"])
    except (KeyError, TypeError) as error:
        raise TrainError("train-invalid", f"{entry}: {error}") from None
    file = Path(body["file"]).resolve() if body.get("file") else None
    return LoadedSpec(train, file, str(body["attr"]))


def _find(module: Any, attr: str | None, entry: str) -> tuple[Train, str]:
    if attr is not None:
        value: Any = module
        for part in attr.split("."):
            if not hasattr(value, part):
                raise TrainError("train-not-found", f"{entry}: no attribute {part!r}")
            value = getattr(value, part)
        if not isinstance(value, Train):
            raise TrainError(
                "train-not-found", f"{entry} is a {type(value).__name__}, not a Train"
            )
        return value, attr
    found = [
        (name, value)
        for name, value in vars(module).items()
        if isinstance(value, Train)
    ]
    if len(found) != 1:
        raise TrainError(
            "train-not-found",
            f"{entry} declares {len(found)} Train objects; name one as {entry}:ATTR",
        )
    return found[0][1], found[0][0]


def _main(entry: str) -> dict[str, Any]:
    from piceli.app.render import RenderError, load_module
    from piceli.cli_contract import describe_user_error

    target, attr = split_entry(entry)
    try:
        module = load_module(target, Path.cwd())
    except RenderError as error:
        return {"error": "train-not-found", "message": str(error)}
    except TrainError as error:
        return {"error": error.code, "message": str(error)}
    except Exception as error:  # the spec module raised while importing
        return {
            "error": "train-load-failed",
            "message": f"importing {entry} failed: {describe_user_error(error)}",
        }
    try:
        train, name = _find(module, attr, entry)
    except TrainError as error:
        return {"error": error.code, "message": str(error)}
    return {
        "file": getattr(module, "__file__", None),
        "attr": name,
        "train": train.to_dict(),
    }


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m piceli.train.spec ENTRY", file=sys.stderr)
        return 2
    real = sys.stdout
    sys.stdout = sys.stderr  # whatever the module prints is not the result
    try:
        body = _main(args[0])
    finally:
        sys.stdout = real
    real.write(json.dumps(body, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
