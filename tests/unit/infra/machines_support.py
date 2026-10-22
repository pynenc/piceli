"""Shared helpers of the ``piceli infra`` tests: scratch homes, a real tofu if any."""

from __future__ import annotations

import json
import os
import shutil
import textwrap
from pathlib import Path
from typing import Any

import pytest

#: A state passphrase and a provider token used only by these tests.
PASSPHRASE = 'test-passphrase-$${not}-"quoted"-\\x'
TOKEN = "hcloud-test-token-" + "z" * 46


def tofu_binary() -> str | None:
    """``$PICELI_TOFU`` or ``tofu`` on PATH (real OpenTofu tests skip without it)."""
    found = os.environ.get("PICELI_TOFU") or shutil.which("tofu")
    return found if found and Path(found).is_file() else None


needs_tofu = pytest.mark.skipif(
    tofu_binary() is None,
    reason="OpenTofu not found (set PICELI_TOFU or put tofu on PATH, e.g. nix shell nixpkgs#opentofu)",
)


MODULE = """
from pathlib import Path

from piceli.infra import Cluster, DnsRecord, Hook, Infrastructure, PrimaryIp, Rule, Server
from piceli.testing.infra import FakeProvider

HERE = Path(__file__).parent
fake = FakeProvider(addresses={{"edge-1": "127.0.0.1"}}, monthly={{"t1": 3.79}})
cluster = Cluster("edge-1", api={api!r}, credentials="edge-1")
edge_1 = Server(
    "edge-1", provider=fake, type="t1", image="img", ipv4=PrimaryIp("edge-1-v4"),
    firewall=[Rule.tcp(443), Rule.udp(3478)],
    install=Hook({hook!r}),
    cluster=cluster,
)
servers = [edge_1]
{extra}
infra = Infrastructure(
    "edge", servers=servers,
    records=[DnsRecord("example.com", "www", "A", server=edge_1)],
    state_dir=HERE / "state",
)
"""


def write_module(
    directory: Path,
    *,
    api: str = "https://127.0.0.1:6443",
    hook: list[str] | None = None,
    extra: str = "",
) -> Path:
    path = directory / "machines.py"
    path.write_text(
        textwrap.dedent(
            MODULE.format(
                api=api,
                hook=hook or ["sh", "-c", "echo installing {name} at {ipv4}"],
                extra=extra,
            )
        )
    )
    return path


def last_json(text: str) -> dict[str, Any]:
    """The last JSON object line of ``text``."""
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            value = json.loads(line)
            assert isinstance(value, dict)
            return value
    raise AssertionError(f"no JSON object in {text!r}")


def hook(kind: str, address: str, action: str, id_value: str | None = None) -> str:
    """One line of ``tofu apply -json``: an ``apply_complete`` or ``apply_errored``."""
    body: dict[str, Any] = {"resource": {"addr": address}, "action": action}
    if id_value is not None:
        body.update(id_key="id", id_value=id_value)
    return json.dumps({"type": kind, "hook": body})


def tofu_error(summary: str, detail: str = "") -> str:
    """One error diagnostic line of ``tofu ... -json``."""
    return json.dumps(
        {"type": "diagnostic",
         "diagnostic": {"severity": "error", "summary": summary, "detail": detail}}
    )  # fmt: skip


class ScriptedTofu:
    """A ``Tofu`` stand-in: fixed plan and state documents, a scripted apply."""

    version_text = "1.13.1"

    def __init__(
        self,
        plan: dict[str, Any],
        *,
        apply_lines: list[str] = (),  # type: ignore[assignment]
        apply_code: int = 0,
        state: dict[str, Any] | None = None,
    ) -> None:
        self.plan, self.state = plan, state or {}
        self.apply_lines, self.apply_code = list(apply_lines), apply_code
        self.calls: list[list[str]] = []

    def run(self, args: Any, *, cwd: Path, env: Any, timeout: float = 0) -> Any:
        from piceli.infra.machines.tofu import TofuResult

        args = list(args)
        self.calls.append(args)
        if args[0] == "plan":
            out = next(a for a in args if a.startswith("-out="))
            Path(out.removeprefix("-out=")).write_text("plan")
        if args[0] == "show":
            document = self.plan if len(args) == 3 else self.state
            return TofuResult(0, json.dumps(document), "")
        if args[0] == "apply":
            return TofuResult(self.apply_code, "\n".join(self.apply_lines), "")
        return TofuResult(0, "", "")


def planned(
    *changes: tuple[str, str, dict[str, Any] | None], infra: str = "edge"
) -> dict[str, Any]:
    """A ``tofu show -json PLAN`` document: (address, action, values) per change.

    A change to an existing resource has ``before`` with the labels of
    ``infra`` (the ledger must still list it); a delete has no ``after``.
    """
    labels = {"piceli.io/managed-by": "piceli", "piceli.io/infra": infra}
    found = []
    for address, action, values in changes:
        actions = ["delete", "create"] if action == "replace" else [action]
        before = (
            None
            if action == "create"
            else {"input": {"labels": labels}, **(values or {})}
        )
        after = None if action == "delete" else values
        found.append(
            {"address": address, "mode": "managed", "type": address.split(".")[0],
             "change": {"actions": actions, "before": before, "after": after}}
        )  # fmt: skip
    return {"format_version": "1.2", "resource_changes": found}
