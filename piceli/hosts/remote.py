"""Talk to a host agent over SSH; decide a wait (0.24.0).

No Kubernetes and no cache access: ``ssh`` (``BatchMode``, strict host key
checking) runs ``cat`` of the status file, so ``piceli host status`` and
``piceli host wait`` work with every cluster down. :func:`control` runs the
agent once for ``piceli host pin|unpin|rollback``; :func:`age_key_state`
and :func:`write_age_key` serve ``piceli host secrets`` (the key goes on
ssh's stdin, never in an argument). Importing this module is side-effect
free.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from piceli.hosts.model import STATE_DIR, HostError, SshTarget

__all__ = [
    "age_key_state",
    "control",
    "matches",
    "outcome",
    "read_status",
    "ssh_argv",
    "write_age_key",
]

STATUS_PATH = f"{STATE_DIR}/status.json"
#: The agent's command, on the PATH of every system with its module.
AGENT = "/run/current-system/sw/bin/piceli-host-agent"
#: A pin may fetch a whole system: the agent's realise and switch limits.
CONTROL_SECONDS = 4 * 3600
_HEX = re.compile(r"[0-9a-f]{7,64}")
_LIMIT = 4 << 20


def ssh_argv(
    target: SshTarget,
    known_hosts: Path | None = None,
    remote: list[str] | None = None,
) -> list[str]:
    """``ssh`` running ``remote`` (default: reading the status file): no
    prompt, a known host key only; as root, else through ``sudo -n``."""
    ssh = shutil.which("ssh")
    if ssh is None:
        raise HostError("host-unreachable", "ssh was not found on PATH")
    files = ["~/.ssh/known_hosts", *([str(known_hosts)] if known_hosts else [])]
    words = remote or ["cat", "--", STATUS_PATH]
    if remote is not None and target.user != "root":
        words = ["sudo", "-n", *words]
    return [
        ssh,
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={' '.join(files)}",
        "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=30",
        "-p", str(target.port),
        "-l", target.user,
        "--", target.address,
        *words,
    ]  # fmt: skip


def _ssh(
    target: SshTarget,
    remote: list[str] | None,
    *,
    known_hosts: Path | None,
    runner: Callable[..., Any],
    timeout: float,
    data: bytes | None = None,
) -> Any:
    try:
        done = runner(
            ssh_argv(target, known_hosts, remote),
            **({"input": data} if data is not None else {"stdin": subprocess.DEVNULL}),
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise HostError("host-unreachable", f"ssh to {target.address} failed") from None
    if done.returncode == 255:
        raise HostError(
            "host-unreachable",
            f"ssh to {target.address} failed (network, host key or login)",
        )
    return done


def control(
    target: SshTarget,
    action: str,
    tag: str | None = None,
    *,
    known_hosts: Path | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Run ``piceli-host-agent ACTION [TAG]`` once on the host; its result."""
    done = _ssh(
        target, [AGENT, action, *([tag] if tag else [])], known_hosts=known_hosts,
        runner=runner, timeout=CONTROL_SECONDS,
    )  # fmt: skip
    value: Any = None
    if done.returncode == 0 and done.stdout:
        try:
            value = json.loads(done.stdout[:_LIMIT].decode("utf-8", "replace"))
        except ValueError:
            value = None
    if not isinstance(value, dict) or value.get("action") != action:
        raise HostError(
            "host-control-unsupported",
            f"{target.address} did not run piceli-host-agent {action}: render the "
            "module with this Piceli and switch once",
        )
    return value


def _key_line(identity: str) -> str:
    return hashlib.sha256(identity.strip().encode()).hexdigest()


