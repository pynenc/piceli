"""External secret sources: SOPS files, HashiCorp Vault KV v2, AWS Secrets Manager.

An external source is read at **every** ``plan`` (the value may have been
rotated upstream), before the release is named. Each value is reduced to a
keyed digest (HMAC-SHA256 under a random key kept in the private state
directory, ``secret-sources.key``); the digest is part of the release
fingerprint and of the carry-over bookkeeping, so:

* an unchanged value names the same release (nothing new is stored);
* a changed value names a new release whose plan stores a **new version**
  (origin ``fetched``); other generators are carried over;
* a refused plan stores nothing: values stay in memory until the release is
  created, exactly like the other generators.

Without the key, the digest reveals nothing about the value (no dictionary
attack on a short password), and neither the digest nor the value is ever
printed. Errors carry a registered code (``secret-source-*``) and a category,
never a value, a token, process output or a server message.

* ``sops``: the ``sops`` binary (``sops --decrypt``) runs with an explicit
  argv (no shell), no stdin, ``PATH`` and ``HOME`` plus the variables named in
  ``pass_env``, a timeout and an output limit; its output is parsed here and
  its stderr is discarded (it is never shown).
* ``vault``: one ``GET /v1/<mount>/data/<path>`` over verified TLS with the
  token in the ``X-Vault-Token`` header (read from a file or an environment
  variable, never argv); no redirects, no proxies.
* ``aws-secrets-manager``: ``GetSecretValue`` through botocore (the
  ``piceli[aws]`` extra), imported only when such a secret is read.

Importing this module has no side effects.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

from piceli.k8s.release_secret_spec import (
    AwsSecretSpec,
    ExternalSpec,
    GeneratorSpec,
    SecretError,
    SopsSecretSpec,
    VaultSecretSpec,
    is_external,
)

__all__ = [
    "KEY_FILE",
    "ExternalSources",
    "Fetched",
    "SecretSourceError",
    "fetch_all",
    "source_digest",
    "source_key",
]

#: File name of the digest key inside a release state directory.
KEY_FILE = "secret-sources.key"
_KEY_BYTES = 32
_MAX_VALUE = 1_000_000
_MAX_SOPS_OUTPUT = 8_000_000
_MAX_TOKEN = 64 * 1024
#: sops exit code "could not retrieve the data key" (wrong or missing key).
_SOPS_NO_KEY = 128
_SOPS_UNREADABLE = 2
_AWS_AUTH = frozenset(
    {
        "AccessDeniedException",
        "UnrecognizedClientException",
        "InvalidSignatureException",
        "ExpiredTokenException",
        "InvalidClientTokenId",
        "SignatureDoesNotMatch",
        "DecryptionFailure",
    }
)
_AWS_NOT_FOUND = frozenset({"ResourceNotFoundException"})


class SecretSourceError(SecretError):
    """An external source could not be read; ``code`` is a ``secret-source-*`` code."""


def _source_refusal(code: str, name: str, spec: ExternalSpec, reason: str) -> NoReturn:
    raise SecretSourceError(
        code, f"cannot read secret {name!r} from {spec.source()}: {reason}"
    )


@dataclass(frozen=True)
class Fetched:
    """One value read from its source, and its keyed digest (``repr`` hides it)."""

    value: bytes = field(repr=False)
    digest: str


# ------------------------------------------------------------------- digest
def source_key(path: Path, *, create: bool) -> bytes | None:
    """The private digest key; created (0600) when ``create``, else ``None``.

    The file must be an owner-only regular file of exactly 32 bytes.
    """
    if create and not path.exists():
        try:
            descriptor = os.open(
                path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600
            )
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(secrets.token_bytes(_KEY_BYTES))
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise ValueError("the secret source key requires an owner-only file")
        key = stream.read(_KEY_BYTES + 1)
    if len(key) != _KEY_BYTES:
        raise ValueError("the secret source key file is damaged")
    return key


def source_digest(key: bytes, name: str, value: bytes) -> str:
    """``hmac-sha256:<hex>`` of one value; meaningless without ``key``."""
    mac = hmac.new(key, name.encode() + b"\0" + value, hashlib.sha256)
    return "hmac-sha256:" + mac.hexdigest()


# ---------------------------------------------------------------- reading
def _scalar(document: Any, path: tuple[str, ...]) -> bytes | None:
    """The scalar at ``path`` as bytes; ``None`` when absent or not a scalar."""
    current = document
    for part in path:
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return None
    if isinstance(current, str):
        return current.encode()
    if isinstance(current, bool | int | float):
        return json.dumps(current).encode()
    return None


def _read_small_file(path: Path, limit: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise LookupError("not a regular file")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise LookupError("the file is too large")
    return data


def _aws_client(spec: AwsSecretSpec) -> Any:
    """A botocore Secrets Manager client (``piceli[aws]``), built on first use."""
    import botocore.config
    import botocore.session

    session = botocore.session.Session(profile=spec.profile)
    config = botocore.config.Config(
        connect_timeout=spec.timeout_seconds,
        read_timeout=spec.timeout_seconds,
        retries={"max_attempts": 2, "mode": "standard"},
    )
    return session.create_client(
        "secretsmanager",
        region_name=spec.region,
        endpoint_url=spec.endpoint_url,
        config=config,
    )


@dataclass
class ExternalSources:
    """How external sources are reached; nothing is read until :meth:`read`.

    ``resolve`` maps a spec-relative path (a SOPS file, a token file, a CA
    file) to an absolute one; ``environ`` is where ``token_env``, ``PATH`` and
    ``pass_env`` are looked up; ``aws_client(spec)`` builds a Secrets Manager
    client (tests pass a stubbed one).
    """

    resolve: Callable[[Path], Path] = lambda path: path
    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)
    aws_client: Callable[[AwsSecretSpec], Any] | None = None

    def read(self, name: str, spec: ExternalSpec) -> bytes:
        if isinstance(spec, SopsSecretSpec):
            value = self._sops(name, spec)
        elif isinstance(spec, VaultSecretSpec):
            value = self._vault(name, spec)
        else:
            value = self._aws(name, spec)
        if not value:
            _source_refusal("secret-source-not-found", name, spec, "the value is empty")
        if len(value) > _MAX_VALUE:
            _source_refusal(
                "secret-source-failed", name, spec, "the value exceeds 1 MB"
            )
        return value

    # ------------------------------------------------------------- sops
    def _sops_tool(self, name: str, spec: SopsSecretSpec) -> Path:
        from piceli.artifacts.process import ToolPin

        if spec.sops.is_absolute():
            found: str | None = str(spec.sops)
        else:
            found = shutil.which(str(spec.sops), path=self.environ.get("PATH") or "")
        if not found:
            _source_refusal(
                "secret-source-tool-missing", name, spec, "sops was not found"
            )
        try:
            tool = Path(os.path.realpath(found, strict=True))
            info = tool.stat()
        except OSError:
            _source_refusal(
                "secret-source-tool-missing", name, spec, "sops was not found"
            )
        if not stat.S_ISREG(info.st_mode) or not os.access(tool, os.X_OK):
            _source_refusal(
                "secret-source-tool-missing", name, spec, "sops is not an executable"
            )
        if spec.sops_sha256 is not None:
            try:
                digest = ToolPin._digest(tool)
            except (OSError, ValueError):
                digest = ""
            if digest != spec.sops_sha256:
                _source_refusal(
                    "secret-source-failed",
                    name,
                    spec,
                    "the sops binary does not match sops_sha256",
                )
        return tool

    def _sops(self, name: str, spec: SopsSecretSpec) -> bytes:
        from piceli.artifacts.process import ProcessLimits, _run_process

        tool = self._sops_tool(name, spec)
        path = self.resolve(spec.file)
        if not path.is_file():
            _source_refusal(
                "secret-source-not-found", name, spec, "the file does not exist"
            )
        binary = spec.format == "binary"
        argv = [str(tool), "--decrypt", "--output-type", "binary" if binary else "json"]
        if spec.format is not None:
            argv += ["--input-type", spec.format]
        argv.append(str(path))
        environment = {
            item: self.environ[item]
            for item in ("PATH", "HOME", *spec.pass_env)
            if item in self.environ and "\0" not in self.environ[item]
        }
        try:
            receipt, stdout, _stderr = _run_process(
                argv,
                path.parent,
                ProcessLimits(spec.timeout_seconds, _MAX_SOPS_OUTPUT),
                environment=environment,
            )
        except (OSError, ValueError):
            _source_refusal(
                "secret-source-tool-missing", name, spec, "sops could not start"
            )
        state = receipt["state"]
        if state == "timed-out":
            _source_refusal(
                "secret-source-timeout",
                name,
                spec,
                f"sops did not finish in {spec.timeout_seconds:g}s",
            )
        if state == "output-limit":
            _source_refusal(
                "secret-source-failed", name, spec, "sops output exceeds 8 MB"
            )
        if state != "succeeded":
            code = receipt.get("exit_code")
            if code == _SOPS_NO_KEY:
                _source_refusal(
                    "secret-source-auth-failed",
                    name,
                    spec,
                    "sops could not decrypt the data key (exit code 128); "
                    "check the age/PGP/KMS key available to it",
                )
            if code == _SOPS_UNREADABLE:
                _source_refusal(
                    "secret-source-not-found",
                    name,
                    spec,
                    "sops could not read the file (exit code 2)",
                )
            _source_refusal(
                "secret-source-failed", name, spec, f"sops failed (exit code {code})"
            )
        if binary:
            return stdout
        try:
            document = json.loads(stdout)
        except (UnicodeDecodeError, ValueError):
            _source_refusal(
                "secret-source-failed", name, spec, "sops output is not a JSON document"
            )
        value = _scalar(document, spec.key)
        if value is None:
            _source_refusal(
                "secret-source-not-found",
                name,
                spec,
                "the key is missing or not a scalar value",
            )
        return value

    # ------------------------------------------------------------ vault
    def _vault_token(self, name: str, spec: VaultSecretSpec) -> str:
        if spec.token_env is not None:
            token = self.environ.get(spec.token_env, "")
            if not token.strip():
                _source_refusal(
                    "secret-source-auth-failed",
                    name,
                    spec,
                    f"the token variable {spec.token_env} is not set",
                )
        else:
            assert spec.token_file is not None
            try:
                token = _read_small_file(
                    self.resolve(spec.token_file), _MAX_TOKEN
                ).decode()
            except (OSError, LookupError, UnicodeDecodeError):
                _source_refusal(
                    "secret-source-auth-failed",
                    name,
                    spec,
                    "the token file is missing or unreadable",
                )
        token = token.strip()
        if not token or any(char in token for char in "\r\n\0"):
            _source_refusal(
                "secret-source-auth-failed", name, spec, "the token is not valid"
            )
        return token

    def _vault(self, name: str, spec: VaultSecretSpec) -> bytes:
        import http.client
        import ssl
        from urllib.parse import quote, urlsplit

        token = self._vault_token(name, spec)
        address = urlsplit(spec.address)
        assert address.hostname is not None
        request = f"/v1/{quote(spec.mount)}/data/{quote(spec.path)}"
        if spec.version is not None:
            request += f"?version={spec.version}"
        headers = {"X-Vault-Token": token, "Accept": "application/json"}
        if spec.namespace:
            headers["X-Vault-Namespace"] = spec.namespace
        connection: http.client.HTTPConnection
        try:
            if address.scheme == "https":
                context = ssl.create_default_context(
                    cafile=str(self.resolve(spec.ca_file)) if spec.ca_file else None
                )
                connection = http.client.HTTPSConnection(
                    address.hostname,
                    address.port,
                    timeout=spec.timeout_seconds,
                    context=context,
                )
            else:
                connection = http.client.HTTPConnection(
                    address.hostname, address.port, timeout=spec.timeout_seconds
                )
        except (OSError, ssl.SSLError):
            _source_refusal(
                "secret-source-failed", name, spec, "the ca_file is not valid"
            )
        try:
            connection.request("GET", request, headers=headers)
            response = connection.getresponse()
            status = response.status
            body = response.read(_MAX_VALUE * 2 + 1)
        except TimeoutError:
            _source_refusal(
                "secret-source-timeout",
                name,
                spec,
                f"Vault did not answer in {spec.timeout_seconds:g}s",
            )
        except ssl.SSLCertVerificationError:
            _source_refusal(
                "secret-source-failed",
                name,
                spec,
                "tls-verify-failed (the server certificate is not trusted; "
                "set ca_file)",
            )
        except (OSError, http.client.HTTPException) as error:
            _source_refusal("secret-source-failed", name, spec, type(error).__name__)
        finally:
            connection.close()
        if status in (401, 403):
            _source_refusal(
                "secret-source-auth-failed",
                name,
                spec,
                f"Vault refused the token (HTTP {status})",
            )
        if status == 404:
            _source_refusal(
                "secret-source-not-found", name, spec, "the path does not exist"
            )
        if status != 200:
            _source_refusal(
                "secret-source-failed", name, spec, f"Vault answered HTTP {status}"
            )
        if len(body) > _MAX_VALUE * 2:
            _source_refusal(
                "secret-source-failed", name, spec, "the response is too large"
            )
        try:
            document = json.loads(body)
            data = document["data"]["data"]
        except (UnicodeDecodeError, ValueError, KeyError, TypeError):
            _source_refusal(
                "secret-source-failed",
                name,
                spec,
                "the response is not a KV version 2 secret",
            )
        value = _scalar(data, (spec.key,))
        if value is None:
            _source_refusal(
                "secret-source-not-found",
                name,
                spec,
                "the key is missing or not a scalar value",
            )
        return value

    # -------------------------------------------------------------- aws
    def _aws(self, name: str, spec: AwsSecretSpec) -> bytes:
        try:
            import botocore.exceptions as boto_errors
        except ImportError:
            _source_refusal(
                "secret-source-tool-missing",
                name,
                spec,
                "botocore is not installed; install piceli[aws]",
            )
        factory = self.aws_client or _aws_client
        request: dict[str, Any] = {"SecretId": spec.secret_id}
        if spec.version_stage is not None:
            request["VersionStage"] = spec.version_stage
        if spec.version_id is not None:
            request["VersionId"] = spec.version_id
        try:
            response = factory(spec).get_secret_value(**request)
        except boto_errors.ClientError as error:
            code = str(error.response.get("Error", {}).get("Code", "unknown"))
            if code in _AWS_AUTH:
                _source_refusal(
                    "secret-source-auth-failed",
                    name,
                    spec,
                    f"AWS refused the request ({code})",
                )
            if code in _AWS_NOT_FOUND:
                _source_refusal("secret-source-not-found", name, spec, code)
            _source_refusal("secret-source-failed", name, spec, f"AWS error {code}")
        except (
            boto_errors.NoCredentialsError,
            boto_errors.PartialCredentialsError,
            boto_errors.ProfileNotFound,
            boto_errors.CredentialRetrievalError,
        ) as error:
            _source_refusal(
                "secret-source-auth-failed",
                name,
                spec,
                f"no usable AWS credentials ({type(error).__name__})",
            )
        except (boto_errors.ConnectTimeoutError, boto_errors.ReadTimeoutError):
            _source_refusal(
                "secret-source-timeout",
                name,
                spec,
                f"AWS did not answer in {spec.timeout_seconds:g}s",
            )
        except boto_errors.BotoCoreError as error:
            _source_refusal("secret-source-failed", name, spec, type(error).__name__)
        if "SecretString" in response and response["SecretString"] is not None:
            text = str(response["SecretString"])
            if spec.key is None:
                return text.encode()
            try:
                document = json.loads(text)
            except ValueError:
                document = None
            value = _scalar(document, (spec.key,))
            if value is None:
                _source_refusal(
                    "secret-source-not-found",
                    name,
                    spec,
                    "the SecretString has no such JSON key (or is not a JSON object)",
                )
            return value
        binary = response.get("SecretBinary")
        if not isinstance(binary, bytes | bytearray):
            _source_refusal(
                "secret-source-not-found", name, spec, "the secret has no value"
            )
        if spec.key is not None:
            _source_refusal(
                "secret-source-not-found",
                name,
                spec,
                "a binary secret has no JSON keys; drop key",
            )
        return bytes(binary)


def fetch_all(
    secrets_spec: Mapping[str, GeneratorSpec],
    key: bytes,
    sources: ExternalSources | None = None,
) -> dict[str, Fetched]:
    """Read every external source (sorted by name) and digest each value."""
    sources = sources or ExternalSources()
    result: dict[str, Fetched] = {}
    for name in sorted(secrets_spec):
        spec = secrets_spec[name]
        if is_external(spec):
            value = sources.read(name, spec)  # type: ignore[arg-type]
            result[name] = Fetched(value, source_digest(key, name, value))
    return result
