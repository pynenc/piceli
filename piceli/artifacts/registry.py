"""OCI Distribution v2 client for owner-operated registries.

The client speaks the parts of the distribution API that an image push needs:
the ``/v2/`` version check, ``HEAD`` on blobs and manifests, blob uploads
(monolithic ``PUT`` for small blobs, chunked ``PATCH`` for large ones, with a
monolithic fallback for registries that refuse chunks) and manifest
``PUT``/``GET``/``HEAD``. Bodies are streamed from a reader and hashed while
they are sent; nothing is spooled to disk.

Transport rules:

- Plain HTTP is accepted only for a loopback host (``127.0.0.0/8``, ``::1``,
  ``localhost``), such as a registry bound to node loopback or reached through
  a local port-forward. Every other endpoint uses TLS with certificate
  verification (optionally against an explicit CA file).
- An upload ``Location`` on another scheme, host or port is refused, so
  credentials never follow a redirect to a different origin.
- Credentials come from :class:`RegistryCredentials` (a private file) and are
  never part of ``repr``, errors or receipts.

Retries (0.78.0): a transient failure (a refused, reset or aborted
connection, a timeout, a name not resolved, a TLS connection cut short, or an
HTTP 429/502/503/504) is retried with a bounded exponential backoff and
jitter (:class:`RetryPolicy`), honouring ``Retry-After`` within bounds and
never past the client's ``deadline``. Only a repeatable request is sent
again (``GET``, ``HEAD``, ``DELETE``, a ``PUT`` with a body in memory, the
``POST`` that opens an upload session). A failed chunk ``PATCH`` is never
resent blindly: the upload session is asked for its ``Range`` and the upload
resumes from the next byte, or the blob starts again in a new session. Each
retry is listed in :attr:`StreamedOciRegistryClient.retries`; a failure
that remains carries its cause in :attr:`RegistryError.detail` (side, host,
method, path without query, digest, offset, exception and errno, status),
never a credential.

Importing this module opens no socket and reads no file.
"""

from __future__ import annotations

import base64
import hashlib
import io
import ipaddress
import json
import os
import random
import re
import stat
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Protocol, TypeVar

if TYPE_CHECKING:  # networking modules load only when a client connects
    import http.client
    import ssl

from piceli.artifacts.delivery_inputs import DeliveryInputError
from piceli.artifacts.plan import validate_digest

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
REPOSITORY = re.compile(rf"{_COMPONENT}(?:/{_COMPONENT})*")
TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
_HOSTNAME = re.compile(
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
)
MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    )
)
_MAX_MANIFEST = 4 * 1024 * 1024
_MAX_RESPONSE = 1024 * 1024
_CHUNK = 16 * 1024 * 1024
_MONOLITHIC_MAX = 32 * 1024 * 1024


class _Answer(Protocol):
    @property
    def status(self) -> int: ...
    @property
    def headers(self) -> Mapping[str, str]: ...


_R = TypeVar("_R", bound=_Answer)


def is_loopback(host: str) -> bool:
    """Whether ``host`` names this machine's loopback interface."""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


