"""Acceptance: external secret sources through ``piceli release`` (fake API).

A fake ``sops`` binary, an in-process Vault KV v2 server and a botocore
Stubber stand in for the real sources; ``HOME`` and credential locations are
isolated. Covers: values reach the cluster and never the output or public
state; an unchanged source re-plans the same release and stores nothing; a
changed value is a new version in a new release (the others are carried);
a refused plan stores nothing; failures and ``--rotate`` refuse with codes.
"""

# ruff: noqa: F811  (tests take the imported ``release_env`` fixture)

from __future__ import annotations

import base64
import json
import stat
from pathlib import Path

import pytest

from tests.acceptance.fake_api import manifest
from tests.acceptance.test_release_cli import (  # noqa: F401 (pytest fixture)
    _run,
    release_env,
)
from tests.acceptance.test_release_secrets_cli import _versions
from tests.secret_source_fakes import (
    VAULT_TOKEN,
    FakeVault,
    aws_value,
    fake_sops,
    isolate_credentials,
    serve_vault,
    sops_file,
    stubbed_aws,
)

DB_PASSWORD = "sops-db-password-value"
API_TOKEN = "vault-api-token-value"
API_TOKEN_2 = "vault-api-token-rotated"
CLOUD_KEY = "aws-cloud-key-value"
VALUES = (DB_PASSWORD, API_TOKEN, API_TOKEN_2, CLOUD_KEY, VAULT_TOKEN)

COMPOSITION = """
from piceli.k8s.ops.plan import DeploymentComponent, DeploymentComposition, ResourceIntent


def build(ctx):
    meta = lambda name: {"name": name, "namespace": ctx.namespace}
    keys = {"db": "db_password", "api": "api_token", "cloud": "cloud_key",
            "url": "db_url"}
    secret = ResourceIntent.from_manifest(
        {"apiVersion": "v1", "kind": "Secret",
         "metadata": meta(f"external-{ctx.values['generation']}"),
         "data": {key: "<private>" for key in keys}}
    )
    for key, name in keys.items():
        secret = secret.with_secret("/data/" + key, ctx.secret(name))
    worker = ResourceIntent.from_manifest(
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta("worker"),
         "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "worker"}},
                  "template": {"metadata": {"labels": {"app": "worker"}},
                               "spec": {"containers": [
                                   {"name": "worker", "image": ctx.image("api")}]}}}}
    )
    return DeploymentComposition((
        DeploymentComponent("secrets", (secret,)),
        DeploymentComponent("worker", (worker,), dependencies=("secrets",)),
    ))
"""


def _secrets(vault_address: str) -> str:
    return f"""
[secrets.db_password]
type = "sops"
file = "secrets/app.enc.json"
key = "db.password"
sops = "bin/sops-placeholder"

[secrets.db_url]
type = "template"
template = "postgres://shop:{{db_password}}@db/shop"

[secrets.api_token]
type = "vault"
address = "{vault_address}"
path = "shop/api"
key = "token"
token_file = "vault-token"
namespace = "team-a"

[secrets.cloud_key]
type = "aws-secrets-manager"
secret_id = "shop/cloud"
region = "eu-west-1"
key = "key"

[values]
generation = 1"""


@pytest.fixture
def external_env(release_env, monkeypatch):
    import piceli.k8s.secret_sources as sources

    api, tmp_path = release_env
    isolate_credentials(monkeypatch, tmp_path / "home")
    tool = fake_sops(tmp_path / "bin")
    sops_file(tmp_path / "secrets" / "app.enc.json", {"db": {"password": DB_PASSWORD}})
    (tmp_path / "vault-token").write_text(VAULT_TOKEN + "\n")
    client, stubber = stubbed_aws()
    monkeypatch.setattr(sources, "_aws_client", lambda spec: client)

    vault = FakeVault(namespace="team-a")
    vault.put("shop/api", {"token": API_TOKEN})
    with serve_vault(vault):
        (tmp_path / "compose.py").write_text(COMPOSITION)
        spec = tmp_path / "release.toml"
        text = spec.read_text()
        old = (
            '[secrets.password]\ntype = "random"\nbytes = 24\n\n[values]\nmode = "blue"'
        )
        assert old in text
        secrets = _secrets(vault.address).replace(
            '"bin/sops-placeholder"', json.dumps(str(tool))
        )
        spec.write_text(text.replace(old, secrets.strip()))
        yield api, tmp_path, vault, stubber


