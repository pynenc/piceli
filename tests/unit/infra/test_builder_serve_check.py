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