def age_key_state(
    target: SshTarget,
    path: str,
    identity: str,
    *,
    known_hosts: Path | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> str:
    """``same``, ``different`` or ``absent``: what the host's key file holds.

    The host prints the sha256 of the file's ``AGE-SECRET-KEY-1`` line (a
    fingerprint, never the key), compared here with the identity's."""
    script = (
        f"if [ -e {shlex.quote(path)} ]; then "
        f"grep -o 'AGE-SECRET-KEY-1[0-9A-Z]*' {shlex.quote(path)} | head -n 1 "
        "| tr -d '\\n' | sha256sum | cut -d ' ' -f 1; else echo absent; fi"
    )
    done = _ssh(
        target, ["sh", "-c", shlex.quote(script)], known_hosts=known_hosts,
        runner=runner, timeout=60,
    )  # fmt: skip
    found = (done.stdout or b"").decode("ascii", "replace").strip()
    if done.returncode != 0 or not re.fullmatch(r"absent|[0-9a-f]{64}", found):
        raise HostError("host-secret-write-failed", "the key file could not be read")
    if found == "absent":
        return "absent"
    return "same" if found == _key_line(identity) else "different"


def write_age_key(
    target: SshTarget,
    path: str,
    content: bytes,
    *,
    known_hosts: Path | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> None:
    """Write ``content`` (on stdin) to ``path``: a temporary file in its
    directory, ``0600`` root, renamed over it."""
    quoted = shlex.quote(path)
    script = (
        "set -eu; umask 077; "
        f'd=$(dirname {quoted}); mkdir -p "$d"; '
        't=$(mktemp "$d/.piceli-age.XXXXXX"); trap \'rm -f "$t"\' EXIT; '
        'cat > "$t"; chmod 0600 "$t"; chown 0:0 "$t"; '
        f'mv -f "$t" {quoted}; trap - EXIT'
    )
    done = _ssh(
        target, ["sh", "-c", shlex.quote(script)], known_hosts=known_hosts,
        runner=runner, timeout=120, data=content,
    )  # fmt: skip
    if done.returncode != 0:
        raise HostError("host-secret-write-failed", "writing the key file failed")


def activate(
    target: SshTarget,
    *,
    known_hosts: Path | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> bool:
    """Re-run the current system's activation (``switch-to-configuration
    switch``), so sops-nix decrypts with the new key."""
    done = _ssh(
        target, ["/run/current-system/bin/switch-to-configuration", "switch"],
        known_hosts=known_hosts, runner=runner, timeout=1800,
    )  # fmt: skip
    return bool(done.returncode == 0)


def read_status(
    target: SshTarget,
    *,
    known_hosts: Path | None = None,
    runner: Callable[..., Any] = subprocess.run,
    timeout: float = 60,
) -> dict[str, Any]:
    """The agent's status (``host-unreachable``, ``host-status-missing``)."""
    done = _ssh(
        target, None, known_hosts=known_hosts, runner=runner, timeout=timeout
    )  # the status file is readable by anyone: no sudo
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


def _published(entry: Any) -> str:
    return str(entry.get("published_at") or "") if isinstance(entry, Mapping) else ""


def outcome(
    status: Mapping[str, Any], rev: str, published_after: str | None = None
) -> tuple[str, str | None]:
    """``("deployed"|"failed"|"waiting", reason)`` of ``rev`` on the host.

    Only the newest publication of ``rev`` counts: the newest ``published_at``
    the host has seen for it (its last poll, an attempt in progress, its
    attempts) or ``published_after``, when it is newer. A failure of an older
    publication is pending until the agent records an attempt of that one.
    """
    if matches(status.get("current"), rev):
        return "deployed", None
    hold = status.get("hold")
    if isinstance(hold, Mapping) and hold.get("kind"):
        # Pinned or rolled back: it moves to nothing else until unpinned.
        return "failed", "host-pinned"
    attempt, poll = status.get("last_attempt"), status.get("poll")
    history = status.get("history")
    seen = [
        _published(entry)
        for entry in (poll, status.get("in_progress"), attempt,
                      *(history if isinstance(history, list) else ()))
        if matches(entry, rev)
    ]  # fmt: skip
    newest = max([published_after or "", *seen])
    if (
        matches(attempt, rev)
        and isinstance(attempt, Mapping)
        and _published(attempt) >= newest
    ):
        state = attempt.get("state")
        if state == "rolled-back":
            return "failed", "host-rolled-back"
        if state in ("failed", "rejected"):
            return "failed", "host-deploy-failed"
    if (
        isinstance(poll, Mapping)
        and poll.get("state") == "skipped"
        and matches(poll, rev)
        and _published(poll) >= newest
    ):
        return "failed", "host-deploy-failed"
    return "waiting", None
