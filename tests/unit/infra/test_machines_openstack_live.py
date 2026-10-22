"""A real OpenStack cloud: plan, apply and destroy one small server (opt-in).

Skipped unless ``PICELI_OPENSTACK_LIVE=1``. It creates a security group, a
port on the public network and the smallest server you name, and always
destroys them (``finally``), also when an assertion fails. Costs a few
minutes of the flavor's price. Environment:

- ``PICELI_OPENSTACK_AUTH_URL``, ``PICELI_OPENSTACK_REGION``,
  ``PICELI_OPENSTACK_NETWORK`` (the external network), ``PICELI_OPENSTACK_FLAVOR``,
  ``PICELI_OPENSTACK_IMAGE`` (an image name at the cloud);
- ``PICELI_OPENSTACK_CREDENTIAL_ID`` and ``PICELI_OPENSTACK_CREDENTIAL_SECRET``
  (an application credential; stored in the test's own credentials directory).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from piceli.infra import Infrastructure, OpenStack, PrimaryIp, Rule, Server
from piceli.infra.machines import credentials

from .machines_support import PASSPHRASE, needs_tofu

LIVE = os.environ.get("PICELI_OPENSTACK_LIVE") == "1"
pytestmark = [
    needs_tofu,
    pytest.mark.skipif(
        not LIVE, reason="set PICELI_OPENSTACK_LIVE=1 (creates resources)"
    ),
    pytest.mark.timeout(1800),
]


def _env(name: str) -> str:
    value = os.environ.get(f"PICELI_OPENSTACK_{name}")
    if not value:
        pytest.fail(f"PICELI_OPENSTACK_{name} is required with PICELI_OPENSTACK_LIVE=1")
    return value


def test_apply_and_destroy_a_server_on_a_port(homes: Path) -> None:
    from piceli.infra.machines.plan import apply, plan, workspace

    identifier, secret = _env("CREDENTIAL_ID"), _env("CREDENTIAL_SECRET")
    credentials.save_credential("live-state", "state-key", PASSPHRASE)
    credentials.save_credential(
        "openstack-live", "provider-token", f"{identifier}:{secret}"
    )
    cloud = OpenStack(
        credentials="openstack-live",
        auth_url=_env("AUTH_URL"),
        region=_env("REGION"),
        public_network=_env("NETWORK"),
    )
    server = Server(
        "piceli-live-1",
        provider=cloud,
        type=_env("FLAVOR"),
        image=_env("IMAGE"),
        ipv4=PrimaryIp("piceli-live-1-port"),
        firewall=[Rule.tcp(22, sources=["192.0.2.0/24"])],
    )
    infra = Infrastructure("live", servers=[server], state_dir=homes / "state")
    try:
        with workspace(infra) as ws:
            planned = plan(ws)
            assert {c.kind for c in planned.changes} >= {"server", "ip", "firewall"}
            _, inventory = apply(ws, planned.plan_hash)
        address = inventory["servers"]["piceli-live-1"]["ipv4"]
        assert address
        for path in (homes / "state").rglob("*"):
            if path.is_file() and ".terraform" not in path.parts:
                assert secret.encode() not in path.read_bytes(), path
    finally:
        with workspace(infra) as ws:
            doomed = plan(ws, destroy=True)
            if doomed.changes:
                apply(ws, doomed.plan_hash, destroy=True)
