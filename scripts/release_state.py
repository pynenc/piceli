"""Release decisions for ``.github/workflows/release.yml`` (and TestPyPI in ``ci.yml``).

The package index, not the git tag, says whether a version is released: a
version is released when the index lists every distribution built for it. The
workflow builds, asks :func:`plan` what to do, uploads the missing files,
waits until :func:`verify` sees all of them on the index, and only then pushes
the tag (:func:`tag`). Every step can be re-run: an interrupted run, re-run or
started again, uploads what is missing, tags once and never moves a tag.

Commands (one JSON object on stdout, human text on stderr; with
``$GITHUB_OUTPUT`` set, the outputs are also written there)::

    plan   --dist DIR --sha SHA [--index URL] [--git-dir DIR]
    verify --dist DIR [--index URL] [--attempts N] [--delay SECONDS]
    tag    --tag TAG --sha SHA [--git-dir DIR] [--message TEXT]

Exit codes: ``0`` done, ``1`` the check failed (files missing after every
attempt, the tag points at another commit, the index cannot be read).

Standard library only, so it runs before anything is installed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

PYPI = "https://pypi.org"
TEST_PYPI = "https://test.pypi.org"
USER_AGENT = "piceli-release-state/1 (+https://github.com/pynenc/piceli)"

#: ``(url) -> parsed JSON, or None when the index answers 404``.
Fetch = Callable[[str], Mapping[str, Any] | None]
Sleep = Callable[[float], None]


class ReleaseStateError(Exception):
    """A release check failed; the message is safe to print."""


# ------------------------------------------------------------ distributions


@dataclass(frozen=True)
class Distribution:
    filename: str
    sha256: str


@dataclass(frozen=True)
class Build:
    """The distributions of one version, as built into ``dist/``."""

    project: str
    version: str
    files: tuple[Distribution, ...]

    @property
    def tag(self) -> str:
        return f"v{self.version}"


_WHEEL = re.compile(
    r"^(?P<name>[^-]+)-(?P<version>[^-]+)(-\d[^-]*)?-[^-]+-[^-]+-[^-]+\.whl$"
)
_SDIST = re.compile(r"^(?P<name>.+)-(?P<version>[^-]+)\.tar\.gz$")


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def read_build(dist: Path) -> Build:
    """The project, version and files in ``dist``: at least one wheel and one sdist.

    Other files (``.gitignore``, attestations) are ignored. Every distribution
    must name the same project and version.
    """
    if not dist.is_dir():
        raise ReleaseStateError(f"no distribution directory {dist}")
    names: set[tuple[str, str]] = set()
    files: list[Distribution] = []
    kinds: set[str] = set()
    for path in sorted(dist.iterdir()):
        match = _WHEEL.match(path.name) or _SDIST.match(path.name)
        if match is None or not path.is_file():
            continue
        kinds.add("wheel" if path.name.endswith(".whl") else "sdist")
        names.add((_normalize(match["name"]), match["version"]))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        files.append(Distribution(path.name, digest))
    if kinds != {"wheel", "sdist"}:
        raise ReleaseStateError(
            f"{dist} must hold a wheel and an sdist, found: "
            + (", ".join(item.filename for item in files) or "nothing")
        )
    if len(names) != 1:
        raise ReleaseStateError(
            "distributions name different projects or versions: "
            + ", ".join(f"{name} {version}" for name, version in sorted(names))
        )
    ((project, version),) = names
    return Build(project, version, tuple(files))


# -------------------------------------------------------------------- index


def http_fetch(url: str, *, timeout: float = 30.0) -> Mapping[str, Any] | None:
    """GET ``url`` as JSON; ``None`` on 404. Other failures raise ``OSError``."""
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def _retrying(
    fetch: Fetch, url: str, *, attempts: int, delay: float, sleep: Sleep
) -> Mapping[str, Any] | None:
    """``fetch(url)``, retrying transient failures (5xx, network) with backoff."""
    for attempt in range(1, attempts + 1):
        try:
            return fetch(url)
        except (OSError, ValueError) as error:
            if attempt == attempts:
                raise ReleaseStateError(
                    f"cannot read {url} after {attempts} attempts: {error}"
                ) from error
            wait = min(delay * 2 ** (attempt - 1), 60.0)
            _say(f"index unavailable ({error}); retrying in {wait:.0f}s")
            sleep(wait)
    raise AssertionError("unreachable")


def published(
    build: Build,
    index: str = PYPI,
    *,
    fetch: Fetch = http_fetch,
    attempts: int = 5,
    delay: float = 5.0,
    sleep: Sleep = time.sleep,
) -> dict[str, str]:
    """``{filename: sha256}`` of the files the index lists for this version."""
    url = f"{index.rstrip('/')}/pypi/{build.project}/{build.version}/json"
    body = _retrying(fetch, url, attempts=attempts, delay=delay, sleep=sleep)
    if body is None:
        return {}
    return {
        str(item["filename"]): str(item.get("digests", {}).get("sha256", ""))
        for item in body.get("urls", ())
    }


# --------------------------------------------------------------------- tags


def remote_tag_commit(ls_remote: str, tag: str) -> str | None:
    """The commit ``tag`` points at, from ``git ls-remote`` output (``None``: absent).

    An annotated tag is listed twice; the peeled ``^{}`` line names the commit.
    """
    direct = peeled = None
    for line in ls_remote.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        sha, ref = parts
        if ref == f"refs/tags/{tag}^{{}}":
            peeled = sha
        elif ref == f"refs/tags/{tag}":
            direct = sha
    return peeled or direct


def _git(git_dir: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(git_dir), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ReleaseStateError(
            f"git {args[0]} failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout


def _has_commit(git_dir: Path, sha: str) -> bool:
    try:
        _git(git_dir, "cat-file", "-e", f"{sha}^{{commit}}")
    except ReleaseStateError:
        return False
    return True


def lookup_tag(git_dir: Path, tag: str, remote: str = "origin") -> str | None:
    output = _git(git_dir, "ls-remote", "--tags", remote, f"refs/tags/{tag}*")
    return remote_tag_commit(output, tag)


# ------------------------------------------------------------------ decide


@dataclass(frozen=True)
class Plan:
    """What the release workflow does for one build.

    ``publish``: upload the files the index does not list. ``finish``: verify,
    tag and publish release notes (``False`` when this version was released
    from another commit and there is nothing left to do).
    """

    project: str
    version: str
    tag: str
    sha: str
    state: str
    publish: bool
    finish: bool
    missing: tuple[str, ...]
    tag_commit: str | None
    mismatched: tuple[str, ...] = field(default=())

    def outputs(self) -> dict[str, str]:
        return {
            "project": self.project,
            "version": self.version,
            "tag": self.tag,
            "sha": self.sha,
            "state": self.state,
            "publish": str(self.publish).lower(),
            "finish": str(self.finish).lower(),
            "missing": " ".join(self.missing),
        }


def decide(
    build: Build, on_index: Mapping[str, str], tag_commit: str | None, sha: str
) -> Plan:
    """The release decision; pure, so it is tested without an index or git.

    - Nothing on the index: publish, then tag ``sha``.
    - Some files on the index (an interrupted upload): publish the rest.
    - Every file on the index: nothing to upload; tag ``sha`` if the tag is
      missing (an interrupted run), finish if it already points at ``sha``,
      and do nothing when the version was released from another commit.
    - A tag that points at another commit while files are still missing is
      refused: the version would be published from a commit its tag does not
      name.
    """
    missing = tuple(
        item.filename for item in build.files if item.filename not in on_index
    )
    mismatched = tuple(
        item.filename
        for item in build.files
        if on_index.get(item.filename) not in (None, "", item.sha256)
    )
    elsewhere = tag_commit is not None and tag_commit != sha
    if missing and elsewhere:
        raise ReleaseStateError(
            f"{build.tag} points at {tag_commit}, not {sha}, but {build.project} "
            f"{build.version} is not fully published (missing: {', '.join(missing)}). "
            f"Release that commit (workflow_dispatch with ref={build.tag}) or bump "
            "the version."
        )
    if not missing:
        state = "released"
    elif len(missing) == len(build.files):
        state = "unreleased"
    else:
        state = "partial"
    return Plan(
        project=build.project,
        version=build.version,
        tag=build.tag,
        sha=sha,
        state=state,
        publish=bool(missing),
        finish=not elsewhere,
        missing=missing,
        tag_commit=tag_commit,
        mismatched=mismatched,
    )


def verify(
    build: Build,
    index: str = PYPI,
    *,
    fetch: Fetch = http_fetch,
    attempts: int = 10,
    delay: float = 5.0,
    sleep: Sleep = time.sleep,
) -> dict[str, Any]:
    """Wait until the index lists every file of ``build``; raise when it never does.

    The index API is cached, so a fresh upload can take a while to appear;
    the delay doubles between attempts (at most 60 seconds).
    """
    missing: list[str] = []
    for attempt in range(1, attempts + 1):
        on_index = published(build, index, fetch=fetch, sleep=sleep)
        missing = [
            item.filename for item in build.files if item.filename not in on_index
        ]
        if not missing:
            mismatched = [
                item.filename
                for item in build.files
                if on_index[item.filename] not in ("", item.sha256)
            ]
            return {
                "state": "verified",
                "project": build.project,
                "version": build.version,
                "files": sorted(on_index),
                "mismatched": mismatched,
                "attempts": attempt,
            }
        if attempt < attempts:
            wait = min(delay * 2 ** (attempt - 1), 60.0)
            _say(f"not on the index yet: {', '.join(missing)}; retrying in {wait:.0f}s")
            sleep(wait)
    raise ReleaseStateError(
        f"{build.project} {build.version}: still missing on {index} after "
        f"{attempts} attempts: {', '.join(missing)}"
    )


@dataclass(frozen=True)
class TagResult:
    tag: str
    sha: str
    action: str  # "created" | "exists"

    def outputs(self) -> dict[str, str]:
        return {"tag": self.tag, "sha": self.sha, "tag_action": self.action}


def tag(
    git_dir: Path,
    name: str,
    sha: str,
    *,
    message: str | None = None,
    remote: str = "origin",
) -> TagResult:
    """Create and push the annotated tag ``name`` at ``sha``, once.

    Already at ``sha``: nothing to do. At another commit: refused, a published
    tag never moves. A push that loses a race is accepted when the tag that
    won points at ``sha``.
    """
    current = lookup_tag(git_dir, name, remote)
    if current == sha:
        return TagResult(name, sha, "exists")
    if current is not None:
        raise ReleaseStateError(f"{name} already points at {current}, not {sha}")
    if not _has_commit(git_dir, sha):  # a shallow checkout of another commit
        _git(git_dir, "fetch", "--no-tags", "--depth=1", remote, sha)
    _git(
        git_dir, "tag", "--force", "-a", name, sha, "-m", message or f"Released {name}"
    )
    try:
        _git(git_dir, "push", remote, f"refs/tags/{name}")
    except ReleaseStateError:
        if lookup_tag(git_dir, name, remote) == sha:
            return TagResult(name, sha, "exists")
        raise
    return TagResult(name, sha, "created")


# --------------------------------------------------------------------- CLI


def _say(message: str) -> None:
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


def _write_outputs(outputs: Mapping[str, str]) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in outputs.items():
            if "\n" in value:
                raise ReleaseStateError(f"output {key} spans lines")
            handle.write(f"{key}={value}\n")


def _emit(value: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, sort_keys=True) + "\n")


def main(argv: Sequence[str] | None = None, *, fetch: Fetch = http_fetch) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    plan_cmd = commands.add_parser("plan", help="decide what this run publishes")
    plan_cmd.add_argument("--dist", type=Path, default=Path("dist"))
    plan_cmd.add_argument("--index", default=PYPI)
    plan_cmd.add_argument("--sha", required=True)
    plan_cmd.add_argument("--git-dir", type=Path, default=Path())
    verify_cmd = commands.add_parser("verify", help="wait for every file on the index")
    verify_cmd.add_argument("--dist", type=Path, default=Path("dist"))
    verify_cmd.add_argument("--index", default=PYPI)
    verify_cmd.add_argument("--attempts", type=int, default=10)
    verify_cmd.add_argument("--delay", type=float, default=5.0)
    tag_cmd = commands.add_parser("tag", help="push the tag once, never move it")
    tag_cmd.add_argument("--tag", required=True)
    tag_cmd.add_argument("--sha", required=True)
    tag_cmd.add_argument("--git-dir", type=Path, default=Path())
    tag_cmd.add_argument("--message")
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            build = read_build(args.dist)
            decision = decide(
                build,
                published(build, args.index, fetch=fetch),
                lookup_tag(args.git_dir, build.tag),
                args.sha,
            )
            _write_outputs(decision.outputs())
            _emit(asdict(decision))
            _say(
                f"{build.project} {build.version} is {decision.state} on {args.index}; "
                f"publish: {decision.publish}; finish: {decision.finish}"
            )
            for name in decision.mismatched:
                _say(f"::warning::{name} on the index differs from this build")
        elif args.command == "verify":
            build = read_build(args.dist)
            result = verify(
                build, args.index, fetch=fetch, attempts=args.attempts, delay=args.delay
            )
            _emit(result)
            _say(f"{build.project} {build.version}: every file is on {args.index}")
            for name in result["mismatched"]:
                _say(
                    f"::warning::{name} on the index differs from this build "
                    "(uploaded by an earlier attempt)"
                )
        else:
            result_tag = tag(args.git_dir, args.tag, args.sha, message=args.message)
            _write_outputs(result_tag.outputs())
            _emit(asdict(result_tag))
            _say(f"{result_tag.tag} at {result_tag.sha}: {result_tag.action}")
    except ReleaseStateError as error:
        _emit({"state": "failed", "message": str(error)})
        _say(f"::error::{error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
