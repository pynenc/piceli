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

Importing this module opens no socket and reads no file.
"""

from __future__ import annotations

import base64
import hashlib
import io
import ipaddress
import json
import os
import re
import stat
import threading
import urllib.parse
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

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


def is_loopback(host: str) -> bool:
    """Whether ``host`` names this machine's loopback interface."""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


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


class RegistryError(RuntimeError):
    """A registry call failed. ``reason`` is a fixed, secret-free phrase."""

    def __init__(self, reason: str, status: int | None = None) -> None:
        super().__init__(reason if status is None else f"{reason} ({status})")
        self.reason = reason
        self.status = status


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


@dataclass(frozen=True)
class RegistryEndpoint:
    """Connection specification for an owner-operated OCI registry.

    ``use_tls=False`` (plain HTTP) is only accepted for a loopback ``host``.
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
        if not self.use_tls and not is_loopback(self.host):
            raise DeliveryInputError(
                "plain-http-not-loopback",
                "plain HTTP is only allowed for a loopback registry",
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


class StreamedOciRegistryClient:
    """Stream blobs and manifests to an OCI Distribution v2 registry."""

    def __init__(
        self,
        endpoint: RegistryEndpoint,
        *,
        chunk_size: int = _CHUNK,
        monolithic_max: int = _MONOLITHIC_MAX,
    ) -> None:
        self.endpoint = endpoint
        self.chunk_size = chunk_size
        self.monolithic_max = monolithic_max
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
        if not is_loopback(host):
            raise RegistryError("plain-http-refused")
        return http.client.HTTPConnection(host, port, timeout=self.endpoint.timeout)

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

    def _call(
        self,
        method: str,
        path: str,
        repository: str | None = None,
        **kwargs: object,
    ) -> Response:
        """Send once; on a 401 with a replayable body, authenticate and resend."""
        if repository is not None and repository not in self._scoped:
            # Authenticate before a streamed body: it cannot be resent.
            self.authenticate(repository)
        response = self._send(method, path, **kwargs)  # type: ignore[arg-type]
        replayable = not isinstance(kwargs.get("body"), Iterator)
        if response.status == 401 and replayable:
            if self._answer(response, repository):
                response = self._send(method, path, **kwargs)  # type: ignore[arg-type]
        if response.status == 401:
            raise RegistryError("registry-unauthorized", 401)
        if response.status == 403:
            raise RegistryError("registry-forbidden", 403)
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
            query.append(("scope", f"repository:{repository}:pull,push"))
        path = realm.path or "/"
        encoded = urllib.parse.urlencode(query)
        if realm.query:
            encoded = realm.query + ("&" + encoded if encoded else "")
        headers = {}
        if creds is not None and creds.scheme == "basic":
            headers["Authorization"] = creds.basic_header()
        connection = self._connection(
            realm.scheme, realm.hostname, realm.port or _default_port(realm.scheme)
        )
        try:
            connection.request(
                "GET", path + ("?" + encoded if encoded else ""), headers=headers
            )
            response = connection.getresponse()
            body = response.read(_MAX_RESPONSE + 1)
        except (OSError, http.client.HTTPException) as error:
            raise RegistryError("registry-unreachable") from error
        finally:
            connection.close()
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
        """Probe ``/v2/`` and answer its challenge for ``repository`` push scope."""
        self._scoped.add(repository)
        response = self._send("GET", "/v2/")
        if response.status == 401 and self._answer(response, repository):
            response = self._send("GET", "/v2/")
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

    def _start_upload(self, repository: str) -> str:
        response = self._call("POST", f"/v2/{repository}/blobs/uploads/", repository)
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
        """
        validate_digest(digest)
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError("invalid blob size")
        reader = _Hashing(source, size, cancel)
        location = self._start_upload(repository)
        if size <= self.monolithic_max:
            self._monolithic(repository, location, digest, size, reader, b"")
            return self._verified(reader, digest, size, "monolithic")
        offset = 0
        while offset < size:
            chunk = reader.read(self.chunk_size)
            response = self._call(
                "PATCH",
                location,
                repository,
                body=chunk,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Range": f"{offset}-{offset + len(chunk) - 1}",
                },
            )
            if response.status in {404, 405, 416, 501} and offset == 0:
                # This registry does not take chunks: restart as one PUT.
                self._cancel_upload(location)
                location = self._start_upload(repository)
                self._monolithic(repository, location, digest, size, reader, chunk)
                return self._verified(reader, digest, size, "monolithic-fallback")
            new_location = response.headers.get("location")
            if response.status != 202 or not new_location:
                self._cancel_upload(location)
                raise RegistryError("upload-rejected", response.status)
            location = self._target(new_location)
            offset += len(chunk)
        if reader.digest != digest:
            self._cancel_upload(location)
            raise RegistryError("blob-digest-mismatch")
        response = self._call(
            "PUT", self._with_digest(location, digest), repository, body=b""
        )
        if response.status not in {201, 204}:
            raise RegistryError("upload-rejected", response.status)
        return self._verified(reader, digest, size, "chunked")

    def _monolithic(
        self,
        repository: str,
        location: str,
        digest: str,
        size: int,
        reader: _Hashing,
        head: bytes,
    ) -> None:
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
        return digest


def _default_port(scheme: str) -> int:
    return 443 if scheme == "https" else 80


_TRUE = {"1", "true", "yes"}
_FALSE = {"0", "false", "no"}


@dataclass(frozen=True)
class RegistryTarget:
    """A parsed ``oci://host[:port]/repository[:tag]`` delivery target.

    ``tls`` defaults to ``False`` for a loopback host and ``True`` otherwise;
    ``?tls=false`` is refused for any non-loopback host. Digest references are
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
        if not self.tls and not is_loopback(self.host):
            raise DeliveryInputError(
                "plain-http-not-loopback",
                "plain HTTP is only allowed for a loopback registry",
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
            tls = not is_loopback(host)
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
