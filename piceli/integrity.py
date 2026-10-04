"""Self-check of an image's Python files against a manifest written at build time.

A node whose disk or memory alters an unpacked image layer gives the
container files Kubernetes cannot see as wrong: the process then fails at
some import with an unrelated error (a malformed ``.pyc`` reads as
``ValueError: code: co_varnames is too small``) and crash-loops. The
controller and UI images ship ``MANIFEST`` (``sha256  path`` of every file
of the Python installation, written by ``python -B -m piceli.integrity
write``); with ``PICELI_SELF_CHECK=<manifest>`` the ``piceli`` command
verifies it before importing anything else and stops with one clear
message.

Standard library only, and nothing of ``piceli`` is imported: this runs
before any file it checks is trusted.
"""

from __future__ import annotations

import hashlib
import os
import sys
import sysconfig
from collections.abc import Iterable, Iterator
from pathlib import Path

#: Where the images keep the manifest (outside the hashed directories).
MANIFEST = "/usr/local/share/piceli/files.sha256"
#: Set by the controller and UI Deployments; unset (a laptop) checks nothing.
ENV = "PICELI_SELF_CHECK"
#: The exit status of a failed check (``EX_SOFTWARE``), told apart by status readers.
EXIT_CORRUPTED = 70
MESSAGE = (
    "controller image files corrupted on this node; remove the image from "
    "the node's containerd and restart"
)
#: Kubernetes shows this file's content as the container's last message.
TERMINATION_LOG = "/dev/termination-log"


def roots() -> list[Path]:
    """The Python installation: the standard library and installed packages."""
    paths = sysconfig.get_paths()
    found: list[Path] = []
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        path = Path(paths[key]).resolve()
        if path.is_dir() and not any(path.is_relative_to(item) for item in found):
            found = [item for item in found if not item.is_relative_to(path)]
            found.append(path)
    return sorted(found)


def _files(directories: Iterable[Path]) -> Iterator[Path]:
    for directory in directories:
        for base, dirs, names in os.walk(directory):
            dirs.sort()
            for name in sorted(names):
                path = Path(base, name)
                if path.is_file() and not path.is_symlink():
                    yield path


def _digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            sha.update(chunk)
    return sha.hexdigest()


def write(out: Path, directories: Iterable[Path] | None = None) -> int:
    """Write the manifest of ``directories`` (default: :func:`roots`); its line count."""
    lines = [f"{_digest(path)}  {path}" for path in _files(directories or roots())]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n")
    return len(lines)


def verify(manifest: Path) -> list[str]:
    """Paths whose bytes differ from the manifest, or that are missing."""
    bad: list[str] = []
    for line in manifest.read_text().splitlines():
        digest, _, name = line.partition("  ")
        if not name:
            continue
        path = Path(name)
        try:
            if _digest(path) != digest:
                bad.append(name)
        except OSError:
            bad.append(name)
    return bad


def self_check(environ: dict[str, str] | None = None) -> None:
    """Verify the manifest named by ``PICELI_SELF_CHECK``; exit 70 when files differ.

    No variable: nothing to check (laptops, CI). A variable naming a missing
    manifest (an image older than this check) is reported once and skipped.
    """
    environ = dict(os.environ if environ is None else environ)
    name = environ.get(ENV)
    if not name:
        return
    manifest = Path(name)
    if not manifest.is_file():
        print(f"piceli: self-check skipped, no manifest at {manifest}", file=sys.stderr)
        return
    bad = verify(manifest)
    if not bad:
        return
    shown = ", ".join(bad[:3]) + (f" and {len(bad) - 3} more" if len(bad) > 3 else "")
    text = f"{MESSAGE} ({len(bad)} file(s) differ from the image: {shown})"
    print(f"piceli: {text}", file=sys.stderr)
    try:
        Path(environ.get("PICELI_TERMINATION_LOG", TERMINATION_LOG)).write_text(text)
    except OSError:
        pass
    raise SystemExit(EXIT_CORRUPTED)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["write"]:
        out = Path(args[2] if args[1:2] == ["--out"] and len(args) > 2 else MANIFEST)
        count = write(out)
        print(f"wrote {count} file digests to {out}")
        return 0
    if args[:1] == ["verify"]:
        manifest = Path(args[1] if len(args) > 1 else MANIFEST)
        bad = verify(manifest)
        for name in bad:
            print(name)
        return EXIT_CORRUPTED if bad else 0
    print(
        "usage: python -B -m piceli.integrity write [--out FILE] | verify [FILE]",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