def _aws(stubber, value: str = CLOUD_KEY, times: int = 1) -> None:
    for _ in range(times):
        stubber.add_response(
            "get_secret_value",
            aws_value("shop/cloud", string=json.dumps({"key": value})),
            {"SecretId": "shop/cloud"},
        )


def _data(api, key: str, generation: int = 1) -> str:
    stored = api.objects[("Secret", f"external-{generation}")]
    return base64.b64decode(stored["data"][key]).decode()


def _public_state(tmp_path: Path) -> str:
    """Every state file except the private value store, concatenated."""
    texts = []
    for path in sorted((tmp_path / "state").rglob("*")):
        if path.is_file() and path.name not in {"secrets.sqlite", "secret-sources.key"}:
            texts.append(path.read_bytes().decode(errors="replace"))
    return "\n".join(texts)


def _bump(tmp_path: Path, generation: int) -> None:
    spec = tmp_path / "release.toml"
    spec.write_text(
        spec.read_text().replace(
            f"generation = {generation - 1}", f"generation = {generation}"
        )
    )


def test_external_values_reach_the_cluster_and_follow_the_source(external_env):
    api, tmp_path, vault, stubber = external_env
    _aws(stubber)
    code, planned, result = _run(tmp_path, "plan")
    assert code == 0, planned
    assert planned["mode"] == "create"
    assert planned["secrets"] == {
        "api_token": "fetched",
        "cloud_key": "fetched",
        "db_password": "fetched",
        "db_url": "rendered",
    }
    output = json.dumps(planned) + result.stderr
    assert not [value for value in VALUES if value in output]
    first = planned["release"]
    _aws(stubber)  # apply re-plans nothing: it runs the stored session
    code, applied, result = _run(tmp_path, "apply", "--approve", planned["plan_hash"])
    assert code == 0, applied
    assert applied["release"] == first
    assert _data(api, "db") == DB_PASSWORD
    assert _data(api, "api") == API_TOKEN
    assert _data(api, "cloud") == CLOUD_KEY
    assert _data(api, "url") == f"postgres://shop:{DB_PASSWORD}@db/shop"
    public = _public_state(tmp_path)
    assert not [value for value in VALUES if value in public]
    key = tmp_path / "state" / "secret-sources.key"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    # The Vault request carried the token and namespace as headers only.
    assert vault.requests[0]["headers"]["X-Vault-Namespace"] == "team-a"
    stored = _versions(tmp_path)

    # Unchanged sources: the same release is re-planned; nothing is stored.
    stubber._queue.clear()
    _aws(stubber)
    code, planned, _ = _run(tmp_path, "plan")
    assert code == 0, planned
    assert planned["release"] == first
    assert planned["mode"] != "create"
    assert _versions(tmp_path) == stored

    # The value changes at the source: a new release with a new version;
    # the other sources are carried over. A new Secret name takes the new
    # value (Secrets are retained objects, as for --rotate).
    vault.put("shop/api", {"token": API_TOKEN_2})
    _bump(tmp_path, 2)
    _aws(stubber)
    code, planned, result = _run(tmp_path, "plan")
    assert code == 0, planned
    assert planned["mode"] == "create" and planned["release"] != first
    assert planned["secrets"]["api_token"] == "fetched"
    assert planned["secrets"]["db_password"] == f"carried:{first}"
    assert planned["secrets"]["cloud_key"] == f"carried:{first}"
    assert API_TOKEN_2 not in json.dumps(planned) + result.stderr
    code, applied, _ = _run(tmp_path, "apply", "--approve", planned["plan_hash"])
    assert code == 0, applied
    assert _data(api, "api", 2) == API_TOKEN_2
    assert _data(api, "db", 2) == DB_PASSWORD
    assert _data(api, "api", 1) == API_TOKEN  # the old Secret is untouched
    assert not [value for value in VALUES if value in _public_state(tmp_path)]

    # secret show: metadata names the source, never the value.
    from typer.testing import CliRunner

    from piceli.k8s.cli.release import app

    shown = CliRunner().invoke(
        app,
        [
            "secret",
            "show",
            "api_token",
            "--json",
            "--spec",
            str(tmp_path / "release.toml"),
        ],
    )
    assert shown.exit_code == 0, shown.output
    metadata = json.loads(shown.stdout)
    assert metadata["type"] == "vault"
    assert metadata["source"].startswith("vault:http://127.0.0.1:")
    assert not [value for value in VALUES if value in shown.output]


