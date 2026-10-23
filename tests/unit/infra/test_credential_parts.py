"""Credentials with two parts: asked for, checked and stored by part (0.25.0).

A Porkbun key pair and an OpenStack application credential are entered part
by part, each checked before anything is stored; they are stored under their
parts' names. An older ``A:B`` credential still reads; a stored one-part
value for a two-part provider is reported before any API call.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.infra import DnsRecord, Infomaniak, Infrastructure, Porkbun, Server
from piceli.infra.machines import credential_parts as cp
from piceli.infra.machines import credentials
from piceli.infra.machines.model import InfraError
from piceli.k8s.cli import app

API_KEY = "pk1_" + "0a" * 32
SECRET_KEY = "sk1_" + "1b" * 32
CRED_ID = "0123456789abcdef0123456789abcdef"
CRED_SECRET = "app-credential-secret-" + "s" * 40
VALUES = (API_KEY, SECRET_KEY, CRED_ID, CRED_SECRET)


def _run(*args: str, input: str | None = None) -> Any:
    return CliRunner().invoke(app, ["secrets", *args], input=input)


def _no_value(result: Any, *values: str) -> None:
    for value in values or VALUES:
        assert value not in result.stdout + result.stderr


def _stored(name: str) -> dict[str, Any]:
    body = json.loads((credentials.credentials_dir() / f"{name}.json").read_text())
    assert isinstance(body, dict)
    return body


def _rejected(result: Any, reason: str) -> dict[str, Any]:
    assert result.exit_code == 2, result.output
    body = json.loads(result.stdout)
    assert body["state"] == "rejected" and body["reason"] == reason, body
    return dict(body)


# ------------------------------------------------------------- storing


def test_porkbun_is_stored_by_part(homes: Path) -> None:
    result = _run("provider", "porkbun", "--prompt", input=f"{API_KEY}\n{SECRET_KEY}\n")
    assert result.exit_code == 0, result.output
    _no_value(result)
    body = json.loads(result.stdout)
    assert body == {
        "state": "stored",
        "name": "porkbun",
        "kind": "provider-token",
        "provider": "porkbun",
        "parts": ["api_key", "secret_api_key"],
        "replaced": False,
        "path": str(credentials.credentials_dir() / "porkbun.json"),
    }
    assert "API key and secret API key" in result.stderr
    stored = _stored("porkbun")
    assert stored["parts"] == {"api_key": API_KEY, "secret_api_key": SECRET_KEY}
    assert stored["provider"] == "porkbun" and "value" not in stored
    # Consumers that split ``A:B`` keep working.
    assert credentials.load_credential("porkbun", "provider-token") == (
        f"{API_KEY}:{SECRET_KEY}"
    )
    assert cp.load_parts("porkbun", "porkbun") == {
        "api_key": API_KEY,
        "secret_api_key": SECRET_KEY,
    }


def test_infomaniak_is_an_openstack_application_credential(homes: Path) -> None:
    result = _run(
        "provider", "infomaniak", "--prompt", input=f"{CRED_ID}\n{CRED_SECRET}\n"
    )
    assert result.exit_code == 0, result.output
    _no_value(result)
    body = json.loads(result.stdout)
    assert body["provider"] == "openstack"
    assert body["parts"] == [
        "application_credential_id",
        "application_credential_secret",
    ]
    provider = Infomaniak(credentials="infomaniak")
    value = credentials.load_credential("infomaniak", "provider-token")
    assert provider.credential_env(value) == {
        "OS_APPLICATION_CREDENTIAL_ID": CRED_ID,
        "OS_APPLICATION_CREDENTIAL_SECRET": CRED_SECRET,
    }


@pytest.mark.parametrize(
    ("lines", "part", "why"),
    [
        (f"{API_KEY}\n", "secret_api_key", "empty"),
        (f"{API_KEY}:{SECRET_KEY}\n", "api_key", "one part per line"),
        (f"{SECRET_KEY}\n{API_KEY}\n", "api_key", "the secret API key"),
        (f" {API_KEY}\n{SECRET_KEY}\n", "api_key", "space"),
        (f"{API_KEY}\n{API_KEY}\n", "secret_api_key", "sk1_"),
        ("\n", "api_key", "empty"),
    ],
    ids=["one-part", "joined", "swapped", "whitespace", "key-twice", "nothing"],
)
def test_a_wrong_porkbun_part_is_refused_and_nothing_stored(
    homes: Path, lines: str, part: str, why: str
) -> None:
    result = _run("provider", "porkbun", "--prompt", input=lines)
    body = _rejected(result, "infra-credential-part-invalid")
    assert body["part"] == part
    assert why in body["message"]
    _no_value(result)
    assert not credentials.credential_present("porkbun")


def test_an_application_credential_id_is_32_hex(homes: Path) -> None:
    result = _run(
        "provider", "infomaniak", "--prompt", input=f"not-an-id\n{CRED_SECRET}\n"
    )
    body = _rejected(result, "infra-credential-part-invalid")
    assert body["part"] == "application_credential_id"
    assert "32 hex" in body["message"]
    assert not credentials.credential_present("infomaniak")


def test_from_env_reads_the_providers_standard_variables(
    homes: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PORKBUN_API_KEY", API_KEY)
    refused = _run("provider", "porkbun", "--prompt", input="x\ny\n")
    assert "--from-env" in _rejected(refused, "infra-credential-refused")["message"]
    monkeypatch.setenv("PORKBUN_SECRET_API_KEY", SECRET_KEY)
    result = _run("provider", "dns", "--provider", "porkbun", "--from-env")
    assert result.exit_code == 0, result.output
    _no_value(result)
    assert cp.load_parts("dns", "porkbun")["secret_api_key"] == SECRET_KEY
    monkeypatch.delenv("PORKBUN_SECRET_API_KEY")
    missing = _run("provider", "dns", "--provider", "porkbun", "--from-env")
    body = _rejected(missing, "infra-credential-part-invalid")
    assert body["part"] == "secret_api_key"
    assert "PORKBUN_SECRET_API_KEY" in body["message"]
    both = _run("provider", "dns", "--from-env", "--prompt")
    _rejected(both, "infra-prompt-required")


def test_the_provider_is_the_option_the_declaration_or_the_name(
    homes: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert cp.shape_for_name("porkbun").provider == "porkbun"
    assert cp.shape_for_name("porkbun-dns").provider == "porkbun"
    assert cp.shape_for_name("infomaniak").provider == "openstack"
    assert cp.shape_for_name("hcloud").provider == "hetzner"
    assert cp.shape_for_name("dns").provider == "token"
    unknown = _run("provider", "dns", "--prompt", input="a-token-0123456789\n")
    assert unknown.exit_code == 0, unknown.output
    assert json.loads(unknown.stdout)["parts"] == ["token"]
    bad = _run("provider", "dns", "--provider", "nope", "--prompt", input="x\n")
    _rejected(bad, "infra-credential-invalid")
    module = tmp_path / "edge_infra.py"
    module.write_text(
        "from piceli.infra import DnsRecord, Infrastructure, Porkbun, Server\n"
        "from piceli.testing.infra import FakeProvider\n"
        "edge = Server('edge-1', provider=FakeProvider(), type='t1', image='img')\n"
        "infra = Infrastructure('edge', servers=[edge], records=[DnsRecord("
        "'example.com', 'www', 'A', value='192.0.2.1', "
        "provider=Porkbun(credentials='dns-keys'))])\n"
    )
    monkeypatch.chdir(tmp_path)
    declared = _run(
        "provider", "dns-keys", "--infra", "edge_infra.py:infra", "--prompt",
        input=f"{API_KEY}\n{SECRET_KEY}\n",
    )  # fmt: skip
    assert declared.exit_code == 0, declared.output
    assert json.loads(declared.stdout)["provider"] == "porkbun"
    unused = _run(
        "provider", "other", "--infra", "edge_infra.py:infra", "--prompt",
        input="x\n",
    )  # fmt: skip
    _rejected(unused, "infra-credential-invalid")


def test_re_running_replaces_without_a_delete(homes: Path) -> None:
    credentials.save_credential("porkbun", "provider-token", SECRET_KEY)
    result = _run("provider", "porkbun", "--prompt", input=f"{API_KEY}\n{SECRET_KEY}\n")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["replaced"] is True
    assert cp.credential_report("porkbun")["complete"] is True


def test_help_names_the_parts_and_the_replacement() -> None:
    result = CliRunner().invoke(app, ["secrets", "provider", "--help"])
    text = " ".join(result.output.split())
    for needle in (
        "Porkbun API key (pk1_…)",
        "Porkbun secret API key (sk1_…)",
        "Application credential ID",
        "Application credential secret",
        "one part per line",
        "PORKBUN_SECRET_API_KEY",
        "OS_APPLICATION_CREDENTIAL_ID",
        "replaces",
    ):
        assert needle in text, needle


def test_prompts_on_a_terminal_name_each_part_and_hide_the_secret(
    homes: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from piceli.k8s.cli import infra as cli

    shown: list[str] = []
    hidden: list[str] = []
    answers = iter([API_KEY, SECRET_KEY])
    monkeypatch.setattr(cli, "_terminal", lambda: True)
    monkeypatch.setattr(cli, "_echoed", lambda p: (shown.append(p), next(answers))[1])
    monkeypatch.setattr(cli, "_hidden", lambda p: (hidden.append(p), next(answers))[1])
    parts = cli._ask_parts(cp.SHAPES["porkbun"], from_env=False)
    assert parts == {"api_key": API_KEY, "secret_api_key": SECRET_KEY}
    assert shown == ["Porkbun API key (pk1_…): "]
    assert hidden == ["Porkbun secret API key (sk1_…, not echoed): "]
    guide = capsys.readouterr().err
    assert "Step 1 of 2" in guide and "Step 2 of 2" in guide
    assert "API Access" in guide


# ------------------------------------------------------------- reading back


def test_an_older_joined_credential_reads_and_is_complete(homes: Path) -> None:
    credentials.save_credential("porkbun", "provider-token", f"{API_KEY}:{SECRET_KEY}")
    report = cp.credential_report("porkbun", "porkbun")
    assert report["complete"] is True and report["storage"] == "joined"
    assert cp.load_parts("porkbun", "porkbun")["api_key"] == API_KEY
    cp.require_complete("porkbun", "porkbun")


def test_a_one_part_value_is_reported_with_the_fix(homes: Path) -> None:
    credentials.save_credential("porkbun", "provider-token", SECRET_KEY)
    report = cp.credential_report("porkbun", "porkbun")
    assert report == {
        "name": "porkbun",
        "kind": "provider-token",
        "provider": "porkbun",
        "expected": ["api_key", "secret_api_key"],
        "stored": 1,
        "storage": "single",
        "complete": False,
        "fix": "piceli secrets provider porkbun --prompt",
        "message": "porkbun: one part of two stored, re-run "
        "`piceli secrets provider porkbun --prompt`",
    }
    assert SECRET_KEY not in json.dumps(report)
    with pytest.raises(InfraError) as caught:
        cp.require_complete("porkbun", "porkbun")
    assert caught.value.code == "infra-credential-incomplete"
    assert str(caught.value) == report["message"]
    # A name that does not tell the provider gets --provider in the fix.
    credentials.save_credential("dns", "provider-token", SECRET_KEY)
    assert cp.credential_report("dns", "porkbun")["fix"] == (
        "piceli secrets provider dns --provider porkbun --prompt"
    )


def test_every_credential_is_reported(homes: Path) -> None:
    credentials.save_credential("porkbun", "provider-token", SECRET_KEY)
    credentials.save_credential("hcloud", "provider-token", "hcloud-token-0123456789")
    credentials.save_credential("cache-s3", "s3-keys", "AKIDEXAMPLE:s3-secret-0123")
    credentials.save_credential("web-state", "state-key", "p" * 32)
    reports = {r["name"]: r for r in cp.credential_reports()}
    assert set(reports) == {"porkbun", "hcloud", "cache-s3", "web-state"}
    assert reports["porkbun"]["complete"] is False
    assert reports["hcloud"]["complete"] is True
    assert reports["cache-s3"]["complete"] is True
    assert reports["web-state"]["complete"] is True


def test_a_plan_reports_an_incomplete_credential_before_any_call(
    homes: Path, tmp_path: Path
) -> None:
    from piceli.infra.machines.plan import workspace
    from piceli.testing.infra import FakeProvider

    credentials.save_credential("web-state", "state-key", "p" * 32)
    credentials.save_credential("porkbun", "provider-token", SECRET_KEY)
    edge = Server("edge-1", provider=FakeProvider(), type="t1", image="img")
    infra = Infrastructure(
        "web",
        servers=[edge],
        records=[DnsRecord("example.com", "www", "A", value="192.0.2.1",
                           provider=Porkbun(credentials="porkbun"))],
        state_dir=tmp_path / "state",
    )  # fmt: skip
    with (
        pytest.raises(InfraError) as caught,
        workspace(infra, tofu=str(tmp_path / "no-tofu")),
    ):
        pass
    assert caught.value.code == "infra-credential-incomplete"
    assert "porkbun: one part of two stored" in str(caught.value)


def test_backup_lists_incomplete_credentials(homes: Path) -> None:
    credentials.save_credential("porkbun", "provider-token", SECRET_KEY)
    from piceli.infra.machines import secrets_backup as sb

    assert [r["name"] for r in sb.incomplete_credentials()] == ["porkbun"]


# ------------------------------------------------------------- S3 keys


def test_s3_keys_are_stored_by_part(homes: Path) -> None:
    result = _run("s3", "cache-s3", "--prompt", input="AKIDEXAMPLE\ns3-secret-0123\n")
    assert result.exit_code == 0, result.output
    assert "s3-secret-0123" not in result.stdout + result.stderr
    body = json.loads(result.stdout)
    assert body["parts"] == ["access_key_id", "secret_access_key"]
    assert _stored("cache-s3")["parts"] == {
        "access_key_id": "AKIDEXAMPLE",
        "secret_access_key": "s3-secret-0123",
    }
    assert credentials.load_credential("cache-s3", "s3-keys") == (
        "AKIDEXAMPLE:s3-secret-0123"
    )


@pytest.mark.parametrize(
    ("lines", "part"),
    [
        ("AKIDEXAMPLE\n", "secret_access_key"),
        ("AKID:SECRET\n", "access_key_id"),
        ("AKIDEXAMPLE\n s3-secret\n", "secret_access_key"),
    ],
    ids=["one-part", "joined", "whitespace"],
)
def test_a_wrong_s3_part_is_refused(homes: Path, lines: str, part: str) -> None:
    result = _run("s3", "cache-s3", "--prompt", input=lines)
    assert _rejected(result, "infra-credential-part-invalid")["part"] == part
    assert not credentials.credential_present("cache-s3")


def test_a_one_part_s3_credential_is_incomplete(homes: Path) -> None:
    credentials.save_credential("cache-s3", "s3-keys", "only-one-part")
    with pytest.raises(InfraError) as caught:
        credentials.load_credential("cache-s3", "s3-keys")
    assert caught.value.code == "infra-credential-incomplete"
    assert "cache-s3: one part of two stored" in str(caught.value)
    assert "piceli secrets s3 cache-s3 --prompt" in str(caught.value)
