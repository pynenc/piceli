"""Offline fakes for external secret sources: a Vault server, a sops binary, AWS.

* :class:`FakeVault` is an in-process KV v2 server on a loopback port, with
  optional TLS (a throwaway self-signed certificate), token and namespace
  checks, failure injection and a request log.
* :func:`fake_sops` writes an executable ``sops`` stand-in. Its "encrypted"
  file is plain JSON ``{"plain": <document>, "exit": <code>, "sleep": <s>}``;
  it prints ``plain`` as JSON (or its ``binary`` string for
  ``--output-type binary``) and logs its environment names next to the file.
* :func:`stubbed_aws` returns a botocore Secrets Manager client wired to a
  ``Stubber`` (no network, fake credentials).
* :func:`isolate_credentials` points ``HOME`` and every credential location at
  an empty temporary directory, so no test reads the developer's ``~/.aws``,
  ``~/.vault-token`` or ``~/.config/sops``.

No test touches the network beyond 127.0.0.1 or uses real credentials.
"""

from __future__ import annotations

import datetime
import json
import ssl
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

VAULT_TOKEN = "fake-vault-token-for-tests"
_CREDENTIAL_ENV = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_ROLE_ARN",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "VAULT_TOKEN",
    "VAULT_ADDR",
    "VAULT_NAMESPACE",
    "SOPS_AGE_KEY",
    "SOPS_AGE_KEY_FILE",
    "GNUPGHOME",
    "XDG_CONFIG_HOME",
)


def isolate_credentials(monkeypatch: pytest.MonkeyPatch, home: Path) -> Path:
    """An empty ``HOME`` and no ambient AWS/Vault/SOPS credentials."""
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    for name in _CREDENTIAL_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(home / "missing-aws-config"))
    monkeypatch.setenv(
        "AWS_SHARED_CREDENTIALS_FILE", str(home / "missing-aws-credentials")
    )
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    return home


# ------------------------------------------------------------------ vault
@dataclass
class FakeVault:
    """KV v2 secrets under ``(mount, path)``: a list of versions (dicts)."""

    token: str = VAULT_TOKEN
    namespace: str | None = None
    secrets: dict[tuple[str, str], list[dict[str, Any]]] = field(default_factory=dict)
    requests: list[dict[str, Any]] = field(default_factory=list)
    status: int | None = None  # force an HTTP status
    delay: float = 0.0
    address: str = ""
    ca_file: Path | None = None

    def put(self, path: str, data: dict[str, Any], mount: str = "secret") -> None:
        self.secrets.setdefault((mount, path), []).append(dict(data))

    def handler(self) -> type[BaseHTTPRequestHandler]:
        vault = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                pass

            def _send(self, status: int, body: dict[str, Any]) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                vault.requests.append(
                    {"path": self.path, "headers": dict(self.headers.items())}
                )
                if vault.delay:
                    time.sleep(vault.delay)
                if vault.status is not None:
                    return self._send(vault.status, {"errors": ["forced"]})
                if self.headers.get("X-Vault-Token") != vault.token:
                    return self._send(403, {"errors": ["permission denied"]})
                if self.headers.get("X-Vault-Namespace") != vault.namespace:
                    return self._send(403, {"errors": ["wrong namespace"]})
                location, _, query = self.path.partition("?")
                parts = location.split("/")
                # /v1/<mount>/data/<path...>
                if len(parts) < 5 or parts[1] != "v1" or parts[3] != "data":
                    return self._send(404, {"errors": []})
                versions = vault.secrets.get((parts[2], "/".join(parts[4:])))
                if not versions:
                    return self._send(404, {"errors": []})
                number = len(versions)
                if query.startswith("version="):
                    number = int(query.split("=", 1)[1])
                if not 1 <= number <= len(versions):
                    return self._send(404, {"errors": []})
                return self._send(
                    200,
                    {
                        "data": {
                            "data": versions[number - 1],
                            "metadata": {"version": number},
                        }
                    },
                )

        return Handler


def _self_signed(directory: Path) -> tuple[Path, Path]:
    crypto = pytest.importorskip("cryptography")
    del crypto
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "vault.crt", directory / "vault.key"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


@contextmanager
def serve_vault(
    vault: FakeVault, *, tls_dir: Path | None = None
) -> Iterator[FakeVault]:
    """Serve ``vault`` on 127.0.0.1 (TLS when ``tls_dir`` is given)."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), vault.handler())
    server.daemon_threads = True
    scheme = "http"
    if tls_dir is not None:
        cert, key = _self_signed(tls_dir)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        server.socket = context.wrap_socket(server.socket, server_side=True)
        vault.ca_file = cert
        scheme = "https"
    vault.address = f"{scheme}://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield vault
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ------------------------------------------------------------------- sops
_SOPS_SCRIPT = """#!{python}
import json, os, sys, time
args = sys.argv[1:]
path = args[-1]
with open(path + ".env.json", "w") as log:
    json.dump(sorted(os.environ), log)
with open(path + ".argv.json", "w") as log:
    json.dump(args, log)
with open(path) as stream:
    document = json.load(stream)
if document.get("sleep"):
    time.sleep(document["sleep"])
if document.get("exit"):
    sys.stderr.write("fake sops failure with a leaked-looking secret-value\\n")
    sys.exit(document["exit"])
if "--output-type" in args and args[args.index("--output-type") + 1] == "binary":
    sys.stdout.write(document["binary"])
else:
    sys.stdout.write(json.dumps(document["plain"]))
"""


def fake_sops(directory: Path) -> Path:
    """An executable stand-in for ``sops`` (see the module docstring)."""
    directory.mkdir(parents=True, exist_ok=True)
    tool = directory / "sops"
    tool.write_text(_SOPS_SCRIPT.format(python=sys.executable))
    tool.chmod(0o755)
    return tool


def sops_file(path: Path, plain: Any, **options: Any) -> Path:
    """A fake "encrypted" file whose decryption is ``plain``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"plain": plain, **options}))
    return path


# -------------------------------------------------------------------- aws
def stubbed_aws(region: str = "eu-west-1") -> tuple[Any, Any]:
    """A Secrets Manager client with a ``Stubber`` (activated) and fake keys."""
    pytest.importorskip("botocore")
    import botocore.session
    from botocore.stub import Stubber

    session = botocore.session.Session()
    client = session.create_client(
        "secretsmanager",
        region_name=region,
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
    stubber = Stubber(client)
    stubber.activate()
    return client, stubber


def aws_value(
    name: str, *, string: str | None = None, binary: bytes | None = None
) -> dict[str, Any]:
    """A ``GetSecretValue`` response."""
    response: dict[str, Any] = {
        "ARN": f"arn:aws:secretsmanager:eu-west-1:123456789012:secret:{name}-AbCdEf",
        "Name": name,
        "VersionId": "a1b2c3d4-5678-90ab-cdef-EXAMPLE11111",
        "VersionStages": ["AWSCURRENT"],
    }
    if string is not None:
        response["SecretString"] = string
    if binary is not None:
        response["SecretBinary"] = binary
    return response
