"""Secret generator specs, templates, imports, tls-ca and materialization."""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path

import pytest

from piceli.k8s.ops.discovery import PlanTarget
from piceli.k8s.ops.secret_versions import SecretVersionStore
from piceli.k8s.release_secret_spec import (
    SecretError,
    consumed_outputs,
    generation_order,
    outputs,
    render_template,
    template_references,
)
from piceli.k8s.release_secrets import (
    ImportSources,
    config_digest,
    input_names,
    materialize,
    part_digests,
)
from piceli.k8s.release_spec import ReleaseSpec, ReleaseSpecError, ReleaseSpecModel

OPENSSL = Path("/usr/bin/openssl")
needs_openssl = pytest.mark.skipif(not OPENSSL.exists(), reason="needs openssl")


def specs(**secrets):
    """Validated generator specs, as the release spec would hold them."""
    return ReleaseSpecModel.model_validate(
        {
            "target": {"kubeconfig": "kc", "context": "ctx", "namespace": "demo"},
            "release": {
                "name": "web",
                "owner": "owner",
                "field_manager": "manager",
                "composition": "compose.py:build",
                "state_dir": "state",
            },
            "images": {"web": "sha256:" + "a" * 64},
            "secrets": secrets,
        }
    ).secrets


def raw(values, name):
    return base64.b64decode(values[name]).decode()


def test_template_placeholders_and_literal_braces():
    assert template_references("a {x} {{b}} {secret:y.crt}") == ("x", "y.crt")
    assert render_template("{{{x}}}", {"x": "v"}) == "{v}"
    for bad in ("{", "}", "{Bad}", "{x", "{ x }"):
        with pytest.raises(SecretError) as caught:
            template_references(bad)
        assert caught.value.code == "secret-template-invalid"


def test_dsn_and_config_file_are_rendered_from_a_random_password():
    secrets = specs(
        cache_password={"type": "random", "bytes": 16},
        cache_url={
            "type": "template",
            "template": "rediss://:{cache_password}@cache:6380/0",
        },
        cache_conf={
            "type": "template",
            "encoding": "raw",
            "template": "requirepass {secret:cache_password}\nurl {cache_url}\n",
        },
    )
    assert generation_order(secrets) == ("cache_password", "cache_url", "cache_conf")
    assert consumed_outputs(secrets) == {"cache_password", "cache_url"}
    result = materialize(secrets)
    password = raw(result.values, "cache_password")
    url = raw(result.values, "cache_url")
    assert url == f"rediss://:{password}@cache:6380/0"
    assert result.values["cache_conf"] == f"requirepass {password}\nurl {url}\n"
    assert result.origin == {
        "cache_password": "generated",
        "cache_url": "rendered",
        "cache_conf": "rendered",
    }
    assert result.meta["cache_conf"]["depends_on"] == ["cache_password", "cache_url"]


def test_template_cycles_and_unknown_references_are_refused(tmp_path):
    base = {
        "target": {"kubeconfig": "kc", "context": "ctx", "namespace": "demo"},
        "release": {
            "name": "web",
            "owner": "owner",
            "field_manager": "manager",
            "composition": "compose.py:build",
            "state_dir": "state",
        },
        "images": {"web": "sha256:" + "a" * 64},
    }
    cycle = {
        "a": {"type": "template", "template": "{b}"},
        "b": {"type": "template", "template": "x{c}"},
        "c": {"type": "template", "template": "{a}"},
    }
    with pytest.raises(SecretError) as caught:
        ReleaseSpec.from_dict(base | {"secrets": cycle}, tmp_path)
    assert caught.value.code == "secret-dependency-cycle"
    assert "a -> b -> c -> a" in str(caught.value)

    unknown = {
        "ca": {"type": "tls-ca", "leaves": {"api": ["api"]}},
        "bundle": {"type": "template", "template": "{ca}"},
    }
    with pytest.raises(SecretError) as caught:
        ReleaseSpec.from_dict(base | {"secrets": unknown}, tmp_path)
    assert caught.value.code == "secret-unknown-reference"
    assert "ca.api.crt" in str(caught.value)
    internal = unknown | {"bundle": {"type": "template", "template": "{ca.ca.key}"}}
    with pytest.raises(SecretError, match="not an exposed output"):
        ReleaseSpec.from_dict(base | {"secrets": internal}, tmp_path)


