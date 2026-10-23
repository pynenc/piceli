"""A tofu apply that fails after changing something reports what it applied.

``tofu apply -json`` prints an ``apply_complete`` line per finished change
(its address, action and the provider's id); a failure after one of them is
a partially applied change: exit 1 with ``partially_applied``, also for a
refusal that would otherwise exit 2 (a Porkbun domain without API access).
A failure before anything changed keeps its code and exit status.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from piceli.infra import DnsRecord, Infrastructure, Porkbun, PrimaryIp, Server
from piceli.infra.machines import porkbun
from piceli.infra.machines.model import InfraError
from piceli.infra.machines.plan import INVENTORY, Workspace, apply, plan_hash
from piceli.infra.machines.render import render
from piceli.infra.machines.state import write_record
from piceli.testing.infra import FakeProvider

from .machines_support import ScriptedTofu, hook, planned, tofu_error

IP = "terraform_data.ip-edge-1-v4"
SERVER = "terraform_data.server-edge-1"


def _infra(tmp_path: Path, *, dns: bool = False) -> Infrastructure:
    edge = Server("edge-1", provider=FakeProvider(), type="t1", image="img",
                  ipv4=PrimaryIp("edge-1-v4"))  # fmt: skip
    dns_ = Porkbun(credentials="porkbun")
    records = [DnsRecord("example.com", "www", "A", server=edge, provider=dns_)]
    return Infrastructure(
        "edge", servers=[edge], records=records if dns else [], state_dir=tmp_path
    )


def _record(infra: Infrastructure) -> str:
    return next(a for a in render(infra).resources if a.startswith("porkbun_"))


def _run(
    tmp_path: Path, document: dict[str, Any], lines: list[str], *, dns: bool = False
) -> InfraError:
    infra = _infra(tmp_path, dns=dns)
    rendered = render(infra)
    write_record(tmp_path, INVENTORY, {"created": sorted(rendered.resources)})
    tofu = ScriptedTofu(document, apply_lines=lines, apply_code=1)
    ws = Workspace(infra, tmp_path, tofu, rendered, {}, {"porkbun": "pk1_a:sk1_b"})  # type: ignore[arg-type]
    with pytest.raises(InfraError) as caught:
        apply(ws, plan_hash("apply", rendered, document))
    return caught.value


@pytest.fixture(autouse=True)
def _api_access_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        porkbun,
        "_post",
        lambda url, body, timeout: (200, b'{"status": "SUCCESS", "records": []}'),
    )


def test_a_failure_after_changes_reports_what_was_applied(tmp_path: Path) -> None:
    document = planned((IP, "create", {}), (SERVER, "create", {}))
    error = _run(tmp_path, document, [
        hook("apply_complete", IP, "create", "ip-123"),
        hook("apply_errored", SERVER, "create"),
        tofu_error("Error creating server", "quota exceeded"),
    ])  # fmt: skip
    assert error.code == "infra-apply-failed"
    log = Path(error.fields.pop("tofu_log"))
    assert log.name == "tofu-apply.log" and "quota exceeded" in log.read_text()
    assert error.fields == {
        "failed_on": [],
        "partially_applied": True,
        "applied": [{"address": IP, "action": "create", "id": "ip-123"}],
        "not_applied": [{"address": SERVER, "action": "create", "failed": True}],
    }
    assert error.details == ["Error creating server quota exceeded"]


def test_a_replace_is_applied_only_when_both_halves_finished(tmp_path: Path) -> None:
    document = planned((IP, "replace", {}), (SERVER, "update", {}))
    error = _run(tmp_path, document, [
        hook("apply_complete", IP, "delete"),
        hook("apply_complete", SERVER, "update", "srv-1"),
        tofu_error("Error creating IP"),
    ])  # fmt: skip
    assert error.fields["applied"] == [
        {"address": IP, "action": "delete"},
        {"address": SERVER, "action": "update", "id": "srv-1"},
    ]
    assert error.fields["not_applied"] == [{"address": IP, "action": "replace"}]


def test_a_failure_before_any_change_keeps_its_behaviour(tmp_path: Path) -> None:
    document = planned((IP, "create", {}))
    error = _run(tmp_path, document, [
        hook("apply_errored", IP, "create"), tofu_error("Error creating IP"),
    ])  # fmt: skip
    assert error.code == "infra-apply-failed"
    assert set(error.fields) == {"failed_on", "tofu_log"}  # nothing applied


def test_a_known_refusal_after_changes_is_partial(tmp_path: Path) -> None:
    infra = _infra(tmp_path, dns=True)
    record = _record(infra)
    document = planned(
        (SERVER, "create", {}), (record, "create", {"domain": "example.com"})
    )
    error = _run(tmp_path, document, [
        hook("apply_complete", SERVER, "create", "srv-1"),
        hook("apply_errored", record, "create"),
        tofu_error("Error Creating DNS Record",
                   "400 Domain is not opted in to API access."),
    ], dns=True)  # fmt: skip
    assert error.code == "infra-dns-api-disabled"
    assert error.fields["partially_applied"] is True
    assert error.fields["not_applied"] == [
        {"address": record, "action": "create", "failed": True}
    ]
