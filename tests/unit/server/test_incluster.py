"""Explicit service-account source never serializes or stages token values."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from piceli.k8s.cli.ui import cluster_observe
from piceli.k8s.ops.provider_factory import api_client_from_kubeconfig
from piceli.server.incluster import InClusterCredential


def test_projected_token_file_is_referenced_and_rotates(tmp_path: Path) -> None:
    ca = tmp_path / "ca.crt"
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-ca")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(hours=1))
        .sign(key, hashes.SHA256())
    )
    ca.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    token = tmp_path / "token"
    token.write_text("first-secret-token")
    credential = InClusterCredential(
        "https://kubernetes.default.svc:443", ca, token, "shop"
    )
    target = credential.write_kubeconfig(tmp_path / "control" / "target.kubeconfig")
    assert target.context == "piceli-incluster"
    assert target.namespace == "shop"
    assert target.kubeconfig.stat().st_mode & 0o777 == 0o600
    assert "first-secret-token" not in target.kubeconfig.read_text()
    document = yaml.safe_load(target.kubeconfig.read_text())
    assert document["users"][0]["user"] == {"tokenFile": str(token)}
    client = api_client_from_kubeconfig(target.kubeconfig, target.context)
    try:
        assert (
            client.configuration.get_api_key_with_prefix("BearerToken")
            == "Bearer first-secret-token"
        )
        token.write_text("second-secret-token")
        assert (
            client.configuration.get_api_key_with_prefix("BearerToken")
            == "Bearer second-secret-token"
        )
    finally:
        client.close()


def test_incluster_source_refuses_ambient_or_untrusted_inputs(tmp_path: Path) -> None:
    ca = tmp_path / "ca.crt"
    ca.write_text("test-ca")
    token = tmp_path / "token"
    token.write_text("secret")
    with pytest.raises(ValueError, match="HTTPS origin"):
        InClusterCredential("http://kubernetes.default.svc", ca, token, "shop")
    with pytest.raises(ValueError, match="loopback"):
        InClusterCredential("https://127.0.0.1:6443", ca, token, "shop")
    with pytest.raises(ValueError, match="explicit readable"):
        InClusterCredential(
            "https://kubernetes.default.svc", ca, tmp_path / "absent", "shop"
        )


def test_cluster_command_wires_only_explicit_read_only_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ca = tmp_path / "ca.crt"
    ca.write_text("ca mounted by installer")
    token = tmp_path / "token"
    token.write_text("projected-token")
    servers = []
    monkeypatch.setattr("uvicorn.run", lambda server, **_kwargs: servers.append(server))
    cluster_observe(
        api_server="https://kubernetes.default.svc:443",
        ca_file=ca,
        token_file=token,
        namespace="shop",
        control_dir=tmp_path / "control",
        origin="https://piceli.example.test",
        oidc_issuer="https://id.example.test",
        oidc_metadata_url="https://id.example.test/.well-known/openid-configuration",
        oidc_client_id="piceli-test",
        authorized_sub=["operator-1"],
        name="Shop",
        host="127.0.0.1",
        port=8000,
        url_prefix="",
    )
    assert len(servers) == 1
    service = servers[0].state.service
    kinds = {kind for _, kind in service.registrations["cluster"].kinds}
    assert "Pod" in kinds and "Secret" not in kinds and "ConfigMap" not in kinds
    assert service.scope_policy is not None
    assert (tmp_path / "control" / "target.kubeconfig").is_file()
    service.close()
