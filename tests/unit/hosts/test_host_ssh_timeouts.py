"""``piceli host status`` and ``wait`` never hang on SSH (0.26.2).

The status read runs ``ssh`` with a connect timeout, keepalives that give
up and an overall timeout; a failure is ``host-unreachable`` (no
connection, host key, login) or ``host-ssh-failed`` (connected, then the
session closed without an exit status or hung), with SSH's own message,
and names the ``HostReport`` endpoint when the host declares one.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.dev.nix import NixCache
from piceli.hosts import remote
from piceli.hosts.model import HostError, HostReport, NixHost, SshTarget, Tag

from .test_agent import PUBLIC

TARGET = SshTarget.parse("ssh://root@web-1.example.net")


def _option(argv: list[str], name: str) -> str:
    for i, word in enumerate(argv):
        if word == "-o" and argv[i + 1].startswith(f"{name}="):
            return argv[i + 1].split("=", 1)[1]
    raise AssertionError(f"no -o {name} in {argv}")


def test_the_status_read_has_a_connect_keepalive_and_overall_timeout() -> None:
    seen: dict[str, Any] = {}

    def runner(argv: list[str], **kwargs: Any) -> Any:
        seen.update(argv=argv, timeout=kwargs.get("timeout"))
        return subprocess.CompletedProcess(argv, 0, b'{"host": "web-1"}', b"")

    remote.read_status(TARGET, runner=runner)
    argv = seen["argv"]
    assert int(_option(argv, "ConnectTimeout")) <= 10
    alive = int(_option(argv, "ServerAliveInterval"))
    count = int(_option(argv, "ServerAliveCountMax"))
    assert alive * count <= 30
    assert seen["timeout"] is not None and seen["timeout"] <= 30


@pytest.mark.parametrize(
    ("stderr", "code"),
    [
        (b"ssh: connect to host web-1.example.net port 22: Connection refused\r\n",
         "host-unreachable"),
        (b"ssh: Could not resolve hostname web-1.example.net: nodename\n",
         "host-unreachable"),
        (b"Host key verification failed.\n", "host-unreachable"),
        (b"root@web-1.example.net: Permission denied (publickey).\n",
         "host-unreachable"),
        # Authenticated, then the session closed without an exit status.
        (b"", "host-ssh-failed"),
        (b"Connection to web-1.example.net closed by remote host.\n",
         "host-ssh-failed"),
    ],
)  # fmt: skip
def test_an_ssh_failure_has_a_clear_code_and_sshs_message(
    stderr: bytes, code: str
) -> None:
    def runner(argv: list[str], **_: Any) -> Any:
        return subprocess.CompletedProcess(argv, 255, b"", stderr)

    with pytest.raises(HostError) as raised:
        remote.read_status(TARGET, runner=runner)
    assert raised.value.code == code
    last = stderr.decode().strip()
    if last:
        assert last in str(raised.value)
    else:
        assert "no exit status" in str(raised.value)


def test_a_hung_session_is_host_ssh_failed() -> None:
    def runner(argv: list[str], **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"], stderr=b"")

    with pytest.raises(HostError) as raised:
        remote.read_status(TARGET, runner=runner)
    assert raised.value.code == "host-ssh-failed"
    assert "did not finish" in str(raised.value)


def _declared(tmp_path: Path) -> NixHost:
    return NixHost(
        "web-1", server="ssh://root@web-1.example.net", flake=".#web-1",
        follow=Tag("v*"), cache=NixCache(PUBLIC, url=f"file://{tmp_path}"),
        report=HostReport("https://otel.example.net:4318"),
    )  # fmt: skip


def _cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reads: list[Any]) -> Any:
    import piceli.hosts.remote as remote_module
    import piceli.k8s.cli.host as cli_host
    from piceli.k8s.cli import app

    answers = iter(reads)

    def read(*_a: Any, **_k: Any) -> Any:
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(remote_module, "read_status", read)
    monkeypatch.setattr(cli_host, "load_host", lambda _ref: _declared(tmp_path))
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    return lambda *args: CliRunner().invoke(app, list(args))


FAILED = HostError("host-ssh-failed", "ssh to web-1.example.net: no exit status")


def test_host_status_rejects_with_the_code_and_names_the_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run = _cli(monkeypatch, tmp_path, [FAILED])
    done = run("host", "status", "hosts.py:web")
    assert done.exit_code == 2
    body = json.loads(done.stdout)
    assert body["reason"] == "host-ssh-failed"
    assert "no exit status" in body["message"]
    assert "https://otel.example.net:4318" in body["message"]
    assert body["report"] == "https://otel.example.net:4318"


def test_host_wait_says_each_ssh_failure_and_keeps_waiting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from .test_hosts_cli import REV, _status

    run = _cli(monkeypatch, tmp_path, [FAILED, FAILED, _status()])
    done = run("host", "wait", "hosts.py:web", REV[:12], "--json")
    assert done.exit_code == 0, done.stdout + done.stderr
    assert done.stderr.count("host-ssh-failed") == 1  # said once, not per read
    assert "otel.example.net" in done.stderr