@pytest.mark.parametrize(
    "secret",
    [
        {"type": "import"},
        {"type": "import", "env": "A", "file": "a"},
        {"type": "import", "env": "1BAD"},
        {"type": "import", "secret": {"name": "Bad_Name", "key": "k"}},
        {"type": "tls-ca", "leaves": {}},
        {"type": "tls-ca", "leaves": {"ca": ["x"]}},
        {"type": "tls-ca", "leaves": {"api": {}}},
        {"type": "tls-ca", "leaves": {"api": ["x"]}, "days": 90, "ca_days": 30},
        {"type": "template", "template": "{"},
        {"type": "static"},
    ],
)
def test_invalid_generator_specs_are_rejected(secret, tmp_path):
    base = {
        "target": {"kubeconfig": "kc", "context": "ctx", "namespace": "demo"},
        "release": {
            "name": "web",
            "owner": "owner",
            "field_manager": "manager",
            "composition": "compose.py:build",
            "state_dir": "state",
        },
        "images": {"web": "sha256:" + "a" * 64},
    }
    with pytest.raises(ReleaseSpecError):
        ReleaseSpec.from_dict(base | {"secrets": {"s": secret}}, tmp_path)


def test_imports_read_file_env_and_live_secret(tmp_path):
    (tmp_path / "password.txt").write_text("from-file\n")
    secrets = specs(
        a={"type": "import", "file": "password.txt"},
        b={"type": "import", "env": "CACHE_PASSWORD", "encoding": "raw"},
        c={"type": "import", "secret": {"name": "legacy", "key": "password"}},
        d={"type": "static", "value": "cache-user", "encoding": "raw"},
    )
    reads = []

    def read_secret(name, key):
        reads.append((name, key))
        return b"from-secret\n"

    sources = ImportSources(
        resolve=lambda path: tmp_path / path,
        read_secret=read_secret,
        environ={"CACHE_PASSWORD": "from-env"},
    )
    result = materialize(secrets, sources=sources)
    assert raw(result.values, "a") == "from-file"  # one trailing newline trimmed
    assert result.values["b"] == "from-env"
    assert raw(result.values, "c") == "from-secret\n"  # live values are verbatim
    assert result.values["d"] == "cache-user"
    assert reads == [("legacy", "password")]
    assert result.origin == {
        "a": "imported",
        "b": "imported",
        "c": "imported",
        "d": "static",
    }
    assert result.meta["c"]["source"] == "secret:legacy/password"


def test_missing_import_sources_are_refused_without_values(tmp_path):
    for secret, sources in [
        ({"type": "import", "env": "UNSET"}, ImportSources(environ={})),
        ({"type": "import", "file": "absent"}, ImportSources(lambda p: tmp_path / p)),
        ({"type": "import", "secret": {"name": "s", "key": "k"}}, ImportSources()),
        ({"type": "import", "env": "EMPTY"}, ImportSources(environ={"EMPTY": "\n"})),
    ]:
        with pytest.raises(SecretError) as caught:
            materialize(specs(s=secret), sources=sources)
        assert caught.value.code == "secret-import-unavailable"

    def denied(name, key):
        from piceli.k8s.ops.kubernetes_provider import ProviderError

        raise ProviderError("rbac-denied", status=403)

    with pytest.raises(SecretError, match="rbac-denied"):
        materialize(
            specs(s={"type": "import", "secret": {"name": "s", "key": "k"}}),
            sources=ImportSources(read_secret=denied),
        )


def test_import_is_carried_then_rotated_by_its_policy():
    secrets = specs(
        token={"type": "import", "env": "TOKEN"},
        again={"type": "import", "env": "TOKEN", "rotate": "reimport", "bytes": 16},
    )
    stored = {"token": base64.b64encode(b"first").decode()}

    def carry(name, part, digest, wanted):
        assert part == "" and digest == config_digest(secrets[name])
        return (dict.fromkeys(wanted, stored["token"]), "web-1")

    environ = {"TOKEN": "second"}
    carried = materialize(secrets, carry=carry, sources=ImportSources(environ=environ))
    assert carried.origin == {"token": "carried:web-1", "again": "carried:web-1"}
    rotated = materialize(
        secrets,
        rotate=("token", "again"),
        carry=carry,
        sources=ImportSources(environ=environ),
    )
    assert rotated.origin == {"token": "rotated", "again": "rotated"}
    assert raw(rotated.values, "again") == "second"
    assert raw(rotated.values, "token") not in {"first", "second"}
    # The rotation policy does not change the value-shaping digest.
    assert config_digest(secrets["again"]) == config_digest(secrets["token"])


