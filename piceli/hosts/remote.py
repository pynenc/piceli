"""Read a host agent's status over SSH; decide a wait (0.24.0).

No Kubernetes and no cache access: ``ssh`` (``BatchMode``, strict host key
checking) runs ``cat`` of the status file, so ``piceli host status`` and
``piceli host wait`` work with every cluster down. Importing this module is
side-effect free.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from piceli.hosts.model import STATE_DIR, HostError, SshTarget

__all__ = ["matches", "outcome", "read_status", "ssh_argv"]

STATUS_PATH = f"{STATE_DIR}/status.json"
_HEX = re.compile(r"[0-9a-f]{7,64}")
_LIMIT = 4 << 20


def ssh_argv(target: SshTarget, known_hosts: Path | None = None) -> list[str]:
    """``ssh`` reading the status file: no prompt, a known host key only."""
    ssh = shutil.which("ssh")
    if ssh is None:
        raise HostError("host-unreachable", "ssh was not found on PATH")
    files = ["~/.ssh/known_hosts", *([str(known_hosts)] if known_hosts else [])]
    return [
        ssh,
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={' '.join(files)}",
        "-o", "ConnectTimeout=10",
        "-p", str(target.port),
        "-l", target.user,
        "--", target.address,
        "cat", "--", STATUS_PATH,
    ]  # fmt: skip


def read_status(
    target: SshTarget,
    *,
    known_hosts: Path | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """The agent's status (``host-unreachable``, ``host-status-missing``)."""
    try:
        done = runner(
            ssh_argv(target, known_hosts),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise HostError("host-unreachable", f"ssh to {target.address} failed") from None
    if done.returncode == 255:
        raise HostError(
            "host-unreachable",
            f"ssh to {target.address} failed (network, host key or login)",
        )
    if done.returncode != 0 or not done.stdout:
        raise HostError(
            "host-status-missing",
            f"{target.address} has no {STATUS_PATH}: is the agent's module imported?",
        )
    try:
        value = json.loads(done.stdout[:_LIMIT])
    except ValueError:
        value = None
    if not isinstance(value, dict):
        raise HostError("host-status-missing", "the status file is not JSON")
    return value


def matches(entry: Mapping[str, Any] | None, rev: str) -> bool:
    """Whether a build (``current``, an attempt) is ``rev``: a commit prefix
    of 7+ hex digits, or a tag."""
    if not isinstance(entry, Mapping):
        return False
    commit = str(entry.get("commit") or "")
    if _HEX.fullmatch(rev) and commit.startswith(rev):
        return True
    return entry.get("tag") == rev


def outcome(status: Mapping[str, Any], rev: str) -> tuple[str, str | None]:
    """``("deployed"|"failed"|"waiting", reason)`` of ``rev`` on the host."""
    if matches(status.get("current"), rev):
        return "deployed", None
    attempt = status.get("last_attempt")
    if matches(attempt, rev) and isinstance(attempt, Mapping):
        state = attempt.get("state")
        if state == "rolled-back":
            return "failed", "host-rolled-back"
        if state in ("failed", "rejected"):
            return "failed", "host-deploy-failed"
    poll = status.get("poll")
    if (
        isinstance(poll, Mapping)
        and poll.get("state") == "skipped"
        and matches(poll, rev)
    ):
        return "failed", "host-deploy-failed"
    return "waiting", None
