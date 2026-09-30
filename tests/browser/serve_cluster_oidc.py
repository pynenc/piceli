"""Disposable HTTPS cluster UI with a signed local OIDC issuer and fake API."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import Request, Response

from piceli.k8s.ops.provider_factory import KubeconfigTarget
from piceli.server.app import create_app
from piceli.server.cluster_security import ClusterSecurity, ClusterSecurityConfig
from piceli.server.security import uvicorn_log_config
from piceli.services.authority import ScopePolicy, request_principal
from piceli.services.contracts import Principal
from piceli.services.query import QueryService
from piceli.services.registration import Registration
from piceli.testing import fake_cluster, manifest
from tests.acceptance.test_ui_cluster_auth import issuer


def certificate(directory: Path) -> tuple[Path, Path]:
    """A throwaway localhost certificate kept only in the runner's tempdir."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / "localhost.crt"
    key_path = directory / "localhost.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    return cert_path, key_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    directory = Path(os.environ["PICELI_OIDC_FIXTURE_DIR"]).resolve(strict=True)
    cert_path, key_path = certificate(directory)
    with issuer() as (issuer_url, _flags), fake_cluster() as cluster:
        cluster.api.put(manifest("Deployment", "web"), owned=True)
        target = KubeconfigTarget(
            cluster.kubeconfig(directory / "kubeconfig"),
            "fake",
            cluster.namespace,
            cluster_uid="cluster-uid",
            namespace_uid="namespace-uid",
            transport="loopback-http",
        )
        principal_id = hashlib.sha256(
            (issuer_url + "\0operator-1").encode()
        ).hexdigest()
        policy = ScopePolicy({principal_id: {"shop": frozenset({"inspect"})}})
        query = QueryService(
            [
                Registration("shop", "Shop", target),
                Registration("other", "Other", target),
            ],
            scope_policy=policy,
        )
        with request_principal(
            Principal(id=principal_id, name="Operator", kind="oidc")
        ):
            preflight = query.resources("shop", fresh=True)
        if preflight.partial or [item.identity.name for item in preflight.items] != [
            "web"
        ]:
            raise RuntimeError(
                f"fake observation preflight failed: {preflight.partial}"
            )
        origin = f"https://127.0.0.1:{args.port}"
        security = ClusterSecurity(
            ClusterSecurityConfig(
                origin=origin,
                issuer=issuer_url,
                metadata_url=issuer_url + "/.well-known/openid-configuration",
                client_id="piceli-test",
                allow_insecure_loopback_test=True,
            )
        )
        app = create_app(query, origin=origin, cluster_security=security)

        @app.post("/__fixture/revoke", include_in_schema=False)
        def revoke(request: Request) -> Response:
            if security.principal(request) is None:
                return Response(status_code=403)
            policy.replace({})
            return Response(status_code=204)

        uvicorn.run(
            app,
            host="127.0.0.1",
            port=args.port,
            ssl_certfile=str(cert_path),
            ssl_keyfile=str(key_path),
            log_level="warning",
            log_config=uvicorn_log_config(),
        )
        assert all(request["method"] == "GET" for request in cluster.api.requests)


if __name__ == "__main__":
    main()
