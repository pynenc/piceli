"""0.29.0: ``builder serve --check`` reaches every source and writes from
inside the builder container; ``--args-file`` and ``--take-next`` on the
command line."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.cli_contract import Rejected
from piceli.infra.builder_serve import check_sources, scratch_check
from piceli.k8s.cli import app
from piceli.k8s.cli.builder import expand_args_file
from tests.unit.infra.git_support import Repo


def test_check_sources_runs_ls_remote_of_each_source(tmp_path: Path) -> None:
    repo = Repo(tmp_path / "remotes", "shop", {"README": "x\n"})
    rows = check_sources(
        {"shop": repo.url, "gone": str(tmp_path / "remotes" / "nothing.git")}
    )
    by = {row["source"]: row for row in rows}
    assert by["shop"]["state"] == "reachable" and by["shop"]["via"] == repo.url
    assert by["gone"]["state"] == "unreachable" and by["gone"]["detail"]


def test_check_sources_tries_ssh_after_https() -> None:
    seen: list[list[str]] = []

    def run(argv: list[str], **_kw: Any) -> subprocess.CompletedProcess[bytes]:
        seen.append(argv)
        ok = argv[-1].startswith("git@")
        return subprocess.CompletedProcess(argv, 0 if ok else 128, b"", b"denied")

    (row,) = check_sources({"app": "https://example.org/team/app.git"}, run=run)
    assert row == {
        "source": "app",
        "url": "https://example.org/team/app.git",
        "state": "reachable",
        "via": "git@example.org:team/app.git",
    }
    assert [argv[:3] for argv in seen] == [["git", "ls-remote", "--heads"]] * 2


class _Cli:
    def __init__(self, code: int, stderr: bytes = b"") -> None:
        self.code, self.stderr = code, stderr
        self.calls: list[tuple[str, ...]] = []

    def run(self, *args: str, timeout: float = 120) -> Any:
        if args[:2] == ("volume", "inspect"):
            return subprocess.CompletedProcess(list(args), 0, b"", b"")
        self.calls.append(args)
        mount = next(a for a in args if a.endswith(":/work/out"))
        assert Path(mount.split(":", 1)[0]).is_dir()  # the scratch exists then
        return subprocess.CompletedProcess(list(args), self.code, b"", self.stderr)


def test_scratch_check_writes_as_the_build_does() -> None:
    cli = _Cli(0)
    assert scratch_check(cli, "builder:1")["state"] == "writable"
    (call,) = cli.calls
    assert call[:4] == ("run", "--rm", "--user", "65532:65532")
    assert "piceli-build-cache:/cache" in call and "builder:1" in call
    mount = next(a for a in call if a.endswith(":/work/out"))
    assert not Path(mount.split(":", 1)[0]).exists()  # removed afterwards

    bad = scratch_check(_Cli(1, b"touch: /work/out/x: Permission denied\n"), "b:1")
    assert bad["state"] == "not-writable" and "Permission denied" in bad["detail"]


class _Engine:
    """0.56.0: an engine with or without the cache volume; ``chown`` fails
    when ``refuse`` is set."""

    def __init__(self, *, volume: bool, refuse: bool = False) -> None:
        self.volume, self.refuse = volume, refuse
        self.calls: list[tuple[str, ...]] = []

    def run(self, *args: str, timeout: float = 120) -> Any:
        self.calls.append(args)
        code = 0
        if args[:2] == ("volume", "inspect"):
            code = 0 if self.volume else 1
        elif "chown" in args and self.refuse:
            code = 1
        return subprocess.CompletedProcess(list(args), code, b"", b"denied")


def test_the_check_never_creates_the_cache_volume() -> None:
    engine = _Engine(volume=False)
    assert scratch_check(engine, "builder:1")["state"] == "missing"
    assert engine.calls == [("volume", "inspect", "piceli-build-cache")]


def test_an_existing_cache_volume_is_given_to_the_build_user() -> None:
    from piceli.infra.builder_serve import BuildFailed, prepare_cache

    engine = _Engine(volume=True)  # made root's by another mount
    prepare_cache(engine, "builder:1")
    assert [c[0] for c in engine.calls] == ["volume", "run"]
    assert engine.calls[-1][-3:] == ("builder:1", "65532:65532", "/cache")
    new = _Engine(volume=False)
    prepare_cache(new, "builder:1")
    assert [c[:2] for c in new.calls][:2] == [
        ("volume", "inspect"),
        ("volume", "create"),
    ]
    with pytest.raises(BuildFailed, match="cache volume"):
        prepare_cache(_Engine(volume=True, refuse=True), "builder:1")


def test_stale_scratch_of_killed_builds_is_removed_through_the_container(
    tmp_path: Path,
) -> None:
    """0.57.0: the container's user wrote files the host's user cannot remove."""
    import os
    import time

    from piceli.infra.builder_serve import clean_stale_scratch

    stale = tmp_path / "piceli-builder-abcd1234"
    (stale / "out" / ".home").mkdir(parents=True)
    fresh = tmp_path / "piceli-builder-efgh5678"
    fresh.mkdir()
    other = tmp_path / "piceli-dev-ijkl9012"
    other.mkdir()
    old = time.time() - 7 * 3600
    for path in (stale, other):
        os.utime(path, (old, old))
    engine = _Engine(volume=True)
    lines: list[str] = []
    assert clean_stale_scratch(engine, "builder:1", lines.append, tmp_path) == 1
    assert not stale.exists() and fresh.exists() and other.exists()
    assert engine.calls == []  # the host's user could remove it all
    assert lines == ["builder: removed 1 scratch director(ies) of killed builds"]
    for path in (fresh, other):  # the suite's leak check sees the names
        path.rmdir()


def test_the_build_opens_its_scratch_to_the_host_user() -> None:
    from piceli.infra.builder_serve import _COPY_AND_RUN

    assert "chmod -R a+rwX /work/out" in _COPY_AND_RUN.splitlines()[3]


def test_args_file_adds_its_arguments(tmp_path: Path) -> None:
    path = tmp_path / "serve.args"
    path.write_text(
        "# written by the operator\n--image\nghcr.io/x/builder@sha256:abc\n\n--poll\n30\n"
    )
    argv = ["builder", "serve", "--cluster", "infra:c", "--args-file", str(path)]
    assert expand_args_file(argv) == [
        "builder", "serve", "--cluster", "infra:c",
        "--image", "ghcr.io/x/builder@sha256:abc", "--poll", "30",
    ]  # fmt: skip
    assert expand_args_file(["gitops", "status", "--args-file", "x"])[-1] == "x"
    path.write_text("--args-file\nother\n")
    with pytest.raises(Rejected) as caught:
        expand_args_file(argv)
    assert caught.value.reason == "cluster-build-invalid"


def test_take_next_needs_once() -> None:
    result = CliRunner().invoke(
        app, ["builder", "serve", "--cluster", "infra:c", "--take-next"]
    )
    assert result.exit_code == 2
    assert "add --once" in result.output


def _run_copy(
    tmp_path: Path,
    dirty: bool = False,
    unreadable: bool = False,
    lose: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """The container's copy step on a local source; ``piceli`` is a stub."""
    import os

    from piceli.infra.builder_serve import _COPY_AND_RUN, SOURCES

    source = tmp_path / "work" / "src" / "shop"
    source.mkdir(parents=True)
    git = ["git", "-C", str(source), "-c", "user.email=t@x", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
    (source / "a.txt").write_text("a\n")
    (source / "run.sh").write_text("#!/bin/sh\n")
    (source / "run.sh").chmod(0o755)
    (source / "link").symlink_to("a.txt")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "one"], check=True)
    if dirty:
        (source / "extra.txt").write_text("not committed\n")
    if lose:  # the source tree lacks a tracked file: the copy does too
        (source / lose).unlink()
    if unreadable:
        (source / "a.txt").chmod(0)
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "piceli").write_text("#!/bin/sh\nexit 0\n")
    (stub / "piceli").chmod(0o755)
    script = _COPY_AND_RUN.replace(SOURCES, str(tmp_path / "dest")).replace(
        "/work/", f"{tmp_path}/work/"
    )
    try:
        return subprocess.run(
            ["sh", "-c", script, "sh"],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "PATH": f"{stub}:{os.environ['PATH']}"},
        )
    finally:
        if (source / "a.txt").exists():
            (source / "a.txt").chmod(0o644)  # the test directory is removed