_CLUSTER_SERVICE = re.compile(
    r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?\.[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?"
    r"\.svc(?:\.cluster\.local)?"
)


def is_cluster_service(host: str) -> bool:
    """Whether ``host`` is a Kubernetes Service name (``name.namespace.svc``).

    Such a name resolves only through cluster DNS (or a node's containerd
    mirror for it), never on the public Internet: the in-cluster registry
    (``Registry.in_cluster``) is reached by it over plain HTTP inside the
    cluster network.
    """
    return bool(_CLUSTER_SERVICE.fullmatch(host.lower()))


def plain_http_allowed(host: str) -> bool:
    """Plain HTTP is allowed to a loopback registry or an in-cluster Service name."""
    return is_loopback(host) or is_cluster_service(host)


def validate_host(host: str) -> str:
    """A DNS name or an IP address; nothing else can reach a URL."""
    if not isinstance(host, str) or not 0 < len(host) <= 253:
        raise ValueError("invalid registry host")
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    if not _HOSTNAME.fullmatch(host):
        raise ValueError("invalid registry host")
    return host


def host_port(host: str, port: int | None) -> str:
    """``host[:port]`` with IPv6 literals in brackets."""
    shown = f"[{host}]" if ":" in host else host
    return shown if port is None else f"{shown}:{port}"


#: HTTP statuses that mean "try again later" (rate limit, gateway, unavailable).
RETRY_STATUSES = frozenset({429, 502, 503, 504})
_DETAIL_KEYS = (
    "side",
    "host",
    "method",
    "path",
    "digest",
    "offset",
    "exception",
    "errno",
    "status",
    "cause",
)


class RegistryError(RuntimeError):
    """A registry call failed. ``reason`` is a fixed, secret-free phrase.

    ``detail`` says where and why (0.78.0): ``side`` (``source`` or
    ``target``), ``host``, ``method``, ``path`` (never a query string),
    ``digest`` and ``offset`` of a blob, ``exception`` and ``errno``,
    ``status``. ``transient`` is whether trying again may succeed; unless
    given, it is judged from the exception that caused the error.
    """

    def __init__(
        self,
        reason: str,
        status: int | None = None,
        *,
        detail: Mapping[str, object] | None = None,
        transient: bool | None = None,
    ) -> None:
        super().__init__(reason if status is None else f"{reason} ({status})")
        self.reason = reason
        self.status = status
        self.detail: dict[str, object] = dict(detail or {})
        self._transient = transient

    @property
    def transient(self) -> bool:
        if self._transient is not None:
            return self._transient
        return is_transient(self.__cause__)

    def public(self) -> dict[str, object]:
        """The detail in a fixed order, ``None`` values left out."""
        values = dict(self.detail)
        cause = self.__cause__
        if cause is not None:
            values.setdefault("exception", type(cause).__name__)
            values.setdefault("errno", _errno(cause))
        if self.status is not None:
            values.setdefault("status", self.status)
        return {key: values[key] for key in _DETAIL_KEYS if values.get(key) is not None}


def is_transient(error: BaseException | None, _depth: int = 0) -> bool:
    """Whether ``error`` is a network failure that trying again may cure."""
    if error is None or _depth > 4:
        return False
    import errno as errnos
    import http.client
    import socket
    import ssl

    if isinstance(error, RegistryError):
        return error.transient
    if isinstance(error, (ssl.SSLEOFError, ssl.SSLZeroReturnError)):
        return True
    if isinstance(error, ssl.SSLError):  # a certificate or protocol refusal
        return False
    if isinstance(
        error,
        (
            ConnectionError,
            TimeoutError,
            socket.gaierror,
            http.client.IncompleteRead,
            http.client.BadStatusLine,
        ),
    ):
        return True
    if isinstance(error, OSError) and error.errno in {
        errnos.ECONNRESET,
        errnos.ECONNREFUSED,
        errnos.ECONNABORTED,
        errnos.ETIMEDOUT,
        errnos.EPIPE,
        errnos.ENETUNREACH,
        errnos.ENETDOWN,
        errnos.EHOSTUNREACH,
        errnos.EHOSTDOWN,
    }:
        return True
    if type(error).__name__ in {  # urllib3, used by the proxied client
        "ProtocolError",
        "NewConnectionError",
        "ConnectTimeoutError",
        "ReadTimeoutError",
        "NameResolutionError",
    }:
        return True
    return any(
        is_transient(arg, _depth + 1)
        for arg in getattr(error, "args", ())
        if isinstance(arg, BaseException)
    ) or is_transient(error.__cause__, _depth + 1)


def _errno(error: BaseException) -> int | None:
    value = getattr(error, "errno", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _cause(error: BaseException | str) -> str:
    """``ConnectionResetError(54)``, ``TimeoutError`` or ``HTTP 503``."""
    if isinstance(error, str):
        return error
    if isinstance(error, RegistryError):
        found = error.public()
        if "exception" in found:
            number = found.get("errno")
            return f"{found['exception']}" + (f"({number})" if number else "")
        if "cause" in found:
            return str(found["cause"])
        return f"HTTP {error.status}" if error.status else error.reason
    number = _errno(error)
    return type(error).__name__ + (f"({number})" if number else "")


def describe(detail: Mapping[str, object] | None) -> str:
    """One line for a failure's detail, e.g.
    ``target ghcr.io PATCH blob sha256:9072a3f9… at 33554432: ConnectionResetError(54)``.
    """
    if not detail:
        return ""
    words = [str(detail[key]) for key in ("side", "host", "method") if key in detail]
    found = detail.get("digest")
    if isinstance(found, str):
        words.append(f"blob {found[:15]}…")
    elif "path" in detail:
        words.append(str(detail["path"]))
    if detail.get("offset") is not None:
        words.append(f"at {detail['offset']}")
    text = " ".join(words)
    if "exception" in detail:
        number = detail.get("errno")
        why = f"{detail['exception']}" + (f"({number})" if number else "")
    elif "cause" in detail:
        why = str(detail["cause"])
    elif "status" in detail:
        why = f"HTTP {detail['status']}"
    else:
        return text
    return f"{text}: {why}" if text else why


def _shown(path: str) -> str:
    """A request path as recorded: no scheme, host or query (it may hold a token)."""
    return urllib.parse.urlsplit(path).path or "/"


def _retry_after(value: str | None) -> float | None:
    """Seconds of a ``Retry-After`` header (seconds or an HTTP date)."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        import email.utils

        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    return max(0.0, when.timestamp() - time.time())


@dataclass(frozen=True)
class RetryPolicy:
    """How a client retries a transient failure.

    ``attempts`` tries per request (so ``attempts - 1`` retries); the pause
    before retry ``n`` is ``min(cap, base * 2**(n-1))``, half of it fixed and
    half random (jitter). A ``Retry-After`` longer than that is honoured up
    to ``retry_after_max`` seconds. ``blob_attempts`` bounds how often a
    whole blob starts again. Nothing waits past the client's deadline.
    """

    attempts: int = 5
    base: float = 1.0
    cap: float = 20.0
    retry_after_max: float = 60.0
    blob_attempts: int = 3

    def __post_init__(self) -> None:
        if self.attempts < 1 or self.blob_attempts < 1:
            raise ValueError("a retry policy needs at least one attempt")
        if min(self.base, self.cap, self.retry_after_max) < 0:
            raise ValueError("retry pauses cannot be negative")

    def pause(self, attempt: int) -> float:
        delay = min(self.cap, self.base * 2 ** (attempt - 1))
        return delay / 2 + random.uniform(0, delay / 2)


@dataclass(frozen=True)
class RegistryCredentials:
    """Basic (username/password) or bearer (token) credentials for one registry.

    Load them with :meth:`load` from a private JSON file:
    ``{"username": "...", "password": "..."}`` or ``{"token": "..."}``.
    Username/password also answer a bearer-token challenge (the Docker token
    flow). Nothing here is ever printed or written to a receipt.
    """

    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)
    token: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        basic = self.username is not None or self.password is not None
        if basic == (self.token is not None):
            raise ValueError("credentials need username/password or a token")
        for value in (self.username, self.password, self.token):
            if value is not None and (
                not isinstance(value, str)
                or not value
                or len(value) > 16384
                or not value.isprintable()
            ):
                raise ValueError("invalid registry credential")
        if basic and (self.username is None or self.password is None):
            raise ValueError("credentials need username/password or a token")
        if self.username is not None and ":" in self.username:
            raise ValueError("invalid registry credential")

    @property
    def scheme(self) -> str:
        return "bearer" if self.token is not None else "basic"

    def basic_header(self) -> str:
        assert self.username is not None and self.password is not None
        raw = f"{self.username}:{self.password}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    @classmethod
    def load(cls, path: Path) -> RegistryCredentials:
        """Read a regular, private (no group/other access) JSON credentials file."""
        if not isinstance(path, Path) or not path.is_absolute():
            raise DeliveryInputError("invalid-credentials-file")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
                raise DeliveryInputError("invalid-credentials-file")
            if info.st_mode & 0o077:
                raise DeliveryInputError(
                    "credentials-file-not-private",
                    "credentials file must not be group/world readable",
                )
            with os.fdopen(os.dup(fd), "rb") as stream:
                body = stream.read(65537)
        finally:
            os.close(fd)
        try:
            value = json.loads(body)
        except ValueError:
            raise DeliveryInputError("invalid-credentials-file") from None
        if not isinstance(value, dict) or set(value) - {
            "username",
            "password",
            "token",
        }:
            raise DeliveryInputError("invalid-credentials-file")
        return cls(value.get("username"), value.get("password"), value.get("token"))


#: ``docker-credential-<name> get``: ``(helper name, registry) -> {"Username", "Secret"}``.
CredentialHelper = Callable[[str, str], Mapping[str, object]]
_HELPER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def run_credential_helper(name: str, registry: str) -> Mapping[str, object]:
    """Ask ``docker-credential-<name>`` for ``registry`` (stdin in, JSON out).

    The helper's output is parsed, never logged; a failure is one fixed code.
    """
    import shutil
    import subprocess

    tool = shutil.which(f"docker-credential-{name}")
    if tool is None:
        raise DeliveryInputError("credential-helper-failed")
    try:
        result = subprocess.run(
            [tool, "get"],
            input=registry.encode(),
            capture_output=True,
            timeout=60,
            check=False,
        )
        value = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        value = None
    if not isinstance(value, dict):
        raise DeliveryInputError("credential-helper-failed")
    return value


def docker_config_credentials(
    path: Path, registry: str, *, helper: CredentialHelper = run_credential_helper
) -> RegistryCredentials | None:
    """Push credentials for ``registry`` from a Docker ``config.json``.

    Reads ``credHelpers[registry]`` (else ``credsStore``) and asks that
    helper, or decodes ``auths[registry].auth``; ``None`` when the file has
    nothing for the registry (anonymous). Nothing is printed or recorded.
    """
    if not isinstance(path, Path) or not path.is_absolute():
        raise DeliveryInputError("invalid-docker-config")
    try:
        body = path.read_bytes()
        if len(body) > 1_048_576:
            raise ValueError
        config = json.loads(body)
        if not isinstance(config, dict):
            raise ValueError
    except (OSError, ValueError):
        raise DeliveryInputError("invalid-docker-config") from None
    keys = (registry, f"https://{registry}", f"http://{registry}")
    helpers = config.get("credHelpers") or {}
    name = next(
        (helpers[key] for key in keys if isinstance(helpers, dict) and key in helpers),
        None,
    )
    auths = config.get("auths") or {}
    entry = next(
        (auths[key] for key in keys if isinstance(auths, dict) and key in auths),
        None,
    )
    if name is None and not (isinstance(entry, dict) and entry.get("auth")):
        name = config.get("credsStore")
    if name is not None:
        if not isinstance(name, str) or not _HELPER_NAME.fullmatch(name):
            raise DeliveryInputError("invalid-docker-config")
        answer = helper(name, registry)
        username, secret = answer.get("Username"), answer.get("Secret")
        if not isinstance(secret, str) or not secret:
            return None  # the helper has nothing for this registry
        if username == "<token>" or not username:
            return RegistryCredentials(token=secret)
        if not isinstance(username, str):
            raise DeliveryInputError("credential-helper-failed")
        return _credentials(username, secret, "credential-helper-failed")
    if not isinstance(entry, dict) or not entry.get("auth"):
        return None
    try:
        raw = base64.b64decode(str(entry["auth"]), validate=True).decode()
    except (ValueError, UnicodeDecodeError):
        raise DeliveryInputError("invalid-docker-config") from None
    username, sep, password = raw.partition(":")
    if not sep:
        raise DeliveryInputError("invalid-docker-config")
    return _credentials(username, password, "invalid-docker-config")


def _credentials(username: str, password: str, code: str) -> RegistryCredentials:
    try:
        return RegistryCredentials(username, password)
    except ValueError:
        raise DeliveryInputError(code) from None


@dataclass(frozen=True)
class RegistryEndpoint:
    """Connection specification for an owner-operated OCI registry.

    ``use_tls=False`` (plain HTTP) is only accepted for a loopback ``host`` or
    an in-cluster Service name (``name.namespace.svc``).
    """

    host: str
    port: int = 5000
    use_tls: bool = False
    auth_token: str | None = field(default=None, repr=False)
    credentials: RegistryCredentials | None = field(default=None, repr=False)
    ca_file: Path | None = field(default=None, repr=False)
    timeout: float = 60.0

    def __post_init__(self) -> None:
        validate_host(self.host)
        if (
            isinstance(self.port, bool)
            or not isinstance(self.port, int)
            or not 0 < self.port < 65536
        ):
            raise ValueError("invalid registry port")
        if not isinstance(self.use_tls, bool):
            raise ValueError("invalid registry TLS flag")
        if not self.use_tls and not plain_http_allowed(self.host):
            raise DeliveryInputError(
                "plain-http-not-loopback",
                "plain HTTP is only allowed for a loopback registry or an "
                "in-cluster Service name",
            )
        if self.auth_token is not None and self.credentials is not None:
            raise ValueError("give either an auth token or credentials")
        if self.ca_file is not None and (
            not self.use_tls or not self.ca_file.is_absolute()
        ):
            raise ValueError("a CA file needs TLS and an absolute path")
        if not 0 < self.timeout <= 3600:
            raise ValueError("invalid registry timeout")

    @property
    def base_url(self) -> str:
        scheme = "https" if self.use_tls else "http"
        return f"{scheme}://{host_port(self.host, self.port)}"

    @property
    def auth_scheme(self) -> str:
        if self.auth_token is not None:
            return "bearer"
        return self.credentials.scheme if self.credentials is not None else "none"


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]
    body: bytes = field(repr=False)


@dataclass(frozen=True)
class BlobUpload:
    """What one blob upload did."""

    digest: str
    size: int
    mode: str  # "monolithic", "chunked" or "monolithic-fallback"


def _parse_challenge(header: str) -> tuple[str, dict[str, str]]:
    scheme, _, rest = header.strip().partition(" ")
    params = dict(re.findall(r'([A-Za-z]+)="([^"]*)"', rest))
    return scheme.lower(), params


class _Hashing:
    """Reads exactly ``size`` bytes from ``source`` while hashing them."""

    def __init__(
        self, source: BinaryIO, size: int, cancel: threading.Event | None
    ) -> None:
        self.source = source
        self.remaining = size
        self.hash = hashlib.sha256()
        self.cancel = cancel
        self.count = 0

    def read(self, limit: int) -> bytes:
        if self.cancel is not None and self.cancel.is_set():
            raise InterruptedError("registry push cancelled")
        if self.remaining <= 0:
            return b""
        block = self.source.read(min(limit, self.remaining))
        if not block:
            raise RegistryError("blob-source-truncated")
        self.remaining -= len(block)
        self.count += len(block)
        self.hash.update(block)
        return block

    @property
    def digest(self) -> str:
        return "sha256:" + self.hash.hexdigest()


class _BlobStream:
    """A blob ``GET`` response whose read failures name the source request."""

    def __init__(
        self, response: http.client.HTTPResponse, detail: dict[str, object]
    ) -> None:
        self.response = response
        self.detail = detail
        self.count = 0

    def read(self, limit: int = -1) -> bytes:
        import http.client

        try:
            block = self.response.read(limit) if limit >= 0 else self.response.read()
        except (OSError, http.client.HTTPException) as error:
            raise RegistryError(
                "registry-unreachable", detail={**self.detail, "offset": self.count}
            ) from error
        remaining = getattr(self.response, "length", None)
        if not block and limit != 0 and isinstance(remaining, int) and remaining > 0:
            # The connection closed before the promised Content-Length.
            raise RegistryError(
                "registry-unreachable", detail={**self.detail, "offset": self.count}
            ) from http.client.IncompleteRead(b"", remaining)
        self.count += len(block)
        return block

    def __getattr__(self, name: str) -> object:
        return getattr(self.response, name)


@dataclass
class _Hop:
    """One blob ``GET`` hop: its open connection and response."""

    connection: http.client.HTTPConnection
    response: http.client.HTTPResponse

    @property
    def status(self) -> int:
        return self.response.status

    @property
    def headers(self) -> dict[str, str]:
        return {key.lower(): value for key, value in self.response.getheaders()}

    def release(self) -> None:
        try:
            self.response.read(_MAX_RESPONSE)
        except Exception:  # the connection is closed anyway
            pass
        self.connection.close()


class StreamedOciRegistryClient:
    """Stream blobs and manifests to an OCI Distribution v2 registry.

    ``side`` (``source`` or ``target``) names this client in failures and
    retries; ``deadline`` (a ``time.time()`` value) is when retrying stops;
    ``retry`` is the :class:`RetryPolicy`; ``sleep`` is a test seam. Every
    retry is appended to ``retries`` (a list callers may share).
    """

    def __init__(
        self,
        endpoint: RegistryEndpoint,
        *,
        chunk_size: int = _CHUNK,
        monolithic_max: int = _MONOLITHIC_MAX,
        actions: str = "pull,push",
        retry: RetryPolicy | None = None,
        side: str | None = None,
        deadline: float | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if actions not in {"pull", "pull,push", "pull,delete"}:
            raise ValueError(
                "registry scope actions must be 'pull', 'pull,push' or 'pull,delete'"
            )
        self.endpoint = endpoint
        self.actions = actions
        self.chunk_size = chunk_size
        self.monolithic_max = monolithic_max
        self.retry = retry or RetryPolicy()
        self.side = side
        self.deadline = deadline
        self.retries: list[dict[str, object]] = []
        self._sleep = sleep
        self._authorization: str | None = None
        self._scoped: set[str] = set()
        if endpoint.auth_token is not None:
            self._authorization = f"Bearer {endpoint.auth_token}"
        self._context: ssl.SSLContext | None = None
        if endpoint.use_tls:
            import ssl

            self._context = ssl.create_default_context(
                cafile=str(endpoint.ca_file) if endpoint.ca_file else None
            )

    # --- transport -------------------------------------------------------------

    def _connection(
        self, scheme: str, host: str, port: int
    ) -> http.client.HTTPConnection:
        import http.client
        import ssl

        if scheme == "https":
            context = self._context or ssl.create_default_context()
            return http.client.HTTPSConnection(
                host, port, timeout=self.endpoint.timeout, context=context
            )
        if not self._plain_http_ok(host):
            raise RegistryError("plain-http-refused")
        return http.client.HTTPConnection(host, port, timeout=self.endpoint.timeout)

    def _plain_http_ok(self, host: str) -> bool:
        """Loopback, or the in-cluster Service this endpoint names (never another)."""
        return is_loopback(host) or (
            is_cluster_service(host) and host.lower() == self.endpoint.host.lower()
        )

    def _target(self, path: str) -> str:
        """Resolve a path or a registry-supplied URL against the endpoint origin."""
        if path.startswith("/"):
            return path
        parts = urllib.parse.urlsplit(path)
        base = urllib.parse.urlsplit(self.endpoint.base_url)
        if (
            parts.scheme,
            parts.hostname,
            parts.port or _default_port(parts.scheme),
        ) != (
            base.scheme,
            base.hostname,
            self.endpoint.port,
        ):
            raise RegistryError("cross-origin-location-refused")
        return parts.path + ("?" + parts.query if parts.query else "")

    def _send(
        self,
        method: str,
        path: str,
        *,
        body: bytes | Iterator[bytes] | None = None,
        length: int | None = None,
        headers: Mapping[str, str] | None = None,
        authorize: bool = True,
        max_body: int = _MAX_RESPONSE,
    ) -> Response:
        import http.client

        target = self._target(path)
        connection = self._connection(
            "https" if self.endpoint.use_tls else "http",
            self.endpoint.host,
            self.endpoint.port,
        )
        try:
            connection.putrequest(method, target, skip_accept_encoding=True)
            sent = dict(headers or {})
            if authorize and self._authorization is not None:
                sent["Authorization"] = self._authorization
            if isinstance(body, bytes):
                length = len(body)
            if body is not None or method in {"PUT", "PATCH", "POST"}:
                sent.setdefault("Content-Length", str(length or 0))
            for key, value in sent.items():
                connection.putheader(key, value)
            connection.endheaders()
            if isinstance(body, bytes):
                if body:
                    connection.send(body)
            elif body is not None:
                for block in body:
                    connection.send(block)
            response = connection.getresponse()
            data = b"" if method == "HEAD" else response.read(max_body + 1)
            if len(data) > max_body:
                raise RegistryError("registry-response-too-large")
            return Response(
                response.status,
                {key.lower(): value for key, value in response.getheaders()},
                data,
            )
        except (OSError, http.client.HTTPException) as error:
            raise RegistryError("registry-unreachable") from error
        finally:
            connection.close()

    # --- retries -----------------------------------------------------------------

    @property
    def _host(self) -> str:
        default = 443 if self.endpoint.use_tls else 80
        port = None if self.endpoint.port == default else self.endpoint.port
        return host_port(self.endpoint.host, port)

    def retry_pause(
        self,
        attempt: int,
        cause: BaseException | str,
        method: str,
        path: str,
        *,
        limit: int | None = None,
        retry_after: float | None = None,
        **context: object,
    ) -> bool:
        """Record a retry after failed ``attempt`` and wait; ``False`` when the
        attempts are used up or the pause would pass the deadline."""
        if attempt >= (limit or self.retry.attempts):
            return False
        pause = self.retry.pause(attempt)
        if retry_after is not None:
            pause = max(pause, min(retry_after, self.retry.retry_after_max))
        if self.deadline is not None and time.time() + pause >= self.deadline:
            return False
        entry: dict[str, object] = {
            "side": context.pop("side", None) or self.side,
            "host": context.pop("host", None) or self._host,
            "method": method,
            "path": _shown(path),
            **context,
            "attempt": attempt,
            "cause": _cause(cause),
            "pause_seconds": round(pause, 3),
        }
        self.retries.append({k: v for k, v in entry.items() if v is not None})
        self._sleep(pause)
        return True

    def _describe(
        self,
        error: RegistryError,
        method: str,
        path: str,
        host: str | None = None,
        **context: object,
    ) -> RegistryError:
        """Fill in where ``error`` happened (keeps what is already there)."""
        found = error.detail
        if self.side is not None:
            found.setdefault("side", self.side)
        found.setdefault("host", host or self._host)
        found.setdefault("method", method)
        found.setdefault("path", _shown(path))
        for key, value in context.items():
            if value is not None:
                found.setdefault(key, value)
        return error

    def _retrying(
        self,
        method: str,
        path: str,
        once: Callable[[], _R],
        *,
        may_retry: bool,
        host: str | None = None,
    ) -> _R:
        """``once()``, repeated on a transient failure while ``may_retry``."""
        attempt = 0
        while True:
            attempt += 1
            try:
                response = once()
            except RegistryError as error:
                self._describe(error, method, path, host)
                if not (
                    may_retry
                    and error.transient
                    and self.retry_pause(attempt, error, method, path, host=host)
                ):
                    raise
                continue
            if (
                may_retry
                and response.status in RETRY_STATUSES
                and self.retry_pause(
                    attempt,
                    f"HTTP {response.status}",
                    method,
                    path,
                    host=host,
                    retry_after=_retry_after(response.headers.get("retry-after")),
                )
            ):
                release = getattr(response, "release", None)
                if release is not None:
                    release()
                continue
            return response

    def _request(
        self, method: str, path: str, *, retry: bool | None = None, **kwargs: object
    ) -> Response:
        """:meth:`_send`, retried when the request is repeatable: ``GET``,
        ``HEAD``, ``DELETE`` or ``PUT`` with a body in memory, or ``retry``."""
        replayable = not isinstance(kwargs.get("body"), Iterator)
        if retry is None:
            retry = replayable and method in {"GET", "HEAD", "PUT", "DELETE"}
        return self._retrying(
            method,
            path,
            lambda: self._send(method, path, **kwargs),  # type: ignore[arg-type]
            may_retry=retry and replayable,
        )

    def _call(
        self,
        method: str,
        path: str,
        repository: str | None = None,
        **kwargs: object,
    ) -> Response:
        """Send (retrying a repeatable request); on a 401 with a replayable
        body, authenticate and resend."""
        if repository is not None and repository not in self._scoped:
            # Authenticate before a streamed body: it cannot be resent.
            self.authenticate(repository)
        response = self._request(method, path, **kwargs)  # type: ignore[arg-type]
        replayable = not isinstance(kwargs.get("body"), Iterator)
        if response.status == 401 and replayable:
            if self._answer(response, repository):
                response = self._request(method, path, **kwargs)  # type: ignore[arg-type]
        if response.status == 401:
            error = RegistryError("registry-unauthorized", 401)
            raise self._describe(error, method, path)
        if response.status == 403:
            error = RegistryError("registry-forbidden", 403)
            raise self._describe(error, method, path)
        return response

    def _answer(self, response: Response, repository: str | None) -> bool:
        """Set an Authorization header for a 401 challenge; False if impossible."""
        creds = self.endpoint.credentials
        challenge = response.headers.get("www-authenticate", "")
        scheme, params = _parse_challenge(challenge)
        if scheme == "basic" and creds is not None and creds.scheme == "basic":
            header = creds.basic_header()
        elif scheme == "bearer" and "realm" in params:
            if creds is not None and creds.scheme == "bearer":
                header = f"Bearer {creds.token}"
            else:
                header = f"Bearer {self._fetch_token(params, repository, creds)}"
        else:
            return False
        if header == self._authorization:
            return False
        self._authorization = header
        return True

    def _fetch_token(
        self,
        params: Mapping[str, str],
        repository: str | None,
        creds: RegistryCredentials | None,
    ) -> str:
        import http.client

        realm = urllib.parse.urlsplit(params["realm"])
        if realm.scheme not in {"http", "https"} or not realm.hostname:
            raise RegistryError("invalid-token-realm")
        query = [("service", params["service"])] if "service" in params else []
        if repository is not None:
            query.append(("scope", f"repository:{repository}:{self.actions}"))
        path = realm.path or "/"
        encoded = urllib.parse.urlencode(query)
        if realm.query:
            encoded = realm.query + ("&" + encoded if encoded else "")
        headers = {}
        if creds is not None and creds.scheme == "basic":
            headers["Authorization"] = creds.basic_header()
        host, port = realm.hostname, realm.port or _default_port(realm.scheme)

        def once() -> Response:
            connection = self._connection(realm.scheme, host, port)
            try:
                connection.request(
                    "GET", path + ("?" + encoded if encoded else ""), headers=headers
                )
                answer = connection.getresponse()
                return Response(
                    answer.status,
                    {key.lower(): value for key, value in answer.getheaders()},
                    answer.read(_MAX_RESPONSE + 1),
                )
            except (OSError, http.client.HTTPException) as error:
                raise RegistryError("registry-unreachable") from error
            finally:
                connection.close()

        response = self._retrying(
            "GET", path, once, may_retry=True, host=host_port(host, realm.port)
        )
        body = response.body
        if response.status != 200 or len(body) > _MAX_RESPONSE:
            raise RegistryError("registry-unauthorized", response.status)
        try:
            value = json.loads(body)
            token = value.get("token") or value.get("access_token")
        except (ValueError, AttributeError):
            token = None
        if not isinstance(token, str) or not token or not token.isprintable():
            raise RegistryError("registry-unauthorized")
        return token

    def authenticate(self, repository: str) -> None:
        """Probe ``/v2/`` and answer its challenge for ``repository`` (``actions`` scope)."""
        self._scoped.add(repository)
        response = self._request("GET", "/v2/")
        if response.status == 401 and self._answer(response, repository):
            response = self._request("GET", "/v2/")
        if response.status == 401:
            raise RegistryError("registry-unauthorized", 401)
        if response.status != 200:
            raise RegistryError("registry-not-oci-v2", response.status)

    # --- API ---------------------------------------------------------------------

    def check_v2_support(self) -> bool:
        """Whether the endpoint answers the OCI Distribution v2 version check."""
        try:
            return self._send("GET", "/v2/").status in {200, 401}
        except RegistryError:
            return False

    def blob_size(self, repository: str, digest: str) -> int | None:
        """Size of a blob present in ``repository``, or ``None`` when absent."""
        validate_digest(digest)
        response = self._call("HEAD", f"/v2/{repository}/blobs/{digest}", repository)
        if response.status == 404:
            return None
        if response.status in {200, 307}:
            try:
                return int(response.headers.get("content-length", "-1"))
            except ValueError:
                return -1
        raise RegistryError("registry-error", response.status)

    def has_blob(self, repository: str, digest: str) -> bool:
        """Check if a layer or config blob already exists in the repository."""
        return self.blob_size(repository, digest) is not None

    @contextmanager
    def open_blob(
        self, repository: str, digest: str, *, max_redirects: int = 5
    ) -> Iterator[BinaryIO]:
        """Stream a blob's bytes (``GET``), following registry redirects.

        Registries often answer a blob ``GET`` with a redirect to object
        storage on another origin. A redirect to the same origin keeps the
        ``Authorization`` header; one to another origin is followed **without**
        it (credentials never leave the registry's origin) and must use HTTPS
        unless it is a loopback address. The caller hashes what it reads: the
        bytes are not verified here.
        """
        import http.client

        validate_digest(digest)
        if repository not in self._scoped:
            self.authenticate(repository)
        base = urllib.parse.urlsplit(self.endpoint.base_url)
        origin = (base.scheme, base.hostname, self.endpoint.port)
        scheme, host, port = origin
        path = f"/v2/{repository}/blobs/{digest}"
        answered = False
        for _ in range(max_redirects + 1):
            assert host is not None
            same = (scheme, host, port) == origin
            shown = self._host if same else host_port(host, port)

            def once(
                scheme: str = scheme, host: str = host, port: int = port,
                path: str = path, same: bool = same,
            ) -> _Hop:  # fmt: skip
                if same or scheme != "https":
                    connection = self._connection(scheme, host, port)
                else:
                    import ssl

                    connection = http.client.HTTPSConnection(
                        host,
                        port,
                        timeout=self.endpoint.timeout,
                        context=ssl.create_default_context(),
                    )
                try:
                    headers = {}
                    if same and self._authorization is not None:
                        headers["Authorization"] = self._authorization
                    connection.request("GET", path, headers=headers)
                    return _Hop(connection, connection.getresponse())
                except (OSError, http.client.HTTPException) as error:
                    connection.close()
                    raise RegistryError("registry-unreachable") from error

            try:
                hop = self._retrying("GET", path, once, may_retry=True, host=shown)
            except RegistryError as error:
                self._describe(error, "GET", path, shown, digest=digest)
                raise
            connection, response = hop.connection, hop.response
            status = response.status
            if status in {301, 302, 303, 307, 308}:
                location = response.getheader("location") or ""
                hop.release()
                current = f"{scheme}://{host_port(host, port)}{path}"
                parts = urllib.parse.urlsplit(urllib.parse.urljoin(current, location))
                if parts.scheme not in {"http", "https"} or not parts.hostname:
                    raise RegistryError("invalid-blob-redirect")
                if parts.scheme == "http" and not self._plain_http_ok(parts.hostname):
                    raise RegistryError("plain-http-refused")
                scheme, host = parts.scheme, parts.hostname
                port = parts.port or _default_port(parts.scheme)
                path = (parts.path or "/") + ("?" + parts.query if parts.query else "")
                continue
            if status == 401 and same and not answered:
                challenge = Response(status, hop.headers, b"")
                hop.release()
                answered = True
                if self._answer(challenge, repository):
                    continue
                raise RegistryError("registry-unauthorized", 401)
            if status != 200:
                hop.release()
                if status == 401:
                    refused = RegistryError("registry-unauthorized", 401)
                elif status == 403:
                    refused = RegistryError("registry-forbidden", 403)
                elif status == 404:
                    refused = RegistryError("blob-not-found", 404)
                else:
                    refused = RegistryError("registry-error", status)
                raise self._describe(refused, "GET", path, shown, digest=digest)
            detail: dict[str, object] = {"side": self.side, "host": shown}
            detail |= {"method": "GET", "path": _shown(path), "digest": digest}
            try:
                yield _BlobStream(  # type: ignore[misc]
                    response, {k: v for k, v in detail.items() if v is not None}
                )
            finally:
                connection.close()
            return
        raise RegistryError("too-many-redirects")

    def _start_upload(self, repository: str) -> str:
        # A repeated POST at worst leaves an empty session the registry expires.
        response = self._call(
            "POST", f"/v2/{repository}/blobs/uploads/", repository, retry=True
        )
        location = response.headers.get("location")
        if response.status != 202 or not location:
            raise RegistryError("upload-rejected", response.status)
        return self._target(location)

    @staticmethod
    def _with_digest(location: str, digest: str) -> str:
        separator = "&" if "?" in location else "?"
        return f"{location}{separator}digest={urllib.parse.quote(digest, safe=':')}"

    def _cancel_upload(self, location: str) -> None:
        try:
            self._send("DELETE", location)
        except RegistryError:
            pass

    def push_blob(
        self,
        repository: str,
        digest: str,
        size: int,
        source: BinaryIO,
        *,
        cancel: threading.Event | None = None,
    ) -> BlobUpload:
        """Upload exactly ``size`` bytes of ``source`` as blob ``digest``.

        The bytes are hashed while they are sent. A chunked upload is only
        committed (the final ``PUT ?digest=``) after the hash matched; a
        monolithic upload carries the digest and the registry verifies it.

        A failed chunk resumes from the session's ``Range``; when the session
        is gone, a seekable ``source`` starts the blob again here, and any
        other raises a transient :class:`RegistryError` for the caller to
        start again with a fresh source.
        """
        validate_digest(digest)
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError("invalid blob size")
        try:
            start = source.tell() if source.seekable() else None
        except (AttributeError, OSError, ValueError):
            start = None
        attempt = 0
        while True:
            reader = _Hashing(source, size, cancel)
            try:
                return self._push_blob(repository, digest, size, reader)
            except RegistryError as error:
                error.detail.setdefault("digest", digest)
                attempt += 1
                if (
                    start is None
                    or not error.transient
                    or not self.retry_pause(
                        attempt,
                        error,
                        "POST",
                        f"/v2/{repository}/blobs/uploads/",
                        limit=self.retry.blob_attempts,
                        digest=digest,
                        action="restart",
                    )
                ):
                    raise
                source.seek(start)

    def _push_blob(
        self, repository: str, digest: str, size: int, reader: _Hashing
    ) -> BlobUpload:
        location = self._start_upload(repository)
        if size <= self.monolithic_max:
            # Small enough to hold: the PUT can then be repeated.
            data = bytearray()
            while len(data) < size:
                data += reader.read(min(1024 * 1024, size - len(data)))
            self._commit(repository, location, digest, bytes(data))
            return self._verified(reader, digest, size, "monolithic")
        offset = 0  # bytes the registry acknowledged
        pending = b""  # bytes read but not acknowledged; they start at offset
        first, failures = True, 0
        while offset < size:
            if not pending:
                pending = reader.read(self.chunk_size)
            end = offset + len(pending)
            failure: RegistryError
            try:
                response = self._call(
                    "PATCH",
                    location,
                    repository,
                    body=pending,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Content-Range": f"{offset}-{end - 1}",
                    },
                )
            except RegistryError as error:
                self._describe(error, "PATCH", location, digest=digest, offset=offset)
                if not error.transient:
                    raise
                failure, retry_after = error, None
            else:
                if response.status in {404, 405, 416, 501} and first:
                    # This registry does not take chunks: restart as one PUT.
                    self._cancel_upload(location)
                    location = self._start_upload(repository)
                    self._monolithic(
                        repository, location, digest, size, reader, pending
                    )
                    return self._verified(reader, digest, size, "monolithic-fallback")
                new_location = response.headers.get("location")
                if response.status == 202 and new_location:
                    location = self._target(new_location)
                    offset, pending, first = end, b"", False
                    continue
                failure = self._describe(
                    RegistryError("upload-rejected", response.status),
                    "PATCH",
                    location,
                    digest=digest,
                    offset=offset,
                )
                if response.status not in RETRY_STATUSES:
                    self._cancel_upload(location)
                    raise failure
                retry_after = _retry_after(response.headers.get("retry-after"))
            failures += 1
            first = False
            if not self.retry_pause(
                failures,
                failure,
                "PATCH",
                location,
                retry_after=retry_after,
                digest=digest,
                offset=offset,
                action="resume",
            ):
                raise failure
            state = self._upload_state(repository, location, offset)
            if state is not None and offset <= state[1] <= end:
                location, acknowledged = state
                pending = pending[acknowledged - offset :]
                offset = acknowledged
                continue
            gone = "gone" if state is None else f"at {state[1]}, not {offset}-{end}"
            if offset == 0:  # every byte read so far is still held: new session
                location = self._start_upload(repository)
                continue
            raise self._describe(
                RegistryError(
                    "registry-unreachable",
                    detail={"cause": f"upload session {gone}"},
                    transient=True,
                ),
                "PATCH",
                location,
                digest=digest,
                offset=offset,
            )
        if reader.digest != digest:
            self._cancel_upload(location)
            raise RegistryError("blob-digest-mismatch")
        self._commit(repository, location, digest, b"")
        return self._verified(reader, digest, size, "chunked")

    def _upload_state(
        self, repository: str, location: str, offset: int
    ) -> tuple[str, int] | None:
        """``(location, bytes received)`` of an upload session (``GET`` on it,
        answered with ``Range: 0-<last byte>``); ``None`` when it is gone or
        says nothing usable."""
        response = self._call("GET", location, repository)
        if response.status not in {200, 204}:
            return None
        found = re.fullmatch(
            r"(?:bytes=)?0-(\d+)", response.headers.get("range", "").strip()
        )
        if found is None:
            return None
        last = int(found.group(1))
        # "0-0" is how registries say "nothing yet" (and, rarely, one byte).
        received = 0 if last == 0 and offset == 0 else last + 1
        new_location = response.headers.get("location")
        return (self._target(new_location) if new_location else location), received

    def _commit(self, repository: str, location: str, digest: str, data: bytes) -> None:
        """The final ``PUT ?digest=`` (with the whole blob, or the rest).

        It is repeated on a transient failure; when a repeat finds the
        session closed, the blob being there means the first one landed.
        """
        response = self._call(
            "PUT",
            self._with_digest(location, digest),
            repository,
            body=data,
            headers={"Content-Type": "application/octet-stream"},
        )
        if response.status in {201, 204}:
            return
        if response.status == 404 and self.blob_size(repository, digest) is not None:
            return
        raise self._describe(
            RegistryError("upload-rejected", response.status),
            "PUT",
            location,
            digest=digest,
        )

    def _monolithic(
        self,
        repository: str,
        location: str,
        digest: str,
        size: int,
        reader: _Hashing,
        head: bytes,
    ) -> None:
        """One streamed ``PUT`` (a registry that refuses chunks; not repeatable)."""

        def body() -> Iterator[bytes]:
            if head:
                yield head
            while block := reader.read(1024 * 1024):
                yield block

        response = self._call(
            "PUT",
            self._with_digest(location, digest),
            repository,
            body=body(),
            length=size,
            headers={"Content-Type": "application/octet-stream"},
        )
        if response.status not in {201, 204}:
            raise RegistryError("upload-rejected", response.status)

    @staticmethod
    def _verified(reader: _Hashing, digest: str, size: int, mode: str) -> BlobUpload:
        if reader.count != size or reader.digest != digest:
            raise RegistryError("blob-digest-mismatch")
        return BlobUpload(digest, size, mode)

    def push_blob_stream(
        self, repository: str, stream: BinaryIO, expected_digest: str
    ) -> str:
        """Upload a small in-memory stream as one blob (compatibility helper)."""
        validate_digest(expected_digest)
        data = stream.read()
        self.push_blob(repository, expected_digest, len(data), io.BytesIO(data))
        return expected_digest

    def manifest_digest(self, repository: str, reference: str) -> str | None:
        """``HEAD`` a manifest by tag or digest; its digest, or ``None`` if absent."""
        response = self._call(
            "HEAD",
            f"/v2/{repository}/manifests/{reference}",
            repository,
            headers={"Accept": MANIFEST_ACCEPT},
        )
        if response.status == 404:
            return None
        if response.status != 200:
            raise RegistryError("registry-error", response.status)
        value = response.headers.get("docker-content-digest", "")
        if not _DIGEST_RE.fullmatch(value):
            if _DIGEST_RE.fullmatch(reference):
                return reference
            # Without the header a tag cannot be resolved cheaply: fetch it.
            body, _ = self.get_manifest(repository, reference)
            return "sha256:" + hashlib.sha256(body).hexdigest()
        return value

    def get_manifest(self, repository: str, reference: str) -> tuple[bytes, str]:
        """Fetch a manifest's exact bytes and media type."""
        response = self._call(
            "GET",
            f"/v2/{repository}/manifests/{reference}",
            repository,
            headers={"Accept": MANIFEST_ACCEPT},
            max_body=_MAX_MANIFEST,
        )
        if response.status == 404:
            raise RegistryError("manifest-not-found", 404)
        if response.status != 200:
            raise RegistryError("registry-error", response.status)
        return response.body, response.headers.get("content-type", "")

    def push_manifest(
        self, repository: str, reference: str, manifest_bytes: bytes, media_type: str
    ) -> str:
        """``PUT`` a manifest under a tag or its digest; returns its digest."""
        return self.put_manifest(repository, reference, manifest_bytes, media_type)[0]

    def put_manifest(
        self, repository: str, reference: str, manifest_bytes: bytes, media_type: str
    ) -> tuple[str, bool]:
        """``PUT`` a manifest; ``(digest, subject acknowledged)``.

        The second value is whether the registry answered ``OCI-Subject``: it
        indexed the manifest's ``subject`` for the referrers API (OCI
        Distribution 1.1), so no fallback tag is needed.
        """
        digest = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
        response = self._call(
            "PUT",
            f"/v2/{repository}/manifests/{reference}",
            repository,
            body=manifest_bytes,
            headers={"Content-Type": media_type},
        )
        if response.status not in {200, 201}:
            raise RegistryError("manifest-rejected", response.status)
        reported = response.headers.get("docker-content-digest")
        if reported and reported != digest:
            raise RegistryError("registry-digest-mismatch")
        return digest, bool(response.headers.get("oci-subject"))

    def list_repositories(self) -> list[str] | None:
        """Repository names (``/v2/_catalog``, paginated); ``None`` if not served.

        Hosted registries often hide or forbid the catalog: the caller then
        needs another source of repository names.
        """
        names: list[str] = []
        path: str | None = "/v2/_catalog?n=1000"
        for _ in range(1000):
            if path is None:
                return names
            try:
                response = self._call("GET", path, max_body=_MAX_MANIFEST)
            except RegistryError as error:
                if error.status in {401, 403}:
                    return None
                raise
            if response.status in {404, 405, 501}:
                return None
            if response.status != 200:
                raise RegistryError("registry-error", response.status)
            names.extend(_names(response.body, "repositories"))
            path = _next_link(response.headers.get("link"))
        raise RegistryError("registry-response-too-large")

    def list_tags(self, repository: str) -> list[str]:
        """Every tag of ``repository`` (paginated); ``[]`` for an unknown one."""
        tags: list[str] = []
        path: str | None = f"/v2/{repository}/tags/list?n=1000"
        for _ in range(1000):
            if path is None:
                return tags
            response = self._call("GET", path, repository, max_body=_MAX_MANIFEST)
            if response.status == 404:
                return tags
            if response.status != 200:
                raise RegistryError("registry-error", response.status)
            tags.extend(_names(response.body, "tags"))
            path = _next_link(response.headers.get("link"))
        raise RegistryError("registry-response-too-large")

    def delete_manifest(self, repository: str, digest: str) -> bool:
        """``DELETE`` a manifest by digest; ``False`` when it was already gone.

        A registry that does not allow deletes answers 405 (or 400 ``UNSUPPORTED``):
        ``registry-delete-disabled``.
        """
        validate_digest(digest)
        response = self._call(
            "DELETE", f"/v2/{repository}/manifests/{digest}", repository
        )
        if response.status in {200, 202, 204}:
            return True
        if response.status == 404:
            return False
        if response.status in {405, 501} or (
            response.status == 400 and b"UNSUPPORTED" in response.body
        ):
            raise RegistryError("registry-delete-disabled", response.status)
        raise RegistryError("registry-error", response.status)

    def referrers(self, repository: str, digest: str) -> list[dict[str, object]] | None:
        """The referrers of ``digest`` (OCI 1.1 API); ``None`` when unsupported."""
        validate_digest(digest)
        response = self._call(
            "GET",
            f"/v2/{repository}/referrers/{digest}",
            repository,
            headers={"Accept": "application/vnd.oci.image.index.v1+json"},
            max_body=_MAX_MANIFEST,
        )
        content = response.headers.get("content-type", "")
        if response.status != 200 or "application/vnd.oci.image.index" not in content:
            return None
        try:
            value = json.loads(response.body)
        except ValueError:
            raise RegistryError("registry-error") from None
        manifests = value.get("manifests") if isinstance(value, dict) else None
        if not isinstance(manifests, list):
            raise RegistryError("registry-error")
        return [item for item in manifests if isinstance(item, dict)]


def _names(body: bytes, key: str) -> list[str]:
    """The string list ``key`` of a registry listing (``null`` counts as empty)."""
    try:
        value = json.loads(body)
    except ValueError:
        raise RegistryError("registry-error") from None
    items = value.get(key) if isinstance(value, dict) else None
    if items is None:
        return []
    if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
        raise RegistryError("registry-error")
    return items


def _next_link(header: str | None) -> str | None:
    """The ``rel="next"`` target of a pagination ``Link`` header, as a path."""
    if not header:
        return None
    for part in header.split(","):
        match = re.fullmatch(r'\s*<([^>]+)>\s*;\s*rel="?next"?\s*', part)
        if match:
            return match.group(1)
    return None


def _default_port(scheme: str) -> int:
    return 443 if scheme == "https" else 80


_TRUE = {"1", "true", "yes"}
_FALSE = {"0", "false", "no"}


@dataclass(frozen=True)
class RegistryTarget:
    """A parsed ``oci://host[:port]/repository[:tag]`` delivery target.

    ``tls`` defaults to ``False`` for a loopback host or an in-cluster
    Service name (``name.namespace.svc``) and ``True`` otherwise; ``?tls=false``
    is refused for any other host. Digest references are
    not targets: the pushed manifest's digest is the result of a delivery.
    """

    host: str
    port: int | None
    repository: str
    tag: str | None = None
    tls: bool = True

    def __post_init__(self) -> None:
        validate_host(self.host)
        if self.port is not None and (
            isinstance(self.port, bool)
            or not isinstance(self.port, int)
            or not 0 < self.port < 65536
        ):
            raise ValueError("invalid registry port")
        if (
            not isinstance(self.repository, str)
            or len(self.repository) > 255
            or not REPOSITORY.fullmatch(self.repository)
        ):
            raise ValueError("invalid registry repository")
        if self.tag is not None and (
            not isinstance(self.tag, str) or not TAG.fullmatch(self.tag)
        ):
            raise ValueError("invalid registry tag")
        if not isinstance(self.tls, bool):
            raise ValueError("invalid registry TLS flag")
        if not self.tls and not plain_http_allowed(self.host):
            raise DeliveryInputError(
                "plain-http-not-loopback",
                "plain HTTP is only allowed for a loopback registry or an "
                "in-cluster Service name",
            )

    @classmethod
    def parse(cls, url: str) -> RegistryTarget:
        if not isinstance(url, str) or len(url) > 1024 or not url.isprintable():
            raise ValueError("invalid registry target")
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "oci" or parts.fragment or not parts.hostname:
            raise ValueError("registry target must be oci://host[:port]/repository")
        if parts.username is not None or parts.password is not None:
            raise ValueError("credentials cannot be part of a registry target")
        pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        query = dict(pairs)
        if len(query) != len(pairs) or set(query) - {"tls"}:
            raise ValueError("unknown or repeated registry target option")
        try:
            port = parts.port
        except ValueError as error:
            raise ValueError("invalid registry port") from error
        path = parts.path.lstrip("/")
        if not path or "@" in path or path.endswith("/"):
            raise ValueError("registry target needs a repository (and no digest)")
        repository, tag = path, None
        if ":" in path.rsplit("/", 1)[-1]:
            repository, tag = path.rsplit(":", 1)
        host = parts.hostname
        tls_text = query.get("tls")
        if tls_text is None:
            tls = not plain_http_allowed(host)
        elif tls_text in _TRUE | _FALSE:
            tls = tls_text in _TRUE
        else:
            raise ValueError("invalid registry TLS flag")
        return cls(host, port, repository, tag, tls)

    @property
    def effective_port(self) -> int:
        return self.port if self.port is not None else (443 if self.tls else 80)

    @property
    def registry(self) -> str:
        """``host[:port]`` exactly as the push endpoint is addressed."""
        return host_port(self.host, self.port)

    @property
    def identity(self) -> str:
        """Canonical target string a grant binds to."""
        tag = f":{self.tag}" if self.tag else ""
        tls = "true" if self.tls else "false"
        return f"oci://{self.registry}/{self.repository}{tag}?tls={tls}"

    def endpoint(
        self,
        *,
        credentials: RegistryCredentials | None = None,
        ca_file: Path | None = None,
        timeout: float = 60.0,
    ) -> RegistryEndpoint:
        return RegistryEndpoint(
            self.host,
            self.effective_port,
            self.tls,
            credentials=credentials,
            ca_file=ca_file,
            timeout=timeout,
        )

    def public(self) -> dict[str, object]:
        return {
            "registry": self.registry,
            "repository": self.repository,
            "tag": self.tag,
            "tls": self.tls,
        }
