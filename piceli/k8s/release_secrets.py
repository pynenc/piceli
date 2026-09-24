"""Secret generators for release specs, materialized through ``SecretVersionStore``.

Generators run only when a release is created and a value is not carried over
from an earlier release of the same spec. Values go straight into the private
store as session inputs; plans, catalogs, journals and reports only ever see
opaque references.

* ``random``: ``secrets.token_urlsafe(bytes)``.
* ``tls-self-signed``: an RSA key and a self-signed X.509 certificate for the
  given DNS names/IPs, produced by a pinned ``openssl`` binary called with an
  explicit argv (no shell). The optional ``openssl_sha256`` pins the binary's
  content; without it the path alone is trusted.

Each generator yields named inputs: ``<name>`` for ``random`` and
``<name>.crt`` / ``<name>.key`` for ``tls-self-signed``. ``encoding = "base64"``
(the default) suits ``Secret.data``; ``"raw"`` suits ``stringData``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import subprocess
import tempfile
from pathlib import Path

from piceli.k8s.release_spec import (
    RandomSecretSpec,
    ReleaseSpecError,
    TlsSelfSignedSpec,
)

GeneratorSpec = RandomSecretSpec | TlsSelfSignedSpec
_OPENSSL_SECONDS = 60


def input_names(name: str, spec: GeneratorSpec) -> tuple[str, ...]:
    """Session input names produced by one generator."""
    if isinstance(spec, TlsSelfSignedSpec):
        return (f"{name}.crt", f"{name}.key")
    return (name,)


def config_digest(spec: GeneratorSpec) -> str:
    """Digest of the settings that shape a value (not of the value).

    A changed digest means a carried-over value no longer matches the spec, so
    it is regenerated. The tool path/pin is excluded: upgrading openssl must
    not rotate certificates.
    """
    material = spec.model_dump(mode="json", exclude={"openssl", "openssl_sha256"})
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _encode(value: str, encoding: str) -> str:
    return base64.b64encode(value.encode()).decode() if encoding == "base64" else value


def _pinned_openssl(spec: TlsSelfSignedSpec) -> Path:
    from piceli.artifacts.process import ToolPin

    try:
        if spec.openssl_sha256 is not None:
            pin = ToolPin(spec.openssl.resolve(strict=True), spec.openssl_sha256)
            pin.verify()
        else:
            pin = ToolPin.capture(spec.openssl)
    except (OSError, ValueError) as error:
        raise ReleaseSpecError(f"openssl pin rejected: {error}") from None
    return pin.path


def _tls(spec: TlsSelfSignedSpec) -> tuple[str, str]:
    openssl = _pinned_openssl(spec)
    names = [f"DNS:{name}" for name in spec.dns_names]
    names += [f"IP:{address}" for address in spec.ip_addresses]
    config = "\n".join(
        (
            "[req]",
            "distinguished_name = dn",
            "x509_extensions = v3",
            "prompt = no",
            "[dn]",
            f"CN = {spec.dns_names[0]}",
            "[v3]",
            "subjectAltName = " + ",".join(names),
            "keyUsage = critical,digitalSignature,keyEncipherment",
            "extendedKeyUsage = serverAuth,clientAuth",
            "",
        )
    )
    with tempfile.TemporaryDirectory(prefix="piceli-tls-") as raw:
        directory = Path(raw)
        os.chmod(directory, 0o700)
        (directory / "req.cnf").write_text(config)
        argv = [
            str(openssl),
            "req",
            "-x509",
            "-newkey",
            f"rsa:{spec.rsa_bits}",
            "-nodes",
            "-sha256",
            "-days",
            str(spec.days),
            "-config",
            str(directory / "req.cnf"),
            "-keyout",
            str(directory / "tls.key"),
            "-out",
            str(directory / "tls.crt"),
        ]
        try:
            subprocess.run(
                argv,
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=_OPENSSL_SECONDS,
                env={"PATH": "/usr/bin:/bin"},
            )
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or b"").decode(errors="replace").strip()[-300:]
            raise ReleaseSpecError(f"openssl failed: {detail}") from None
        except subprocess.TimeoutExpired:
            raise ReleaseSpecError("openssl timed out") from None
        return (directory / "tls.crt").read_text(), (directory / "tls.key").read_text()


def generate(name: str, spec: GeneratorSpec) -> dict[str, str]:
    """Produce fresh values for every input of one generator."""
    if isinstance(spec, TlsSelfSignedSpec):
        certificate, key = _tls(spec)
        return {
            f"{name}.crt": _encode(certificate, spec.encoding),
            f"{name}.key": _encode(key, spec.encoding),
        }
    return {name: _encode(secrets.token_urlsafe(spec.bytes), spec.encoding)}
