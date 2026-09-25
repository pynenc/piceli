"""External secret sources: SOPS, Vault KV v2 and AWS Secrets Manager, offline.

Every source is a fake (a sops script, an in-process Vault, a botocore
Stubber); ``HOME`` and credential locations are isolated per test. Values
must never appear in an error message.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from pydantic import ValidationError

from piceli.k8s.release_secret_spec import (
    AwsSecretSpec,
    SecretError,
    SopsSecretSpec,
    VaultSecretSpec,
)
from piceli.k8s.release_secrets import config_digest, describe, materialize
from piceli.k8s.release_spec import ReleaseSpecModel
from piceli.k8s.secret_sources import (
    ExternalSources,
    Fetched,
    SecretSourceError,
    fetch_all,
    source_digest,
    source_key,
)
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

SECRET_VALUE = "s3cr3t-value-never-printed"
VERSION_ID = "a1b2c3d4-5678-90ab-cdef-000000000001"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    isolate_credentials(monkeypatch, tmp_path / "home")


def _sops_spec(tmp_path: Path, **extra) -> SopsSecretSpec:
    return SopsSecretSpec.model_validate(
        {
            "type": "sops",
            "file": str(tmp_path / "app.enc.json"),
            "key": "db.password",
            "sops": str(fake_sops(tmp_path / "bin")),
            **extra,
        }
    )


def _read(spec, *, environ=None, aws=None, name="value") -> bytes:
    sources = ExternalSources(
        environ=environ if environ is not None else dict(os.environ),
        aws_client=aws,
    )
    return sources.read(name, spec)


def _refused(code: str, spec, **kwargs) -> SecretSourceError:
    with pytest.raises(SecretSourceError) as caught:
        _read(spec, **kwargs)
    assert caught.value.code == code, caught.value
    assert SECRET_VALUE not in str(caught.value)
    assert VAULT_TOKEN not in str(caught.value)
    return caught.value


# ------------------------------------------------------------------ specs
def test_specs_parse_from_release_toml_tables():
    model = ReleaseSpecModel.model_validate(
        {
            "target": {"kubeconfig": "k", "context": "c", "namespace": "shop"},
            "release": {
                "name": "shop",
                "owner": "o",
                "field_manager": "f",
                "composition": "c.py:build",
                "state_dir": "state",
            },
            "images": {"api": {"digest": "sha256:" + "1" * 64}},
            "secrets": {
                "db": {"type": "sops", "file": "s.enc.yaml", "key": "db.password"},
                "api": {
                    "type": "vault",
                    "address": "https://vault.example:8200",
                    "path": "shop/api",
                    "key": "token",
                    "token_env": "VAULT_TOKEN",
                    "namespace": "team-a",
                },
                "cloud": {
                    "type": "aws-secrets-manager",
                    "secret_id": "shop/db",
                    "region": "eu-west-1",
                    "key": "password",
                },
            },
        }
    )
    assert isinstance(model.secrets["db"], SopsSecretSpec)
    assert model.secrets["db"].key == ("db", "password")
    assert isinstance(model.secrets["api"], VaultSecretSpec)
    assert isinstance(model.secrets["cloud"], AwsSecretSpec)
    assert describe("api", model.secrets["api"])["source"] == (
        "vault:https://vault.example:8200/secret/shop/api#token?namespace=team-a"
    )


@pytest.mark.parametrize(
    "table",
    [
        {"address": "http://vault.example:8200"},  # plain http off loopback
        {"address": "https://user:pw@vault.example"},  # credentials in URL
        {"address": "https://vault.example/v1"},  # a path
        {"token_env": None, "token_file": None},  # no token source
        {"token_file": "t"},  # both token sources
        {"path": "../escape"},
        {"token_env": "BAD NAME"},
    ],
)
def test_vault_spec_refuses_unsafe_settings(table):
    base = {
        "type": "vault",
        "address": "https://vault.example:8200",
        "path": "shop/api",
        "key": "token",
        "token_env": "VAULT_TOKEN",
    }
    with pytest.raises(ValidationError):
        VaultSecretSpec.model_validate({**base, **table})


def test_vault_spec_accepts_loopback_http():
    spec = VaultSecretSpec.model_validate(
        {
            "type": "vault",
            "address": "http://127.0.0.1:8200/",
            "path": "shop/api",
            "key": "token",
            "token_file": "token",
        }
    )
    assert spec.address == "http://127.0.0.1:8200"


@pytest.mark.parametrize(
    "table",
    [
        {"key": None},  # a key is required ...
        {"format": "binary"},  # ... unless binary, which refuses one
        {"sops": "bin/sops"},  # relative path
        {"sops_sha256": "abc"},
        {"pass_env": ["NOT VALID"]},
    ],
)
def test_sops_spec_refuses_invalid_settings(table):
    base = {"type": "sops", "file": "s.enc.yaml", "key": "db.password"}
    with pytest.raises(ValidationError):
        SopsSecretSpec.model_validate(
            {k: v for k, v in {**base, **table}.items() if v is not None}
        )


def test_aws_spec_refuses_invalid_settings():
    base = {
        "type": "aws-secrets-manager",
        "secret_id": "shop/db",
        "region": "eu-west-1",
    }
    for table in (
        {"region": "moon"},
        {"secret_id": "bad secret"},
        {"version_stage": "AWSCURRENT", "version_id": "abc"},
        {"endpoint_url": "http://aws.example"},
    ):
        with pytest.raises(ValidationError):
            AwsSecretSpec.model_validate({**base, **table})


def test_config_digest_ignores_how_a_source_is_reached(tmp_path):
    spec = _sops_spec(tmp_path)
    moved = spec.model_copy(
        update={"sops": Path("/opt/sops"), "timeout_seconds": 5, "pass_env": ("X",)}
    )
    assert config_digest(spec) == config_digest(moved)
    other = spec.model_copy(update={"key": ("db", "user")})
    assert config_digest(spec) != config_digest(other)


# ------------------------------------------------------------------- sops
def test_sops_reads_one_key_with_a_minimal_environment(tmp_path):
    spec = _sops_spec(tmp_path, pass_env=["SOPS_AGE_KEY_FILE"])
    sops_file(tmp_path / "app.enc.json", {"db": {"password": SECRET_VALUE}})
    environ = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "SOPS_AGE_KEY_FILE": str(tmp_path / "age.txt"),
        "AWS_SECRET_ACCESS_KEY": "must-not-reach-sops",
        "VAULT_TOKEN": "must-not-reach-sops",
    }
    assert _read(spec, environ=environ) == SECRET_VALUE.encode()
    seen = json.loads((tmp_path / "app.enc.json.env.json").read_text())
    assert "AWS_SECRET_ACCESS_KEY" not in seen and "VAULT_TOKEN" not in seen
    # Only PATH, HOME, pass_env and locale variables (the interpreter adds some).
    assert {name for name in seen if not name.startswith(("LC_", "LANG", "__CF"))} == {
        "PATH",
        "HOME",
        "SOPS_AGE_KEY_FILE",
    }
    assert "SOPS_AGE_KEY_FILE" in seen
    argv = json.loads((tmp_path / "app.enc.json.argv.json").read_text())
    assert argv[:3] == ["--decrypt", "--output-type", "json"]


def test_sops_keys_with_dots_numbers_and_binary_files(tmp_path):
    sops_file(
        tmp_path / "app.enc.json",
        {"a.b": {"port": 6379, "list": ["x", "y"]}},
        binary="whole-file\n",
    )
    assert _read(_sops_spec(tmp_path, key=["a.b", "port"])) == b"6379"
    assert _read(_sops_spec(tmp_path, key=["a.b", "list", "1"])) == b"y"
    binary = _sops_spec(tmp_path, key=[], format="binary")
    assert _read(binary) == b"whole-file\n"
    argv = json.loads((tmp_path / "app.enc.json.argv.json").read_text())
    assert argv[:5] == [
        "--decrypt",
        "--output-type",
        "binary",
        "--input-type",
        "binary",
    ]


def test_sops_missing_key_file_binary_and_pin(tmp_path):
    sops_file(tmp_path / "app.enc.json", {"db": {"password": SECRET_VALUE}})
    error = _refused("secret-source-not-found", _sops_spec(tmp_path, key="db.missing"))
    assert "sops:" in str(error) and "db.missing" in str(error)
    _refused(
        "secret-source-not-found",
        _sops_spec(tmp_path, key="db"),  # a mapping, not a scalar
    )
    _refused(
        "secret-source-not-found",
        _sops_spec(tmp_path, file=str(tmp_path / "absent.enc.json")),
    )
    _refused(
        "secret-source-tool-missing",
        _sops_spec(tmp_path, sops=str(tmp_path / "no-such-sops")),
    )
    _refused(
        "secret-source-tool-missing",
        _sops_spec(tmp_path, sops="sops-not-in-path"),
        environ={"PATH": str(tmp_path)},
    )
    _refused(
        "secret-source-failed", _sops_spec(tmp_path, sops_sha256="sha256:" + "0" * 64)
    )


def test_sops_bare_name_is_found_in_path(tmp_path):
    spec = _sops_spec(tmp_path, sops="sops")
    sops_file(tmp_path / "app.enc.json", {"db": {"password": SECRET_VALUE}})
    assert _read(spec, environ={"PATH": str(tmp_path / "bin")}) == SECRET_VALUE.encode()


def test_sops_decryption_failure_timeout_and_exit_codes(tmp_path):
    path = tmp_path / "app.enc.json"
    sops_file(path, {"db": {"password": SECRET_VALUE}}, exit=128)
    error = _refused("secret-source-auth-failed", _sops_spec(tmp_path))
    assert "leaked-looking" not in str(error)  # stderr is never shown
    sops_file(path, {}, exit=1)
    _refused("secret-source-failed", _sops_spec(tmp_path))
    sops_file(path, {}, exit=2)
    _refused("secret-source-not-found", _sops_spec(tmp_path))
    sops_file(path, {}, sleep=5)
    _refused("secret-source-timeout", _sops_spec(tmp_path, timeout_seconds=0.3))


# ------------------------------------------------------------------ vault
def _vault_spec(vault: FakeVault, tmp_path: Path, **extra) -> VaultSecretSpec:
    token = tmp_path / "vault-token"
    token.write_text(VAULT_TOKEN + "\n")
    table = {
        "type": "vault",
        "address": vault.address,
        "path": "shop/api",
        "key": "token",
        "token_file": str(token),
        **({"ca_file": str(vault.ca_file)} if vault.ca_file else {}),
        **extra,
    }
    return VaultSecretSpec.model_validate(
        {k: v for k, v in table.items() if v is not None}
    )


def test_vault_reads_over_verified_tls_with_namespace_and_versions(tmp_path):
    vault = FakeVault(namespace="team-a")
    vault.put("shop/api", {"token": "first-" + SECRET_VALUE})
    vault.put("shop/api", {"token": SECRET_VALUE, "port": 443})
    with serve_vault(vault, tls_dir=tmp_path):
        spec = _vault_spec(vault, tmp_path, namespace="team-a")
        assert _read(spec) == SECRET_VALUE.encode()
        pinned = _vault_spec(vault, tmp_path, namespace="team-a", version=1)
        assert _read(pinned) == ("first-" + SECRET_VALUE).encode()
        port = _vault_spec(vault, tmp_path, namespace="team-a", key="port")
        assert _read(port) == b"443"
        # Without the private CA the server certificate is not trusted.
        untrusted = spec.model_copy(update={"ca_file": None})
        error = _refused("secret-source-failed", untrusted)
        assert "tls-verify-failed" in str(error)
    first = vault.requests[0]
    assert first["path"] == "/v1/secret/data/shop/api"
    assert first["headers"]["X-Vault-Token"] == VAULT_TOKEN
    assert first["headers"]["X-Vault-Namespace"] == "team-a"
    assert vault.requests[1]["path"] == "/v1/secret/data/shop/api?version=1"


def test_vault_token_from_environment_variable(tmp_path):
    vault = FakeVault()
    vault.put("shop/api", {"token": SECRET_VALUE})
    with serve_vault(vault):
        spec = _vault_spec(vault, tmp_path, token_file=None, token_env="MY_TOKEN")
        assert _read(spec, environ={"MY_TOKEN": VAULT_TOKEN}) == SECRET_VALUE.encode()
        error = _refused("secret-source-auth-failed", spec, environ={})
        assert "MY_TOKEN" in str(error)


def test_vault_refusals_have_codes(tmp_path):
    vault = FakeVault()
    vault.put("shop/api", {"token": SECRET_VALUE, "nested": {"a": 1}})
    with serve_vault(vault):
        _refused("secret-source-not-found", _vault_spec(vault, tmp_path, key="nope"))
        _refused("secret-source-not-found", _vault_spec(vault, tmp_path, key="nested"))
        _refused(
            "secret-source-not-found", _vault_spec(vault, tmp_path, path="shop/none")
        )
        wrong = _vault_spec(vault, tmp_path)
        (tmp_path / "vault-token").write_text("wrong-token")
        _refused("secret-source-auth-failed", wrong)
        (tmp_path / "vault-token").unlink()
        _refused("secret-source-auth-failed", wrong)
        (tmp_path / "vault-token").write_text("line1\nline2")
        _refused("secret-source-auth-failed", wrong)
        vault.status = 503
        error = _refused("secret-source-failed", _vault_spec(vault, tmp_path))
        assert "HTTP 503" in str(error) and "forced" not in str(error)
        vault.status = None
        vault.delay = 1.0
        _refused(
            "secret-source-timeout", _vault_spec(vault, tmp_path, timeout_seconds=0.2)
        )
        vault.delay = 0
    closed = vault.address  # the server is gone
    spec = _vault_spec(FakeVault(address=closed), tmp_path)
    _refused("secret-source-failed", spec)


# -------------------------------------------------------------------- aws
def _aws_spec(**extra) -> AwsSecretSpec:
    return AwsSecretSpec.model_validate(
        {
            "type": "aws-secrets-manager",
            "secret_id": "shop/db",
            "region": "eu-west-1",
            **extra,
        }
    )


def test_aws_reads_a_string_a_json_key_and_a_binary_secret():
    client, stubber = stubbed_aws()
    stubber.add_response(
        "get_secret_value",
        aws_value("shop/db", string=SECRET_VALUE),
        {"SecretId": "shop/db"},
    )
    stubber.add_response(
        "get_secret_value",
        aws_value("shop/db", string=json.dumps({"password": SECRET_VALUE})),
        {"SecretId": "shop/db", "VersionStage": "AWSPREVIOUS"},
    )
    stubber.add_response(
        "get_secret_value",
        aws_value("shop/db", binary=b"\x00\x01binary"),
        {"SecretId": "shop/db", "VersionId": VERSION_ID},
    )
    factory = lambda spec: client  # noqa: E731
    assert _read(_aws_spec(), aws=factory) == SECRET_VALUE.encode()
    assert (
        _read(_aws_spec(key="password", version_stage="AWSPREVIOUS"), aws=factory)
        == SECRET_VALUE.encode()
    )
    assert _read(_aws_spec(version_id=VERSION_ID), aws=factory) == b"\x00\x01binary"
    stubber.assert_no_pending_responses()


@pytest.mark.parametrize(
    ("error", "code"),
    [
        ("AccessDeniedException", "secret-source-auth-failed"),
        ("UnrecognizedClientException", "secret-source-auth-failed"),
        ("ResourceNotFoundException", "secret-source-not-found"),
        ("InternalServiceError", "secret-source-failed"),
    ],
)
def test_aws_client_errors_map_to_codes(error, code):
    client, stubber = stubbed_aws()
    stubber.add_client_error(
        "get_secret_value",
        service_error_code=error,
        service_message="message with " + SECRET_VALUE,
        http_status_code=400,
    )
    refused = _refused(code, _aws_spec(), aws=lambda spec: client)
    assert "message with" not in str(refused)


def test_aws_missing_json_key_and_missing_credentials():
    client, stubber = stubbed_aws()
    stubber.add_response(
        "get_secret_value", aws_value("shop/db", string=json.dumps({"user": "shop"}))
    )
    _refused("secret-source-not-found", _aws_spec(key="password"), aws=lambda s: client)
    # The real client factory with no credentials anywhere (HOME is isolated).
    pytest.importorskip("botocore")
    _refused("secret-source-auth-failed", _aws_spec())


def test_aws_without_botocore_is_a_tool_missing_refusal(monkeypatch):
    import builtins

    real = builtins.__import__

    def no_botocore(name, *args, **kwargs):
        if name.startswith("botocore"):
            raise ImportError(name)
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_botocore)
    error = _refused("secret-source-tool-missing", _aws_spec())
    assert "piceli[aws]" in str(error)


# --------------------------------------------------------------- digests
def test_source_key_is_private_and_stable(tmp_path):
    path = tmp_path / "state" / "secret-sources.key"
    path.parent.mkdir(mode=0o700)
    assert source_key(path, create=False) is None
    key = source_key(path, create=True)
    assert key is not None and len(key) == 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert source_key(path, create=True) == key
    path.chmod(0o644)
    with pytest.raises(ValueError, match="owner-only"):
        source_key(path, create=False)


def test_digest_is_keyed_and_value_free():
    first = source_digest(b"k" * 32, "db", SECRET_VALUE.encode())
    assert first.startswith("hmac-sha256:")
    assert first == source_digest(b"k" * 32, "db", SECRET_VALUE.encode())
    assert first != source_digest(b"j" * 32, "db", SECRET_VALUE.encode())
    assert first != source_digest(b"k" * 32, "db", b"other")
    fetched = Fetched(SECRET_VALUE.encode(), first)
    assert SECRET_VALUE not in repr(fetched)


def test_fetch_all_reads_only_external_generators(tmp_path):
    sops_file(tmp_path / "app.enc.json", {"db": {"password": SECRET_VALUE}})
    spec = {"db": _sops_spec(tmp_path)}
    fetched = fetch_all(spec, b"k" * 32, ExternalSources())
    assert fetched["db"].value == SECRET_VALUE.encode()
    assert fetch_all({}, b"k" * 32) == {}


# ------------------------------------------------------------ materialize
def test_materialize_carries_an_unchanged_value_and_stores_a_changed_one(tmp_path):
    spec = {"db": _sops_spec(tmp_path, encoding="raw")}
    key = b"k" * 32
    stored: dict[str, tuple[str, dict[str, str]]] = {}

    def carry(name, part, digest, wanted):
        if digest in stored:
            release, values = stored[digest]
            return values, release
        return None

    def fetched(value: str) -> dict[str, Fetched]:
        return {"db": Fetched(value.encode(), source_digest(key, "db", value.encode()))}

    first = materialize(spec, carry=carry, external=fetched("v1"))
    assert first.origin == {"db": "fetched"}
    assert first.meta["db"]["source"].startswith("sops:")
    stored[first.parts["db"][""]] = ("rel-1", first.values)
    again = materialize(spec, carry=carry, external=fetched("v1"))
    assert again.origin == {"db": "carried:rel-1"}
    assert again.values == {"db": "v1"}
    changed = materialize(spec, carry=carry, external=fetched("v2"))
    assert changed.origin == {"db": "fetched"}
    assert changed.values == {"db": "v2"}
    assert "v2" not in json.dumps(changed.parts) + json.dumps(changed.meta)


def test_materialize_refuses_rotation_and_unread_sources(tmp_path):
    spec = {"db": _sops_spec(tmp_path)}
    with pytest.raises(SecretError) as caught:
        materialize(spec, rotate=["db"], external={})
    assert caught.value.code == "secret-rotation-refused"
    with pytest.raises(SecretError) as caught:
        materialize(spec, external={})
    assert caught.value.code == "secret-source-failed"


def test_templates_can_use_external_values(tmp_path):
    from piceli.k8s.release_secret_spec import TemplateSecretSpec

    spec = {
        "db": _sops_spec(tmp_path),
        "url": TemplateSecretSpec(
            type="template", template="postgres://shop:{db}@db/shop", encoding="raw"
        ),
    }
    key = b"k" * 32
    result = materialize(
        spec, external={"db": Fetched(b"pw", source_digest(key, "db", b"pw"))}
    )
    assert result.values["url"] == "postgres://shop:pw@db/shop"


def test_pipeline_factories_build_the_same_specs(tmp_path):
    from piceli.pipeline import AwsSecret, Secrets, Sops, Vault

    secrets = Secrets(
        db=Sops("secrets/app.enc.yaml", "db.password", pass_env=["SOPS_AGE_KEY_FILE"]),
        api=Vault(
            "https://vault.example:8200", "shop/api", "token", token_env="VAULT_TOKEN"
        ),
        cloud=AwsSecret("shop/db", region="eu-west-1", key="password"),
        binary=Sops("secrets/blob.enc", format="binary"),
    )
    assert secrets.inputs() == ("db", "api", "cloud", "binary")
    assert secrets.generators["db"].key == ("db", "password")
    assert secrets.generators["api"].type == "vault"
    assert secrets.generators["cloud"].type == "aws-secrets-manager"