def test_refused_plan_with_a_changed_value_stores_nothing(external_env):
    api, tmp_path, vault, stubber = external_env
    _aws(stubber)
    code, planned, _ = _run(tmp_path, "apply", "--auto-approve")
    assert code == 0, planned
    stored = _versions(tmp_path)
    vault.put("shop/api", {"token": API_TOKEN_2})
    _bump(tmp_path, 2)
    del api.objects[("Deployment", "worker")]
    api.put(manifest("Deployment", "worker"))  # now unmanaged: needs adoption
    _aws(stubber)
    code, refused, _ = _run(tmp_path, "plan")
    assert code == 2, refused
    assert "Deployment/worker" in refused["message"]
    assert _versions(tmp_path) == stored
    assert API_TOKEN_2 not in json.dumps(refused)


@pytest.mark.parametrize(
    ("break_source", "code"),
    [
        ("token", "secret-source-auth-failed"),
        ("path", "secret-source-not-found"),
        ("sops", "secret-source-auth-failed"),
        ("aws", "secret-source-not-found"),
    ],
)
def test_source_failures_refuse_with_codes(external_env, break_source, code):
    api, tmp_path, vault, stubber = external_env
    if break_source == "token":
        (tmp_path / "vault-token").write_text("wrong-token")
    elif break_source == "path":
        vault.secrets.clear()
    elif break_source == "sops":
        sops_file(
            tmp_path / "secrets" / "app.enc.json",
            {"db": {"password": DB_PASSWORD}},
            exit=128,
        )
    _aws(stubber)
    if break_source == "aws":
        stubber._queue.clear()
        stubber.add_client_error(
            "get_secret_value", service_error_code="ResourceNotFoundException"
        )
    code_, refused, result = _run(tmp_path, "plan")
    assert code_ == 2, refused
    assert refused["reason"] == code
    assert _versions(tmp_path) == 0
    assert not [value for value in VALUES if value in result.output]


def test_rotating_an_external_source_is_refused(external_env):
    api, tmp_path, vault, stubber = external_env
    code, refused, _ = _run(tmp_path, "plan", "--rotate", "api_token")
    assert code == 2
    assert refused["reason"] == "secret-rotation-refused"
    assert "rotate it at its source" in refused["message"]
    assert vault.requests == []  # refused before any source is read
    assert _versions(tmp_path) == 0


def test_changed_value_under_the_same_secret_name_writes_nothing(external_env):
    """Secrets are retained: a new value needs a new Secret name (as --rotate)."""
    api, tmp_path, vault, stubber = external_env
    _aws(stubber)
    code, applied, _ = _run(tmp_path, "apply", "--auto-approve")
    assert code == 0, applied
    vault.put("shop/api", {"token": API_TOKEN_2})
    _aws(stubber)
    code, result, _ = _run(tmp_path, "apply", "--auto-approve")
    assert code == 1, result
    assert result["reason"] == "retained-content-precondition-failed"
    assert _data(api, "api") == API_TOKEN
