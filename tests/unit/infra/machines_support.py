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