def test_templates_and_static_values_are_not_rotated():
    secrets = specs(
        user={"type": "static", "value": "u"},
        url={"type": "template", "template": "x://{user}"},
    )
    for name in ("user", "url"):
        with pytest.raises(SecretError) as caught:
            materialize(secrets, rotate=(name,))
        assert caught.value.code == "secret-rotation-refused"


def test_raw_encoding_needs_text():
    secrets = specs(
        blob={"type": "import", "env": "B"},
        text={"type": "template", "template": "{blob}", "encoding": "raw"},
    )
    carry_blob = base64.b64encode(b"\xff\xfe").decode()
    with pytest.raises(SecretError) as caught:
        materialize(
            secrets,
            carry=lambda name, part, digest, wanted: (
                dict.fromkeys(wanted, carry_blob),
                "web-1",
            ),
        )
    assert caught.value.code == "secret-not-text"


def _verify(ca: str, certificate: str, tmp_path: Path) -> str:
    (tmp_path / "ca.pem").write_text(ca)
    (tmp_path / "leaf.pem").write_text(certificate)
    subprocess.run(
        [str(OPENSSL), "verify", "-CAfile", "ca.pem", "leaf.pem"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    return subprocess.run(
        [str(OPENSSL), "x509", "-noout", "-text", "-in", "leaf.pem"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@needs_openssl
def test_tls_ca_signs_leaves_and_keeps_its_key_internal(tmp_path):
    secrets = specs(
        ca={
            "type": "tls-ca",
            "days": 2,
            "ca_days": 3,
            "leaves": {
                "cache": ["cache", "cache.shop.svc"],
                "api": {"dns_names": ["api"], "ip_addresses": ["10.0.0.7"]},
            },
        }
    )
    spec = secrets["ca"]
    assert input_names("ca", spec) == (
        "ca.ca.crt",
        "ca.api.crt",
        "ca.api.key",
        "ca.cache.crt",
        "ca.cache.key",
    )
    assert [item.name for item in outputs("ca", spec) if item.internal] == ["ca.ca.key"]
    result = materialize(secrets)
    assert result.origin == {"ca": "generated"}
    authority = raw(result.values, "ca.ca.crt")
    text = _verify(authority, raw(result.values, "ca.cache.crt"), tmp_path)
    assert "DNS:cache, DNS:cache.shop.svc" in text and "CA:FALSE" in text
    text = _verify(authority, raw(result.values, "ca.api.crt"), tmp_path)
    assert "IP Address:10.0.0.7" in text
    assert "PRIVATE KEY" in raw(result.values, "ca.ca.key")

    # Adding a leaf keeps the CA and the other leaves; only the new one is signed.
    grown = specs(
        ca=spec.model_dump(mode="json")
        | {
            "leaves": {
                "cache": {"dns_names": ["cache", "cache.shop.svc"]},
                "api": {"dns_names": ["api"], "ip_addresses": ["10.0.0.7"]},
                "web": {"dns_names": ["web"]},
            }
        }
    )
    old_parts = part_digests(spec)
    new_parts = part_digests(grown["ca"])
    assert old_parts["ca"] == new_parts["ca"]
    assert old_parts["leaf:cache"] == new_parts["leaf:cache"]

    def carry(name, part, digest, wanted):
        if old_parts.get(part) != digest:
            return None
        return ({item: result.values[item] for item in wanted}, "web-1")

    extended = materialize(grown, carry=carry)
    assert extended.origin == {"ca": "signed:web-1"}
    for item in ("ca.ca.crt", "ca.ca.key", "ca.cache.crt", "ca.api.key"):
        assert extended.values[item] == result.values[item]
    _verify(authority, raw(extended.values, "ca.web.crt"), tmp_path)


def test_store_discards_only_unreferenced_versions(tmp_path):
    target = PlanTarget("cluster", "demo")
    store = SecretVersionStore(tmp_path / "state" / "secrets.sqlite")
    try:
        kept = store.put_once(target, "a" * 32, "kept", "v1")
        loose = store.put(target, "v2")
        store.put_once(target, "b" * 32, "gone", "v3")
        assert store.discard(target, [loose], session_id="b" * 32) == 2
        assert store.resolve(target, kept) == "v1"
        assert store.discard(target, [kept]) == 0  # still bound to a session
        count = store.connection.execute("SELECT COUNT(*) FROM versions").fetchone()
        assert count == (1,)
    finally:
        store.close()
