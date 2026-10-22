"""Plan-time DNS conflicts at Porkbun (0.24.0).

An approved plan must not fail later for a reason the plan could see. The
zone holds records Piceli did not create and never touches (a parking
ALIAS at the apex, a wildcard, an A record added by hand). When one sits
where the plan creates a record, the plan fails with
``infra-dns-record-conflict``; a wildcard is only a warning. Porkbun's API
is an injected transport: the real one is never called.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from piceli.infra.machines import porkbun
from piceli.infra.machines.model import InfraError
from piceli.infra.machines.plan import INVENTORY, plan
from piceli.infra.machines.state import write_record

from .test_machines_porkbun import API_KEY, CREDENTIAL, SECRET_KEY, _answers, _planning

ZONE = "example.com"
PARKING = [
    {"id": "901", "name": "example.com", "type": "ALIAS",
     "content": "pixie.porkbun.com", "ttl": "600"},
    {"id": "902", "name": "*.example.com", "type": "CNAME",
     "content": "pixie.porkbun.com", "ttl": "600"},
    {"id": "903", "name": "example.com", "type": "NS",
     "content": "curitiba.ns.porkbun.com", "ttl": "86400"},
]  # fmt: skip


def _answer(records: list[dict[str, Any]]) -> tuple[int, bytes]:
    return 200, json.dumps({"status": "SUCCESS", "records": records}).encode()


def _change(
    address: str,
    action: str,
    name: str,
    type_: str,
    content: str | None = None,
    record_id: str | None = None,
) -> dict[str, Any]:
    values = {"domain": ZONE, "subdomain": name, "type": type_, "ttl": 600}
    if content is not None:
        values["content"] = content
    before = {**values, "id": record_id} if action != "create" else None
    actions = {"replace": ["delete", "create"]}.get(action, [action])
    return {
        "address": f"porkbun_dns_record.{address}",
        "mode": "managed",
        "type": "porkbun_dns_record",
        "change": {
            "actions": actions,
            "before": before,
            "after": None if action == "delete" else values,
        },
    }


def _check(
    changes: list[dict[str, Any]],
    records: list[dict[str, Any]],
    ledger: list[str] | None = None,
) -> Any:
    document = {"resource_changes": changes}
    owned = (
        {c["address"] for c in changes if c["change"]["before"]}
        if ledger is None
        else set(ledger)
    )
    return porkbun.Porkbun(credentials="porkbun").plan_check(
        document,
        CREDENTIAL,
        ledger=frozenset(owned),
        post=_answers(example_com=_answer(records)),
    )


def _refused(*args: Any, **kwargs: Any) -> InfraError:
    with pytest.raises(InfraError) as caught:
        _check(*args, **kwargs)
    assert caught.value.code == "infra-dns-record-conflict"
    text = str(caught.value) + json.dumps(caught.value.fields)
    assert API_KEY not in text and SECRET_KEY not in text
    return caught.value


def test_a_parking_alias_at_the_apex_fails_the_plan() -> None:
    error = _refused(
        [_change("apex-a", "create", "", "A"),
         _change("www-a", "create", "www", "A")],
        PARKING,
    )  # fmt: skip
    assert error.fields["conflicts"] == [
        {"record": {"type": "ALIAS", "name": "example.com",
                    "content": "pixie.porkbun.com"},
         "planned": {"type": "A", "name": "example.com"},
         "why": "a CNAME or ALIAS allows no other record at its name"},
    ]  # fmt: skip
    message = str(error)
    assert "ALIAS example.com pixie.porkbun.com" in message
    assert "remove or change" in message and "never touches" in message
    # The wildcard and the apex NS are no conflict: www is an explicit name.
    assert "*.example.com" not in json.dumps(error.fields["conflicts"])


def test_any_record_where_piceli_creates_a_cname_conflicts() -> None:
    error = _refused(
        [_change("app-cname", "create", "app", "CNAME", "web.example.net")],
        [{"id": "7", "name": "app.example.com", "type": "TXT",
          "content": "verify=abc"}],
    )  # fmt: skip
    assert error.fields["conflicts"][0]["record"]["type"] == "TXT"
    assert error.fields["conflicts"][0]["why"] == (
        "a CNAME allows no other record at its name"
    )


def test_the_same_name_and_type_conflicts_unless_piceli_owns_it() -> None:
    records = [{"id": "11", "name": "www.example.com", "type": "A",
                "content": "192.0.2.10"}]  # fmt: skip
    error = _refused([_change("www-a", "create", "www", "A")], records)
    assert error.fields["conflicts"][0]["record"] == {
        "type": "A", "name": "www.example.com", "content": "192.0.2.10",
    }  # fmt: skip
    # Piceli's own record, replaced or kept beside a new one: no conflict.
    for changes in (
        [_change("www-a", "replace", "www", "A", "192.0.2.10", "11")],
        [_change("www-old", "delete", "www", "A", "192.0.2.10", "11"),
         _change("www-a", "create", "www", "A")],
        [_change("www-1", "no-op", "www", "A", "192.0.2.10", "11"),
         _change("www-2", "create", "www", "A")],
    ):  # fmt: skip
        check = _check(changes, records)
        assert check[1]["conflicts"] == [] and check[1]["foreign"] == 0
    # The same record, but its address is not in the ledger: not Piceli's.
    _refused(
        [_change("www-1", "no-op", "www", "A", "192.0.2.10", "11"),
         _change("www-2", "create", "www", "A")],
        records,
        ledger=[],
    )  # fmt: skip


def test_txt_records_conflict_only_with_the_same_content() -> None:
    records = [{"id": "21", "name": "example.com", "type": "TXT",
                "content": "google-site-verification=xyz"}]  # fmt: skip
    check = _check([_change("spf", "create", "", "TXT", "v=spf1 -all")], records)
    assert check[1]["conflicts"] == []
    (warning,) = check[1]["warnings"]
    assert warning["kind"] == "txt-beside"
    assert warning["record"]["content"] == "google-site-verification=xyz"
    same = [{**records[0], "content": "v=spf1 -all"}]
    error = _refused([_change("spf", "create", "", "TXT", "v=spf1 -all")], same)
    assert "same name, type and content" in error.fields["conflicts"][0]["why"]


def test_a_wildcard_is_a_warning_not_a_failure() -> None:
    check = _check([_change("www-a", "create", "www", "A")], PARKING[1:])
    access, records = check
    assert access == {"check": "porkbun-api-access", "ran": True,
                      "domains": {"example.com": "enabled"}}  # fmt: skip
    assert records["check"] == "porkbun-record-conflicts" and records["ran"]
    assert records["creates"] == 1 and records["foreign"] == 2
    assert records["conflicts"] == []
    (warning,) = records["warnings"]
    assert warning["kind"] == "wildcard"
    assert warning["record"] == {"type": "CNAME", "name": "*.example.com",
                                 "content": "pixie.porkbun.com"}  # fmt: skip
    assert warning["covers"] == ["www.example.com"]
    assert "explicit" in warning["why"]


def test_a_plan_fails_on_a_conflict_and_lists_the_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from piceli.k8s.cli import infra as infra_cli

    ws = _planning(tmp_path, {}, porkbun=CREDENTIAL)
    ws.tofu.plan = {  # type: ignore[attr-defined]
        "resource_changes": [
            _change("apex-a", "create", "", "A"),
            _change("www-a", "create", "www", "A"),
        ]
    }
    monkeypatch.setattr(porkbun, "_post", _answers(example_com=_answer(PARKING)))
    with pytest.raises(InfraError) as caught:
        plan(ws)
    assert caught.value.code == "infra-dns-record-conflict"
    assert caught.value.fields["conflicts"][0]["record"]["type"] == "ALIAS"
    with pytest.raises(SystemExit) as exited:
        infra_cli._refuse(caught.value)
    assert exited.value.code == 2
    out, err = capsys.readouterr()
    body = json.loads(out)
    assert body["reason"] == "infra-dns-record-conflict"
    assert body["conflicts"] == caught.value.fields["conflicts"]
    assert "conflict: ALIAS example.com pixie.porkbun.com" in err
    # Without it, the plan passes, the wildcard is a warning in its checks.
    monkeypatch.setattr(porkbun, "_post", _answers(example_com=_answer(PARKING[1:])))
    write_record(tmp_path, INVENTORY, {"created": []})
    passed = plan(ws)
    checks = passed.to_dict()["checks"]
    assert [c["check"] for c in checks] == [
        "porkbun-api-access", "porkbun-record-conflicts",
    ]  # fmt: skip
    assert checks[1]["warnings"][0]["kind"] == "wildcard"
    infra_cli._say_plan(passed)
    err = capsys.readouterr().err
    assert "checked porkbun-record-conflicts" in err
    assert "warning: CNAME *.example.com pixie.porkbun.com" in err
