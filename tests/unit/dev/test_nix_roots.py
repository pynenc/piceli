"""GC roots on a docker build host: ``BuildHost.docker(roots_dir=)`` (0.26.2).

A docker host's Nix steps see the host's ``/nix`` read-only; with
``roots_dir`` they also bind-mount that host directory at the same path,
read-write, so the indirect roots they make there are ones the host's daemon
honours: a fetched input's from the fetch until the build ended (then a
release step removes them), and the last image of each ``--image`` name.
Nothing here runs docker; ``test_nix_step_docker.py`` checks it live.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from piceli.dev import nix_fetch, nix_inputs, nix_step
from piceli.dev.model import BuildHost, DevError

IMAGE = "ghcr.io/example/builder@sha256:" + "b" * 64
ROOTS = "/var/lib/piceli/gcroots"
STORE = f"/nix/store/{'b' * 32}-source"


def _docker(**options: Any) -> BuildHost:
    return BuildHost.docker("nix-host", context="ssh://nix@nix-host", **options)


class Steps:
    """Records each step's argv and request; answers like the fetcher."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> Any:
        request = json.loads(kwargs["input"])
        self.calls.append((argv, request))
        if "release" in request:
            outcome: dict[str, Any] = {"state": "released"}
        elif "hold" in request:
            outcome = {"state": "held"}
        else:
            outcome = {"state": "fetched", "inputs": []}
        line = nix_fetch.RESULT_MARKER + json.dumps(outcome)
        return subprocess.CompletedProcess(argv, 0, line.encode(), b"")


# ------------------------------------------------------------------ the model


def test_roots_dir_is_an_absolute_path_of_a_docker_host() -> None:
    assert _docker(roots_dir=ROOTS).roots_dir == ROOTS
    for bad in ("gcroots", "/", "/a/../b", "/a:/b", "/a b"):
        with pytest.raises(Exception, match="roots_dir"):
            _docker(roots_dir=bad)
    with pytest.raises(Exception, match="roots_dir"):
        BuildHost.native("mini", roots_dir=ROOTS)


def test_roots_dir_is_described_only_when_set() -> None:
    assert "roots_dir" not in _docker().describe()  # earlier hashes hold
    described = _docker(roots_dir=ROOTS).describe()
    assert described["roots_dir"] == ROOTS
    assert BuildHost.from_dict(described) == _docker(roots_dir=ROOTS)


# ------------------------------------------------------------------ the steps


def test_the_steps_mount_the_roots_dir_read_write_at_the_same_path() -> None:
    argv = nix_step.step_argv(_docker(roots_dir=ROOTS), IMAGE, "pass")
    assert argv[argv.index(f"{ROOTS}:{ROOTS}") - 1] == "-v"
    assert "/nix:/nix:ro" in argv
    plain = nix_step.step_argv(_docker(), IMAGE, "pass")
    assert not any(ROOTS in item for item in plain)


def test_a_docker_host_holds_input_roots_until_the_build_ended() -> None:
    steps = Steps()
    fetch = nix_inputs.fetch_inputs(_docker(roots_dir=ROOTS), IMAGE, runner=steps)
    with fetch.roots() as roots:
        assert isinstance(roots, str) and roots.startswith(ROOTS + "/")
        fetch([], lambda _line: None, roots=roots)
        assert [request.get("roots") for _argv, request in steps.calls] == [roots]
    released = steps.calls[-1][1]
    assert released == {"release": roots}  # removed by a step on the host
    assert len(steps.calls) == 2


def test_the_roots_are_released_when_the_fetch_fails() -> None:
    steps = Steps()
    fetch = nix_inputs.fetch_inputs(_docker(roots_dir=ROOTS), IMAGE, runner=steps)
    with pytest.raises(RuntimeError), fetch.roots() as roots:
        raise RuntimeError("the build failed")
    assert steps.calls[-1][1] == {"release": roots}


def test_without_roots_dir_a_docker_host_keeps_no_root() -> None:
    steps = Steps()
    fetch = nix_inputs.fetch_inputs(_docker(), IMAGE, runner=steps)
    with fetch.roots() as roots:
        assert roots is None
        fetch([], lambda _line: None, roots=roots)
    assert [request for _argv, request in steps.calls] == [{"inputs": []}]