def test_a_clean_copy_says_nothing(tmp_path: Path) -> None:
    done = _run_copy(tmp_path)
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")


def test_a_copy_that_lost_a_tracked_file_is_restored_and_says_so(
    tmp_path: Path,
) -> None:
    done = _run_copy(tmp_path, lose="a.txt")
    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert lines[0] == "builder: after the copy, source shop is not clean:  D a.txt;"
    assert lines[1].startswith("builder: in the mount: ")
    assert "No such file or directory" in lines[1] and "a.txt" in lines[1]
    assert lines[-1] == "builder: source shop restored from its objects; it is clean"


def test_an_untracked_file_in_the_copy_is_named_not_hidden(tmp_path: Path) -> None:
    done = _run_copy(tmp_path, dirty=True)
    assert done.returncode == 0
    assert "source shop is not clean: ?? extra.txt;" in done.stdout
    assert done.stdout.splitlines()[-1] == (
        "builder: source shop is still not clean: ?? extra.txt;"
    )


@pytest.mark.skipif(__import__("os").geteuid() == 0, reason="root reads any file")
def test_a_file_the_copy_cannot_read_stops_the_build_with_its_reason(
    tmp_path: Path,
) -> None:
    done = _run_copy(tmp_path, unreadable=True)
    assert done.returncode != 0
    assert "a.txt" in done.stderr and "ermission denied" in done.stderr
