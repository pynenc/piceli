"""Release spec parsing, image pinning, receipts and composition loading."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from piceli.k8s.release_secrets import config_digest, generate, input_names
from piceli.k8s.release_spec import (
    RandomSecretSpec,
    ReleaseSpec,
    ReleaseSpecError,
    TlsSelfSignedSpec,
    load_build_receipt,
)

DIGEST = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64


def base(**overrides):
    value = {
        "target": {"kubeconfig": "kc", "context": "ctx", "namespace": "demo"},
        "release": {
            "name": "web",
            "owner": "owner",
            "field_manager": "manager",
            "composition": "compose.py:build",
            "state_dir": "state",
        },
        "images": {"web": f"docker.io/library/nginx@{DIGEST}"},
    }
    value.update(overrides)
    return value


def test_valid_spec_resolves_paths_relative_to_spec(tmp_path):
    spec = ReleaseSpec.from_dict(base(), tmp_path)
    assert spec.state_dir == tmp_path / "state"
    assert spec.catalog_path == tmp_path / "state" / "catalog.json"
    target = spec.kubeconfig_target()
    assert target.kubeconfig == tmp_path / "kc"
    assert target.context == "ctx"
    assert target.transport == "https"
    image = spec.images()["web"]
    assert image.reference == f"docker.io/library/nginx@{DIGEST}"
    assert image.identity == DIGEST


@pytest.mark.parametrize(
    "mutation",
    [
        lambda v: v.update(extra=1),
        lambda v: v["target"].update(current_context=True),
        lambda v: v["release"].update(name="Bad_Name"),
        lambda v: v["release"].update(composition="no-function"),
        lambda v: v.update(images={"web": "nginx:latest"}),
        lambda v: v.update(images={}),
        lambda v: v.update(secrets={"s": {"type": "random", "bytes": 4}}),
        lambda v: v.update(secrets={"s": {"type": "vault"}}),
        lambda v: v.update(
            secrets={"t": {"type": "tls-self-signed", "dns_names": ["bad name"]}}
        ),
        lambda v: v.update(
            secrets={
                "t": {
                    "type": "tls-self-signed",
                    "dns_names": ["a"],
                    "openssl": "openssl",
                }
            }
        ),
        lambda v: v["target"].pop("context"),
    ],
)
def test_invalid_specs_are_rejected(tmp_path, mutation):
    value = base()
    mutation(value)
    with pytest.raises(ReleaseSpecError):
        spec = ReleaseSpec.from_dict(value, tmp_path)
        spec.images()


def test_image_forms(tmp_path):
    spec = ReleaseSpec.from_dict(
        base(
            images={
                "bare": DIGEST,
                "table": {"ref": "registry.test:5000/app:v1", "digest": OTHER},
            }
        ),
        tmp_path,
    )
    images = spec.images()
    assert images["table"].reference == f"registry.test:5000/app@{OTHER}"
    assert images["table"].tag == "v1"
    with pytest.raises(ReleaseSpecError, match="no repository"):
        _ = images["bare"].reference
    with pytest.raises(ReleaseSpecError, match="ref digest"):
        ReleaseSpec.from_dict(
            base(images={"x": {"ref": f"app@{DIGEST}", "digest": OTHER}}), tmp_path
        ).images()


def _receipt(path: Path, images: dict) -> None:
    path.write_text(
        json.dumps(
            {"revision": "piceli.build-receipt.v1", "outputs": {"images": images}}
        )
    )


def test_build_receipt_contract(tmp_path):
    receipt = tmp_path / "build.receipt.json"
    _receipt(
        receipt,
        {
            "api": {
                "image_id": OTHER,
                "digest": DIGEST,
                "platform": "linux/arm64",
                "ref": "registry.test/app/api:r1",
                "future_field": True,
            },
            "local": {"image_id": OTHER, "digest": None, "ref": "app/local:r1"},
        },
    )
    images = load_build_receipt(receipt)
    assert images["api"].identity == DIGEST
    assert images["api"].reference == f"registry.test/app/api@{DIGEST}"
    assert images["api"].platform == "linux/arm64"
    # Without a registry digest the local image ID is the identity and the
    # tag is the reference (for runtimes that received the image directly).
    assert images["local"].identity == OTHER
    assert images["local"].reference == "app/local:r1"

    spec = ReleaseSpec.from_dict(
        base(images_from="build.receipt.json", images={}), tmp_path
    )
    assert sorted(spec.images()) == ["api", "local"]
    collision = ReleaseSpec.from_dict(
        base(images_from="build.receipt.json", images={"api": DIGEST}), tmp_path
    )
    with pytest.raises(ReleaseSpecError, match="both"):
        collision.images()


@pytest.mark.parametrize(
    "document",
    [
        {"revision": "other", "outputs": {"images": {"a": {"image_id": DIGEST}}}},
        {"revision": "piceli.build-receipt.v1", "outputs": {}},
        {
            "revision": "piceli.build-receipt.v1",
            "outputs": {"images": {"a": {"image_id": "latest"}}},
        },
        {
            "revision": "piceli.build-receipt.v1",
            "outputs": {"images": {"a": {"image_id": DIGEST, "digest": "md5:x"}}},
        },
    ],
)
def test_invalid_receipts(tmp_path, document):
    path = tmp_path / "r.json"
    path.write_text(json.dumps(document))
    with pytest.raises(ReleaseSpecError):
        load_build_receipt(path)


def test_composition_entry_points(tmp_path):
    (tmp_path / "compose.py").write_text("def build(ctx):\n    return ctx.namespace\n")
    spec = ReleaseSpec.from_dict(base(), tmp_path)
    function = spec.load_composition()
    assert function(spec.context(spec.images(), {})) == "demo"

    module_spec = ReleaseSpec.from_dict(
        base(
            release=base()["release"] | {"composition": "json:dumps"},
        ),
        tmp_path,
    )
    assert module_spec.load_composition() is json.dumps
    missing = ReleaseSpec.from_dict(
        base(release=base()["release"] | {"composition": "missing.py:build"}),
        tmp_path,
    )
    with pytest.raises(ReleaseSpecError, match="not found"):
        missing.load_composition()


def test_context_reports_unknown_names(tmp_path):
    spec = ReleaseSpec.from_dict(base(), tmp_path)
    context = spec.context(spec.images(), {})
    with pytest.raises(ReleaseSpecError, match="unknown image"):
        context.image("api")
    with pytest.raises(ReleaseSpecError, match="unknown secret"):
        context.secret("token")


def test_random_generator():
    spec = RandomSecretSpec(type="random", bytes=24)
    assert input_names("token", spec) == ("token",)
    first = generate("token", spec)["token"]
    assert len(base64.b64decode(first)) == 32  # token_urlsafe(24) is 32 chars
    assert generate("token", spec)["token"] != first
    raw = generate("token", RandomSecretSpec(type="random", encoding="raw"))["token"]
    assert raw.isascii() and len(raw) >= 32
    assert config_digest(spec) != config_digest(RandomSecretSpec(type="random"))


@pytest.mark.skipif(not Path("/usr/bin/openssl").exists(), reason="needs openssl")
def test_tls_self_signed_generator():
    import subprocess

    spec = TlsSelfSignedSpec(
        type="tls-self-signed",
        dns_names=("web.demo.svc", "web"),
        ip_addresses=("127.0.0.1",),
        days=2,
    )
    assert input_names("web-tls", spec) == ("web-tls.crt", "web-tls.key")
    values = generate("web-tls", spec)
    certificate = base64.b64decode(values["web-tls.crt"]).decode()
    key = base64.b64decode(values["web-tls.key"]).decode()
    assert certificate.startswith("-----BEGIN CERTIFICATE-----")
    assert "PRIVATE KEY-----" in key
    text = subprocess.run(
        ["/usr/bin/openssl", "x509", "-noout", "-text"],
        input=certificate.encode(),
        capture_output=True,
        check=True,
    ).stdout.decode()
    assert "DNS:web.demo.svc" in text and "DNS:web" in text
    assert "127.0.0.1" in text
    # The tool pin is not part of the value-shaping configuration.
    pinned = spec.model_copy(update={"openssl_sha256": "sha256:" + "0" * 64})
    assert config_digest(pinned) == config_digest(spec)
    with pytest.raises(ReleaseSpecError, match="pin"):
        generate("web-tls", pinned)


def test_tls_generator_requires_the_pinned_tool(tmp_path):
    fake = tmp_path / "openssl"
    spec = TlsSelfSignedSpec(type="tls-self-signed", dns_names=("a",), openssl=fake)
    with pytest.raises(ReleaseSpecError, match="pin"):
        generate("tls", spec)
