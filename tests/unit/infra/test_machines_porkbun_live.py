"""Porkbun: plan, apply and destroy one TXT record at a real domain (opt-in).

Skipped unless ``PICELI_PORKBUN_LIVE=1``. It creates one TXT record under
``piceli-live`` in the test domain and always destroys it (``finally``),
also when an assertion fails; the plan checks the domain's API access. DNS at the registrar is free. Environment:

- ``PICELI_PORKBUN_DOMAIN``: a domain at Porkbun with API access on;
- ``PICELI_PORKBUN_API_KEY`` and ``PICELI_PORKBUN_SECRET_KEY`` (stored in
  the test's own credentials directory).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from piceli.infra import DnsRecord, Infrastructure, Porkbun
from piceli.infra.machines import credentials

from .machines_support import PASSPHRASE

LIVE = os.environ.get("PICELI_PORKBUN_LIVE") == "1"
pytestmark = [
    pytest.mark.tofu,
    pytest.mark.skipif(not LIVE, reason="set PICELI_PORKBUN_LIVE=1 (creates a record)"),
    pytest.mark.timeout(600),
]


def _env(name: str) -> str:
    value = os.environ.get(f"PICELI_PORKBUN_{name}")
    if not value:
        pytest.fail(f"PICELI_PORKBUN_{name} is required with PICELI_PORKBUN_LIVE=1")
    return value


def test_apply_and_destroy_a_txt_record(homes: Path) -> None:
    from piceli.infra.machines.plan import apply, plan, workspace

    key, secret = _env("API_KEY"), _env("SECRET_KEY")
    credentials.save_credential("live-state", "state-key", PASSPHRASE)
    credentials.save_credential("porkbun-live", "provider-token", f"{key}:{secret}")
    dns = Porkbun(credentials="porkbun-live")
    record = DnsRecord(
        _env("DOMAIN"), "piceli-live", "TXT", value="piceli live test", provider=dns
    )
    infra = Infrastructure("live", records=[record], state_dir=homes / "state")
    try:
        with workspace(infra) as ws:
            planned = plan(ws)
            assert [c.kind for c in planned.changes] == ["record"]
            # The plan asked Porkbun whether the domain has API access.
            (check,) = planned.to_dict()["checks"]
            assert check == {"check": "porkbun-api-access", "ran": True,
                             "domains": {_env("DOMAIN"): "enabled"}}  # fmt: skip
            _, inventory = apply(ws, planned.plan_hash)
        assert [r["kind"] for r in inventory["resources"]] == ["record"]
        for path in (homes / "state").rglob("*"):
            if path.is_file() and ".terraform" not in path.parts:
                body = path.read_bytes()
                assert secret.encode() not in body and key.encode() not in body, path
    finally:
        with workspace(infra) as ws:
            doomed = plan(ws, destroy=True)
            if doomed.changes:
                apply(ws, doomed.plan_hash, destroy=True)
