"""Ownership: a plan may only change or delete what Piceli created; Hetzner prices."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from piceli.infra import Hetzner, Infrastructure, PrimaryIp, Server
from piceli.infra.machines.hetzner import parse_prices
from piceli.infra.machines.plan import INVENTORY, Workspace, changes_of, estimate
from piceli.infra.machines.render import render
from piceli.infra.machines.state import write_record
from piceli.testing.infra import PRICING, mock_hetzner_api

from .machines_support import TOKEN

OURS = {"piceli.io/managed-by": "piceli", "piceli.io/infra": "edge"}


def _workspace(tmp_path: Path, provider: Hetzner | None = None) -> Workspace:
    hcloud = provider or Hetzner(credentials="hcloud", location="fsn1")
    infra = Infrastructure(
        "edge",
        servers=[
            Server(
                "edge-1",
                provider=hcloud,
                type="cax11",
                image="debian-12",
                ipv4=PrimaryIp("edge-1-v4"),
            )
        ],
        state_dir=tmp_path,
    )
    return Workspace(infra, tmp_path, None, render(infra), {})  # type: ignore[arg-type]


def _change(
    address: str, actions: list[str], labels: dict[str, str] | None
) -> dict[str, Any]:
    before = None if labels is None else {"labels": labels, "name": "x"}
    return {
        "address": address,
        "mode": "managed",
        "change": {"actions": actions, "before": before},
    }


def _ledger(tmp_path: Path, *addresses: str) -> None:
    write_record(tmp_path, INVENTORY, {"created": list(addresses)})


SERVER = "hcloud_server.server-edge-1"
IP = "hcloud_primary_ip.ip-edge-1-v4"


def test_creates_are_always_allowed(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    changes, foreign = changes_of(
        ws, {"resource_changes": [_change(SERVER, ["create"], None)]}
    )
    assert [c.action for c in changes] == ["create"] and foreign == ()


def test_deleting_what_piceli_created_is_allowed(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    _ledger(tmp_path, SERVER, IP)
    plan = {
        "resource_changes": [
            _change(SERVER, ["delete"], OURS),
            _change(IP, ["delete", "create"], OURS),
        ]
    }
    changes, foreign = changes_of(ws, plan)
    assert {c.action for c in changes} == {"delete", "replace"}
    assert foreign == ()


def test_a_resource_outside_the_ledger_is_foreign(tmp_path: Path) -> None:
    """Imported into the state by hand: labelled like ours, never created by Piceli."""
    ws = _workspace(tmp_path)
    _ledger(tmp_path, IP)
    changes, foreign = changes_of(
        ws, {"resource_changes": [_change(SERVER, ["delete"], OURS)]}
    )
    assert foreign == (SERVER,)


@pytest.mark.parametrize(
    "labels",
    [
        None,
        {},
        {"piceli.io/managed-by": "someone-else", "piceli.io/infra": "edge"},
        {"piceli.io/managed-by": "piceli", "piceli.io/infra": "other-infra"},
    ],
    ids=["no-state", "no-labels", "other-manager", "other-infra"],
)
def test_a_resource_without_our_labels_is_foreign(
    tmp_path: Path, labels: dict[str, str] | None
) -> None:
    ws = _workspace(tmp_path)
    _ledger(tmp_path, SERVER)
    for actions in (["delete"], ["update"], ["delete", "create"]):
        _, foreign = changes_of(
            ws, {"resource_changes": [_change(SERVER, actions, labels)]}
        )
        assert foreign == (SERVER,), actions


def test_an_unknown_address_is_foreign(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    _ledger(tmp_path, "hcloud_server.someone-elses")
    _, foreign = changes_of(
        ws,
        {
            "resource_changes": [
                _change("hcloud_server.someone-elses", ["delete"], OURS)
            ]
        },
    )
    assert foreign == ("hcloud_server.someone-elses",)


def test_no_ops_and_reads_are_not_changes(tmp_path: Path) -> None:
    ws = _workspace(tmp_path)
    changes, foreign = changes_of(
        ws,
        {
            "resource_changes": [
                _change(SERVER, ["no-op"], {}),
                {
                    "address": "data.x.y",
                    "mode": "data",
                    "change": {"actions": ["read"]},
                },
            ]
        },
    )
    assert changes == () and foreign == ()


def test_prices_parse_from_the_api_shape() -> None:
    prices = parse_prices(PRICING)
    assert prices is not None
    assert prices.server("cax11", "fsn1") == pytest.approx(3.79)
    assert prices.primary_ip("ipv4", "hel1") == pytest.approx(0.5)
    assert prices.server("cax11", "nowhere") is None
    assert parse_prices({"nope": 1}) is None


def test_estimate_from_the_mock_api_sends_the_token_as_a_header_only(
    tmp_path: Path,
) -> None:
    with mock_hetzner_api(TOKEN) as api:
        provider = Hetzner(credentials="hcloud", location="fsn1", endpoint=api.url)
        ws = _workspace(tmp_path, provider)
        ws.tokens = {"hetzner": TOKEN}
        result = estimate(ws)
        assert api.requests == [("GET", "/v1/pricing", True)]
        assert all(TOKEN not in path for _, path, _ in api.requests)
        ws.tokens = {"hetzner": "wrong"}
        unknown = estimate(ws)
    assert result is not None
    assert result["currency"] == "EUR" and result["complete"] is True
    assert result["monthly_net"] == pytest.approx(3.79 + 0.5)
    assert unknown is None  # unauthorized: no prices, never an error
