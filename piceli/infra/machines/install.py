"""The OS hand-off: run a server's install :class:`~piceli.infra.Hook`, with approval.

Piceli does not own the operating system. After ``piceli infra apply`` a
server has its addresses and id (``inventory.json``); ``piceli infra install
REF SERVER`` renders the hook's command with them and prints it with its
digest (exit 3). ``--approve DIGEST`` runs exactly that command: no shell,
the caller's working directory and environment minus every credential
Piceli knows of, its output on stderr (stdout stays one JSON object). The
outcome is recorded in ``installs.json``.

Importing this module is side-effect free.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from piceli.infra.machines.model import InfraError, Infrastructure, Server
from piceli.infra.machines.state import read_record, write_record

__all__ = ["InstallPreview", "preview_install", "run_install", "server_values"]

INSTALLS = "installs.json"
#: Never handed to a hook (OpenTofu's and Piceli's secret variables).
SCRUBBED = ("TF_ENCRYPTION", "TF_HTTP_PASSWORD", "HCLOUD_TOKEN")


def server_values(
    infra: Infrastructure, directory: Path, server: Server
) -> dict[str, str]:
    """``name``, ``id``, ``ipv4``, ``ipv6`` and ``address`` of an applied server."""
    from piceli.infra.machines.plan import INVENTORY

    found = (read_record(directory, INVENTORY).get("servers") or {}).get(server.name)
    if not isinstance(found, dict) or not found.get("id"):
        raise InfraError(
            "infra-not-applied",
            f"server {server.name} has not been created; run piceli infra apply first",
        )
    values = {"name": server.name}
    for key in ("id", "ipv4", "ipv6"):
        if isinstance(found.get(key), str) and found[key]:
            values[key] = found[key]
    address = server.ssh.address or values.get("ipv4") or values.get("ipv6")
    if address:
        values["address"] = address
    return values


@dataclass(frozen=True)
class InstallPreview:
    infra: str
    server: str
    argv: tuple[str, ...]
    timeout: int
    digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "infra": self.infra,
            "server": self.server,
            "argv": list(self.argv),
            "timeout": self.timeout,
            "digest": self.digest,
        }


def preview_install(
    infra: Infrastructure, directory: Path, name: str
) -> InstallPreview:
    """The rendered hook of server ``name`` and its approval digest."""
    server = infra.server(name)
    if server is None:
        raise InfraError(
            "infra-server-unknown", f"no server named {name!r} is declared"
        )
    if server.install is None:
        raise InfraError(
            "infra-install-unavailable", f"server {name} declares no install hook"
        )
    argv = tuple(server.install.render(server_values(infra, directory, server)))
    body = json.dumps(
        {
            "infra": infra.name,
            "server": name,
            "argv": argv,
            "timeout": server.install.timeout,
        },
        sort_keys=True,
    )
    digest = "sha256:" + hashlib.sha256(body.encode()).hexdigest()
    return InstallPreview(infra.name, name, argv, server.install.timeout, digest)


def run_install(
    infra: Infrastructure, directory: Path, preview: InstallPreview
) -> dict[str, Any]:
    """Run the approved hook; record and return its outcome."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in SCRUBBED and not k.startswith("PICELI_")
    }
    started = time.time()
    state = "installed"
    code: int | None = None
    try:
        target: int | None = sys.stderr.fileno()
    except (AttributeError, OSError, ValueError):
        target = None  # stderr is not a file (embedded use): collect, then copy
    try:
        process = subprocess.Popen(
            list(preview.argv),
            env=env,
            stdout=target if target is not None else subprocess.PIPE,
            stderr=None if target is not None else subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError:
        raise InfraError(
            "infra-install-failed",
            "the install command could not be started (not found?)",
        ) from None
    try:
        output, _ = process.communicate(timeout=preview.timeout)
        code = process.returncode
    except subprocess.TimeoutExpired:
        _stop(process)
        output = b""
        state = "timed-out"
    except BaseException:
        _stop(process)
        raise
    if output:
        sys.stderr.write(output.decode("utf-8", "replace"))
    if state == "installed" and code != 0:
        state = "failed"
    outcome = {
        "state": state,
        "exit_code": code,
        "digest": preview.digest,
        "seconds": round(time.time() - started, 1),
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    record = read_record(directory, INSTALLS)
    record[preview.server] = outcome
    write_record(directory, INSTALLS, record)
    return outcome


def _stop(process: subprocess.Popen[Any]) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except OSError:
            return
        try:
            process.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            continue
