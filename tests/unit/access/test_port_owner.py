"""Who holds a local port: /proc on Linux, lsof and ps elsewhere; never raises."""

from __future__ import annotations

import os
import shutil
import socket
import sys
from pathlib import Path

import pytest

from piceli.k8s import port_owner as module
from piceli.k8s.port_owner import MAX_COMMAND, PortOwner, ProcessInfo, port_owner


def _fake_proc(root: Path, port: int) -> Path:
    proc = root / "proc"
    (proc / "net").mkdir(parents=True)
    header = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
    listen = f"   0: 0100007F:{port:04X} 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 424242 1\n"
    established = f"   1: 0100007F:{port:04X} 0100007F:9999 01 00000000:00000000 00:00000000 00000000  1000        0 999 1\n"
    (proc / "net" / "tcp").write_text(header + established + listen)
    (proc / "net" / "tcp6").write_text(header)
    for pid, parent, command, inode in (
        (4242, 77, b"kubectl\0port-forward\0service/web\x0018080:3000\0", "424242"),
        (77, 1, b"piceli\0observe\0serve\0", None),
        (88, 1, b"other\0", "1"),
    ):
        (proc / str(pid) / "fd").mkdir(parents=True)
        (proc / str(pid) / "cmdline").write_bytes(command)
        (proc / str(pid) / "stat").write_text(f"{pid} (x (y)) S {parent} 0 0\n")
        if inode:
            os.symlink(f"socket:[{inode}]", proc / str(pid) / "fd" / "3")
    return proc


def test_linux_proc_lookup_finds_the_listener_and_its_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    proc = _fake_proc(tmp_path, 18080)
    owner = port_owner(18080, proc=proc)
    assert owner == PortOwner(
        port=18080,
        pid=4242,
        command="kubectl port-forward service/web 18080:3000",
        parent=ProcessInfo(77, "piceli observe serve"),
    )
    assert owner.describe() == (
        "pid 4242 (kubectl port-forward service/web 18080:3000), "
        "started by pid 77 (piceli observe serve)"
    )
    assert owner.to_dict()["parent"] == {"pid": 77, "command": "piceli observe serve"}
    assert port_owner(18081, proc=proc) is None


def test_lsof_lookup_parses_fields_and_asks_ps_for_the_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(module.shutil, "which", lambda name: f"/bin/{name}")
    calls: list[list[str]] = []

    def run(argv: list[str]) -> str | None:
        calls.append(argv)
        if argv[0] == "/bin/lsof":
            return "p4242\ncpython3\nf3\n"
        pid = argv[-1]
        return {
            "4242": "   77 kubectl port-forward svc/web 18080:3000\n",
            "77": "1 dash\n",
        }[pid]

    monkeypatch.setattr(module, "_run", run)
    owner = port_owner(18080, proc=Path("/nonexistent"))
    assert owner == PortOwner(
        18080, 4242, "kubectl port-forward svc/web 18080:3000", ProcessInfo(77, "dash")
    )
    assert calls[0] == ["/bin/lsof", "-nP", "-iTCP:18080", "-sTCP:LISTEN", "-Fpc"]


def test_lookup_degrades_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    monkeypatch.setattr(module.os.path, "exists", lambda path: False)
    assert port_owner(18080, proc=Path("/nonexistent")) is None
    monkeypatch.setattr(module, "_lsof_owner", lambda port: 1 / 0)
    assert port_owner(18080, proc=Path("/nonexistent")) is None


def test_long_commands_are_truncated() -> None:
    assert module._truncate("a  b\n c") == "a b c"
    long = module._truncate("x" * (MAX_COMMAND + 50))
    assert long is not None and len(long) == MAX_COMMAND and long.endswith("…")
    assert module._truncate("") is None


def test_real_listener_is_this_process() -> None:
    has_tool = sys.platform.startswith("linux") or shutil.which("lsof")
    if not has_tool:
        pytest.skip("no /proc and no lsof on this system")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        owner = port_owner(listener.getsockname()[1])
    assert owner is not None
    assert owner.pid == os.getpid()
