"""Secret generators for release specs, materialized through ``SecretVersionStore``.

Generators run only when a release is created, after its plan was validated,
and only for values that are not carried over from an earlier release of the
same spec. Values go straight into the private store; plans, catalogs,
journals and reports only ever see opaque references.

* ``random``: ``secrets.token_urlsafe(bytes)``.
* ``tls-self-signed``: an RSA key and a self-signed X.509 certificate.
* ``tls-ca``: a private CA and leaf certificates it signs. The CA key is an
  internal output: it never reaches a composition.
* ``template``: text with ``{name}`` placeholders, rendered here from the raw
  values of other outputs.
* ``import``: a value read from a file, an environment variable or a live
  Secret key, recorded as the first version.
* ``static``: a public value.
* ``sops``, ``vault``, ``aws-secrets-manager``: values read from an external
  source by :mod:`piceli.k8s.secret_sources` before the plan; carried over
  while the value (its keyed digest) is unchanged, else stored as ``fetched``.

Certificates come from a pinned ``openssl`` binary called with an explicit
argv (no shell). The optional ``openssl_sha256`` pins the binary's content;
without it the path alone is trusted. ``encoding = "base64"`` (the default)
suits ``Secret.data``; ``"raw"`` suits ``stringData`` and needs UTF-8 text.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import stat
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

from piceli.k8s.release_secret_spec import (
    GeneratorSpec,
    ImportSecretSpec,
    Output,
    RandomSecretSpec,
    SecretError,
    StaticSecretSpec,
    TemplateSecretSpec,
    TlsCaSpec,
    TlsLeafSpec,
    TlsSelfSignedSpec,
    check_rotation,
    generation_order,
    is_external,
    outputs,
    render_template,
    template_dependencies,
)
from piceli.k8s.release_spec import ReleaseSpecError

if TYPE_CHECKING:
    from piceli.k8s.secret_sources import Fetched

__all__ = [
    "Carry",
    "ImportSources",
    "Materialized",
    "SecretGeneratorError",
    "config_digest",
    "decode",
    "encode",
    "generate",
    "input_names",
    "materialize",
    "part_digests",
]

_OPENSSL_SECONDS = 60
_IMPORT_BYTES = 1_000_000

# (generator, part, part digest, output names) -> (encoded values, release) | None
Carry = Callable[[str, str, str, tuple[str, ...]], tuple[dict[str, str], str] | None]


class SecretGeneratorError(ReleaseSpecError):
    """openssl or its pin failed; nothing was stored."""

    code = "secret-generator-failed"

    def __init__(self, message: str) -> None:
        super().__init__(f"{message} [{self.code}]")


def input_names(name: str, spec: GeneratorSpec) -> tuple[str, ...]:
    """Session input names a composition may bind (internal outputs excluded)."""
    return tuple(item.name for item in outputs(name, spec) if not item.internal)


def _digest(material: Any) -> str:
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def config_digest(spec: GeneratorSpec) -> str:
    """Digest of the settings that shape a value (not of the value).

    A changed digest means a carried-over value no longer matches the spec, so
    it is regenerated. The tool path/pin is excluded (upgrading openssl must
    not rotate certificates), and so is an import's rotation policy and an
    external source's tool, credentials and time limits.
    """
    exclude = {"openssl", "openssl_sha256"}
    if isinstance(spec, ImportSecretSpec):
        exclude |= {"rotate", "bytes"}
    elif is_external(spec):
        # How the source is reached (tool, credentials, CA, time limits) does
        # not shape the value; where it is read from does.
        exclude |= {
            "sops",
            "sops_sha256",
            "pass_env",
            "timeout_seconds",
            "token_file",
            "token_env",
            "ca_file",
            "profile",
        }
    return _digest(spec.model_dump(mode="json", exclude=exclude))


def part_digests(spec: GeneratorSpec) -> dict[str, str]:
    """Per carry-over part digests; one part (``""``) except for ``tls-ca``."""
    if not isinstance(spec, TlsCaSpec):
        return {"": config_digest(spec)}
    ca = _digest(
        spec.model_dump(
            mode="json", include={"type", "common_name", "ca_days", "rsa_bits"}
        )
        | {"encoding": spec.encoding}
    )
    result = {"ca": ca}
    for leaf, settings in sorted(spec.leaves.items()):
        result[f"leaf:{leaf}"] = _digest(
            {
                "ca": ca,
                "leaf": leaf,
                "days": spec.days,
                "rsa_bits": spec.rsa_bits,
                **settings.model_dump(mode="json"),
            }
        )
    return result


def encode(raw: bytes, encoding: str) -> str:
    if encoding == "base64":
        return base64.b64encode(raw).decode()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise SecretError(
            "secret-not-text", 'the value is not UTF-8 text; use encoding = "base64"'
        ) from None


def decode(value: str, encoding: str) -> bytes:
    return base64.b64decode(value) if encoding == "base64" else value.encode()


# ------------------------------------------------------------------ openssl
def _pinned_openssl(spec: TlsSelfSignedSpec | TlsCaSpec) -> Path:
    from piceli.artifacts.process import ToolPin

    try:
        if spec.openssl_sha256 is not None:
            pin = ToolPin(spec.openssl.resolve(strict=True), spec.openssl_sha256)
            pin.verify()
        else:
            pin = ToolPin.capture(spec.openssl)
    except (OSError, ValueError) as error:
        raise SecretGeneratorError(f"openssl pin rejected: {error}") from None
    return pin.path


def _openssl(openssl: Path, *arguments: str) -> None:
    try:
        subprocess.run(
            [str(openssl), *arguments],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=_OPENSSL_SECONDS,
            env={"PATH": "/usr/bin:/bin"},
        )
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or b"").decode(errors="replace").strip()[-300:]
        raise SecretGeneratorError(f"openssl failed: {detail}") from None
    except subprocess.TimeoutExpired:
        raise SecretGeneratorError("openssl timed out") from None


def _private_tempdir() -> tempfile.TemporaryDirectory[str]:
    directory = tempfile.TemporaryDirectory(prefix="piceli-tls-")
    os.chmod(directory.name, 0o700)
    return directory


def _write_private_file(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)


def _subject_alt_names(dns_names: Sequence[str], ips: Sequence[str]) -> str:
    names = [f"DNS:{name}" for name in dns_names]
    names += [f"IP:{address}" for address in ips]
    return ",".join(names)


def _tls(spec: TlsSelfSignedSpec) -> tuple[bytes, bytes]:
    openssl = _pinned_openssl(spec)
    config = "\n".join(
        (
            "[req]",
            "distinguished_name = dn",
            "x509_extensions = v3",
            "prompt = no",
            "[dn]",
            f"CN = {spec.dns_names[0]}",
            "[v3]",
            "subjectAltName = " + _subject_alt_names(spec.dns_names, spec.ip_addresses),
            "keyUsage = critical,digitalSignature,keyEncipherment",
            "extendedKeyUsage = serverAuth,clientAuth",
            "",
        )
    )
    with _private_tempdir() as raw:
        directory = Path(raw)
        (directory / "req.cnf").write_text(config)
        _openssl(
            openssl,
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
        )
        return (directory / "tls.crt").read_bytes(), (
            directory / "tls.key"
        ).read_bytes()


def _tls_ca(
    spec: TlsCaSpec,
    ca: tuple[bytes, bytes] | None,
    leaves: Sequence[str],
) -> tuple[tuple[bytes, bytes], dict[str, tuple[bytes, bytes]]]:
    """Create (or reuse) the CA and sign ``leaves``; returns PEM bytes."""
    openssl = _pinned_openssl(spec)
    with _private_tempdir() as raw:
        directory = Path(raw)
        ca_crt, ca_key = directory / "ca.crt", directory / "ca.key"
        if ca is None:
            (directory / "ca.cnf").write_text(
                "\n".join(
                    (
                        "[req]",
                        "distinguished_name = dn",
                        "x509_extensions = v3",
                        "prompt = no",
                        "[dn]",
                        f"CN = {spec.common_name}",
                        "[v3]",
                        "basicConstraints = critical,CA:TRUE,pathlen:0",
                        "keyUsage = critical,keyCertSign,cRLSign",
                        "subjectKeyIdentifier = hash",
                        "",
                    )
                )
            )
            _openssl(
                openssl,
                "req",
                "-x509",
                "-newkey",
                f"rsa:{spec.rsa_bits}",
                "-nodes",
                "-sha256",
                "-days",
                str(spec.ca_days),
                "-config",
                str(directory / "ca.cnf"),
                "-keyout",
                str(ca_key),
                "-out",
                str(ca_crt),
            )
        else:
            _write_private_file(ca_crt, ca[0])
            _write_private_file(ca_key, ca[1])
        signed: dict[str, tuple[bytes, bytes]] = {}
        for leaf in leaves:
            settings: TlsLeafSpec = spec.leaves[leaf]
            common_name = (settings.dns_names or settings.ip_addresses)[0]
            config = directory / f"{leaf}.cnf"
            config.write_text(
                "\n".join(
                    (
                        "[req]",
                        "distinguished_name = dn",
                        "prompt = no",
                        "[dn]",
                        f"CN = {common_name}",
                        "[v3]",
                        "basicConstraints = critical,CA:FALSE",
                        "subjectAltName = "
                        + _subject_alt_names(settings.dns_names, settings.ip_addresses),
                        "keyUsage = critical,digitalSignature,keyEncipherment",
                        "extendedKeyUsage = serverAuth,clientAuth",
                        "authorityKeyIdentifier = keyid",
                        "",
                    )
                )
            )
            key, request, certificate = (
                directory / f"{leaf}.key",
                directory / f"{leaf}.csr",
                directory / f"{leaf}.crt",
            )
            _openssl(
                openssl,
                "req",
                "-new",
                "-newkey",
                f"rsa:{spec.rsa_bits}",
                "-nodes",
                "-sha256",
                "-config",
                str(config),
                "-keyout",
                str(key),
                "-out",
                str(request),
            )
            _openssl(
                openssl,
                "x509",
                "-req",
                "-in",
                str(request),
                "-CA",
                str(ca_crt),
                "-CAkey",
                str(ca_key),
                "-set_serial",
                f"0x{secrets.randbits(127):032x}",
                "-days",
                str(spec.days),
                "-sha256",
                "-extfile",
                str(config),
                "-extensions",
                "v3",
                "-out",
                str(certificate),
            )
            signed[leaf] = (certificate.read_bytes(), key.read_bytes())
        return (ca_crt.read_bytes(), ca_key.read_bytes()), signed


def generate(name: str, spec: GeneratorSpec) -> dict[str, str]:
    """Fresh encoded values for a self-contained generator (no carry-over).

    ``template`` and ``import`` need other values or sources; use
    :func:`materialize` for them.
    """
    raw = _fresh(name, spec, None, ())
    return {output: encode(value, spec.encoding) for output, value in raw.items()}


def _fresh(
    name: str,
    spec: GeneratorSpec,
    ca: tuple[bytes, bytes] | None,
    leaves: Sequence[str],
) -> dict[str, bytes]:
    if isinstance(spec, RandomSecretSpec):
        return {name: secrets.token_urlsafe(spec.bytes).encode()}
    if isinstance(spec, TlsSelfSignedSpec):
        certificate, key = _tls(spec)
        return {f"{name}.crt": certificate, f"{name}.key": key}
    if isinstance(spec, TlsCaSpec):
        wanted = leaves if ca is not None else sorted(spec.leaves)
        (ca_crt, ca_key), signed = _tls_ca(spec, ca, wanted)
        result = {f"{name}.ca.crt": ca_crt, f"{name}.ca.key": ca_key}
        for leaf, (certificate, key) in signed.items():
            result[f"{name}.{leaf}.crt"] = certificate
            result[f"{name}.{leaf}.key"] = key
        return result
    if isinstance(spec, StaticSecretSpec):
        return {name: spec.value.encode()}
    raise ReleaseSpecError(f"secret {name!r} ({spec.type}) needs materialize()")


# ------------------------------------------------------------------ imports
@dataclass
class ImportSources:
    """Where ``import`` generators read from; nothing is read until needed.

    ``resolve`` maps a spec-relative file path to an absolute one;
    ``read_secret(name, key)`` reads one live Secret key through the release's
    explicit kubeconfig (``None`` when no cluster connection exists).
    """

    resolve: Callable[[Path], Path] = lambda path: path
    read_secret: Callable[[str, str], bytes] | None = None
    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)

    def read(self, name: str, spec: ImportSecretSpec) -> bytes:
        source = spec.source()
        try:
            if spec.file is not None:
                value = self._file(self.resolve(spec.file))
            elif spec.env is not None:
                if spec.env not in self.environ:
                    raise LookupError("the environment variable is not set")
                value = self.environ[spec.env].encode()
            else:
                assert spec.secret is not None
                if self.read_secret is None:
                    raise LookupError("no cluster connection for a live import")
                value = self.read_secret(spec.secret.name, spec.secret.key)
        except SecretError:
            raise
        except OSError as error:
            self._refuse(name, source, error.strerror or type(error).__name__)
        except LookupError as error:
            self._refuse(name, source, str(error.args[0]) if error.args else "missing")
        except Exception as error:  # a category, never content
            self._refuse(name, source, getattr(error, "category", type(error).__name__))
        if spec.trim_newline and spec.secret is None:
            value = value.removesuffix(b"\n").removesuffix(b"\r")
        if not value:
            self._refuse(name, source, "the value is empty")
        return value

    @staticmethod
    def _refuse(name: str, source: str, reason: str) -> NoReturn:
        raise SecretError(
            "secret-import-unavailable",
            f"cannot import secret {name!r} from {source}: {reason}",
        )

    @staticmethod
    def _file(path: Path) -> bytes:
        descriptor = os.open(path, os.O_RDONLY)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise LookupError("not a regular file")
            value = stream.read(_IMPORT_BYTES + 1)
        if len(value) > _IMPORT_BYTES:
            raise LookupError("the file is larger than 1 MB")
        return value


# -------------------------------------------------------------- materialize
@dataclass
class Materialized:
    """Every output value (encoded) plus public bookkeeping for the release.

    ``origin`` per generator: ``generated``, ``imported``, ``rotated``,
    ``rendered``, ``static``, ``fetched`` (a new or changed external value),
    ``carried:<release>`` or, for a ``tls-ca``
    whose CA was carried, ``signed:<release>``.
    """

    values: dict[str, str] = field(repr=False)
    origin: dict[str, str]
    parts: dict[str, dict[str, str]]
    meta: dict[str, dict[str, Any]]


def describe(name: str, spec: GeneratorSpec) -> dict[str, Any]:
    """Public metadata of one generator (types, keys, sources; never values)."""
    items = outputs(name, spec)
    value: dict[str, Any] = {
        "type": spec.type,
        "encoding": spec.encoding,
        "outputs": {item.key or "": item.name for item in items},
        "internal": [item.name for item in items if item.internal],
    }
    if isinstance(spec, ImportSecretSpec):
        value["source"] = spec.source()
        value["rotate"] = spec.rotate
    elif is_external(spec):
        value["source"] = spec.source()  # type: ignore[union-attr]
    return value


def materialize(
    secrets_spec: Mapping[str, GeneratorSpec],
    *,
    rotate: Sequence[str] = (),
    carry: Carry | None = None,
    sources: ImportSources | None = None,
    external: Mapping[str, Fetched] | None = None,
) -> Materialized:
    """Produce every output, in dependency order, carrying values over.

    ``external`` holds the values already read from external sources (see
    :func:`piceli.k8s.secret_sources.fetch_all`). Nothing here writes to the
    store; the caller stores the result only after the plan was accepted.
    """
    check_rotation(secrets_spec, rotate)
    sources = sources or ImportSources()
    dependencies = template_dependencies(secrets_spec)
    raw: dict[str, bytes] = {}
    values: dict[str, str] = {}
    origin: dict[str, str] = {}
    parts: dict[str, dict[str, str]] = {}
    meta: dict[str, dict[str, Any]] = {}
    for name in generation_order(secrets_spec):
        spec = secrets_spec[name]
        digests = part_digests(spec)
        fetched = (external or {}).get(name)
        if is_external(spec):
            if fetched is None:
                raise SecretError(
                    "secret-source-failed",
                    f"secret {name!r} was not read from its source before the plan",
                )
            # The keyed value digest makes a changed value a new version.
            digests = {"": _digest({"config": digests[""], "value": fetched.digest})}
        parts[name] = digests
        meta[name] = describe(name, spec)
        if fetched is not None and is_external(spec):
            found = carry(name, "", digests[""], (name,)) if carry is not None else None
            if found is not None:
                fresh = {
                    key: decode(value, spec.encoding) for key, value in found[0].items()
                }
                origin[name] = f"carried:{found[1]}"
            else:
                fresh = {name: fetched.value}
                origin[name] = "fetched"
        elif isinstance(spec, TemplateSecretSpec):
            texts: dict[str, str] = {}
            for placeholder, output in dependencies[name].items():
                try:
                    texts[placeholder] = raw[output].decode("utf-8")
                except UnicodeDecodeError:
                    raise SecretError(
                        "secret-not-text",
                        f"template {name!r} uses {output!r}, which is not UTF-8 text",
                    ) from None
            meta[name]["depends_on"] = sorted(set(dependencies[name].values()))
            fresh = {name: render_template(spec.template, texts).encode()}
            origin[name] = "rendered"
        elif isinstance(spec, StaticSecretSpec):
            fresh = _fresh(name, spec, None, ())
            origin[name] = "static"
        else:
            fresh, origin[name] = _carried_or_new(
                name, spec, digests, name in rotate, carry, sources
            )
        for output, value in fresh.items():
            raw[output] = value
            values[output] = encode(value, spec.encoding)
    return Materialized(values, origin, parts, meta)


def _carried_or_new(
    name: str,
    spec: GeneratorSpec,
    digests: Mapping[str, str],
    rotating: bool,
    carry: Carry | None,
    sources: ImportSources,
) -> tuple[dict[str, bytes], str]:
    groups: dict[str, list[Output]] = {}
    for item in outputs(name, spec):
        groups.setdefault(item.part, []).append(item)

    def carried(part: str) -> tuple[dict[str, bytes], str] | None:
        if rotating or carry is None:
            return None
        found = carry(
            name, part, digests[part], tuple(item.name for item in groups[part])
        )
        if found is None:
            return None
        stored, release = found
        return {key: decode(value, spec.encoding) for key, value in stored.items()}, (
            release
        )

    if isinstance(spec, TlsCaSpec):
        ca = carried("ca")
        if ca is None:
            return _fresh(name, spec, None, ()), "rotated" if rotating else "generated"
        result = dict(ca[0])
        missing = []
        for leaf in sorted(spec.leaves):
            leaf_values = carried(f"leaf:{leaf}")
            if leaf_values is None:
                missing.append(leaf)
            else:
                result.update(leaf_values[0])
        if missing:
            authority = (result[f"{name}.ca.crt"], result[f"{name}.ca.key"])
            result.update(
                {
                    key: value
                    for key, value in _fresh(name, spec, authority, missing).items()
                    if key not in {f"{name}.ca.crt", f"{name}.ca.key"}
                }
            )
            return result, f"signed:{ca[1]}"
        return result, f"carried:{ca[1]}"
    found = carried("")
    if found is not None:
        return found[0], f"carried:{found[1]}"
    if isinstance(spec, ImportSecretSpec):
        if rotating and spec.rotate == "random":
            return {name: secrets.token_urlsafe(spec.bytes).encode()}, "rotated"
        return {name: sources.read(name, spec)}, "rotated" if rotating else "imported"
    return _fresh(name, spec, None, ()), "rotated" if rotating else "generated"