def test_a_native_host_keeps_its_roots_in_a_temporary_directory() -> None:
    fetch = nix_inputs.fetch_inputs(BuildHost.native("mini"), None, runner=Steps())
    with fetch.roots() as roots:
        assert isinstance(roots, Path) and roots.is_dir()
    assert not roots.exists()


def test_the_kept_image_is_rooted_by_name_on_the_host() -> None:
    steps = Steps()
    said: list[str] = []
    held = nix_inputs.hold_root(
        _docker(roots_dir=ROOTS), IMAGE, "web-1-image", STORE, said.append,
        runner=steps,
    )  # fmt: skip
    assert held == f"{ROOTS}/images/web-1-image"
    ((argv, request),) = steps.calls
    assert request == {"hold": {"link": held, "path": STORE}}
    assert f"{ROOTS}:{ROOTS}" in argv
    assert nix_inputs.hold_root(_docker(), IMAGE, "x", STORE, said.append,
                                runner=steps) is None  # fmt: skip
    assert len(steps.calls) == 1


def test_a_kept_image_without_a_root_says_so() -> None:
    def refused(argv: list[str], **_kwargs: Any) -> Any:
        line = nix_fetch.RESULT_MARKER + json.dumps({"state": "failed"})
        return subprocess.CompletedProcess(argv, 1, line.encode(), b"error: no")

    said: list[str] = []
    held = nix_inputs.hold_root(
        _docker(roots_dir=ROOTS), IMAGE, "web-1-image", STORE, said.append,
        runner=refused,
    )  # fmt: skip
    assert held is None
    assert any("no GC root" in line for line in said)


def test_a_docker_host_without_an_image_is_still_refused_first() -> None:
    with pytest.raises(DevError, match="no image"):
        nix_inputs.fetch_inputs(_docker(roots_dir=ROOTS), None)


# ------------------------------------------------------------------ the fetcher


def _nix(tmp_path: Path) -> dict[str, str]:
    (tmp_path / "nix").write_text("#!/bin/sh\n")
    (tmp_path / "nix").chmod(0o755)
    return {"PATH": str(tmp_path)}


def test_the_fetcher_holds_and_releases_a_root(tmp_path: Path) -> None:
    seen: list[list[str]] = []

    def runner(argv: list[str], **_kwargs: Any) -> Any:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    link = tmp_path / "roots" / "images" / "web-1-image"
    held = nix_fetch.run(
        {"hold": {"link": str(link), "path": STORE}}, runner=runner,
        environ=_nix(tmp_path),
    )  # fmt: skip
    assert held == {"state": "held"}
    assert seen[-1][-3:] == ["--out-link", str(link), STORE]
    assert link.parent.is_dir()  # made for it
    (link.parent / "web-1-image").write_text("")
    released = nix_fetch.run({"release": str(tmp_path / "roots")}, runner=runner,
                             environ=_nix(tmp_path))  # fmt: skip
    assert released == {"state": "released"}
    assert not (tmp_path / "roots").exists()


def test_the_fetcher_holds_only_store_paths(tmp_path: Path) -> None:
    outcome = nix_fetch.run(
        {"hold": {"link": str(tmp_path / "l"), "path": "/etc"}},
        runner=lambda *_a, **_k: None, environ=_nix(tmp_path),
    )  # fmt: skip
    assert outcome["state"] == "failed"


def test_the_fetcher_makes_the_roots_directory(tmp_path: Path) -> None:
    roots = tmp_path / "not-yet" / "abc"

    def runner(argv: list[str], **_kwargs: Any) -> Any:
        if "--out-link" in argv:
            assert roots.is_dir()
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        return subprocess.CompletedProcess(argv, 0, STORE.encode(), b"")

    request = {"inputs": [{"name": "app", "mode": "none",
                           "locked": {"type": "git", "url": "https://x/app"}}],
               "roots": str(roots)}  # fmt: skip
    outcome = nix_fetch.fetch(request, runner=runner, environ=_nix(tmp_path))
    assert outcome["inputs"][0]["rooted"] is True
    assert os.listdir(roots) == []  # the fake made no link
