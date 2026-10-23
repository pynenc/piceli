"""Nix builds on build hosts, a signed binary cache and build manifests (0.24.0).

``piceli dev nix build ATTR`` builds a flake output (a NixOS system, a disk
image) as a development run on a build host whose profile uses the host's
Nix (``DevProfile.for_nix()``), and with ``--publish`` pushes the closure,
signed, to a :class:`NixCache` and uploads a signed **build manifest**:

``piceli/<name>/<commit>.json`` in the cache's bucket (or directory)::

    {"schema": "piceli.nix-build.v1", "name": "web-1", "commit": "<sha>",
     "tag": "v1.4.0", "attribute": ".#nixosConfigurations.web-1.config.system.build.toplevel",
     "store_path": "/nix/store/...", "system": "x86_64-linux",
     "nar_hash": "sha256:...", "nar_size": 123, "closure_size": 456,
     "built_at": "2026-10-22T10:00:00Z", "host": "nix-1", "image": null,
     "signed_by": "cache-1", "signature": "cache-1:<base64 ed25519 signature>"}

Next to it, publishing writes a signed **ref pointer** for the tag and the
branch it built, ``piceli/<name>/refs/tags/<tag>.json`` and
``piceli/<name>/refs/heads/<branch>.json`` (``piceli.nix-ref.v1``: the
commit, the manifest's key and sha256, and a ``published_at`` that only
grows), so a host resolves a ref with read access to the cache alone.

A host that follows a Git tag finds the store path of a commit there
without evaluating anything, and trusts it only after
:func:`~piceli.dev.nix_verify.verify_manifest` (the manifest's signature,
with the cache's public key) and
:func:`~piceli.dev.nix_verify.verify_narinfo` (the store path's
``.narinfo``: signed by the same key, ``NarHash`` equal to the
manifest's); both are standard library only. ``piceli dev nix gc`` keeps
the manifests of the last tags and the main branch and every path their
closures reach, and removes the rest after approval.

Keys are Nix's own format: ``NAME:base64(ed25519 seed || public key)`` for
the secret key, ``NAME:base64(public key)`` for the public one; signatures
are ``NAME:base64(signature)``.

Importing this module is side-effect free.
"""

from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import json
import re
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from piceli.dev.model import DevError
from piceli.dev.nix_publish import FEATURES
from piceli.dev.nix_verify import (
    MANIFEST_SCHEMA,
    POINTER_SCHEMA,
    canonical,
    manifest_digest,
    nar_hash,
    narinfo_fields,
    nix32,
    sigv4_headers,
    verify,
    verify_manifest,
    verify_narinfo,
    verify_pointer,
)

__all__ = [
    "MANIFEST_SCHEMA",
    "POINTER_SCHEMA",
    "NixCache",
    "canonical",
    "generate_key",
    "narinfo_fields",
    "nix32",
    "public_key",
    "sign",
    "verify",
    "verify_manifest",
    "verify_narinfo",
    "verify_pointer",
]
PLAN_SCHEMA = "piceli.nix-publish-plan.v1"
RESULT_SCHEMA = "piceli.dev-nix-build.v1"
#: The run's out-link, in the command's directory (artifacts are read through it).
OUT_LINK = "piceli-nix-out"
IMAGE_FORMATS = ("qcow2", "raw", "img", "vhd", "vhdx", "vmdk", "iso")
#: S3 endpoints of providers whose buckets a cache may name.
S3_ENDPOINTS = {"infomaniak": "s3.pub1.infomaniak.cloud"}
_KEY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,62}")
_ATTR = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+-]{0,511}")
_LABEL = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")
_REF = re.compile(r"[A-Za-z0-9_+-][A-Za-z0-9._+/-]{0,199}")
_REV = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_STORE_PATH = re.compile(r"/nix/store/[0-9a-df-np-sv-z]{32}-[A-Za-z0-9+._?=-]+")
_HOST = re.compile(r"[A-Za-z0-9.-]+(?::[0-9]{1,5})?")
_REGION = re.compile(r"[a-z0-9-]{1,32}")


def _invalid(message: str) -> DevError:
    return DevError("dev-nix-cache-invalid", message)


# ------------------------------------------------------------------ keys


def _ed25519() -> Any:
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
    except ImportError:  # pragma: no cover - the crypto extra is missing
        raise DevError(
            "dev-nix-crypto-unavailable",
            "Nix cache keys need the cryptography package (pip install 'piceli[crypto]')",
        ) from None
    return ed25519


def _split(text: str, size: int | tuple[int, ...], what: str) -> tuple[str, bytes]:
    name, sep, body = str(text).strip().partition(":")
    sizes = (size,) if isinstance(size, int) else size
    try:
        raw = base64.b64decode(body, validate=True)
    except ValueError:
        raw = b""
    if not sep or not _KEY_NAME.fullmatch(name) or len(raw) not in sizes:
        raise DevError("dev-nix-key-invalid", f"not a Nix {what} (NAME:base64)")
    return name, raw


def check_public_key(text: str) -> str:
    """``text`` if it is a Nix public key (``NAME:base64`` of 32 bytes)."""
    _split(text, 32, "public key")
    return str(text).strip()


def key_name(text: str) -> str:
    return str(text).strip().partition(":")[0]


def generate_key(name: str) -> str:
    """A new Nix secret key ``NAME:base64(seed || public)``."""
    if not _KEY_NAME.fullmatch(name):
        raise DevError("dev-nix-key-invalid", "a key name is letters, digits, . _ + -")
    from cryptography.hazmat.primitives import serialization

    key = _ed25519().Ed25519PrivateKey.generate()
    raw = serialization.Encoding.Raw
    seed = key.private_bytes(
        raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    public = key.public_key().public_bytes(raw, serialization.PublicFormat.Raw)
    return f"{name}:{base64.b64encode(seed + public).decode()}"


def _private(secret: str) -> tuple[str, Any]:
    name, raw = _split(secret, (64, 32), "secret key")
    return name, _ed25519().Ed25519PrivateKey.from_private_bytes(raw[:32])


def public_key(secret: str) -> str:
    """The public key of a Nix secret key."""
    from cryptography.hazmat.primitives import serialization

    name, key = _private(secret)
    raw = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return f"{name}:{base64.b64encode(raw).decode()}"


def sign(secret: str, message: bytes) -> str:
    """``NAME:base64(ed25519 signature of message)``."""
    name, key = _private(secret)
    return f"{name}:{base64.b64encode(key.sign(message)).decode()}"


# ------------------------------------------------------------------ cache


@dataclass(frozen=True)
class NixCache:
    """A Nix binary cache builds are pushed to and hosts substitute from.

    Exactly one of ``bucket`` (a :class:`~piceli.infra.Bucket`; an S3 API
    at ``endpoint``, known for Infomaniak) or ``url`` (``s3://BUCKET?endpoint=
    HOST&region=R``, ``file:///absolute/dir``, or a read-only ``https://``
    cache that cannot be published to).

    :param public_key: ``NAME:base64``: what hosts trust
        (``piceli secrets setup MODULE`` or ``piceli secrets nix-cache NAME
        --generate`` prints it).
    :param key_name: The key's name alone, for a cache whose key does not
        exist yet (0.25.0): ``NixCache(key_name="cache-1", bucket=...)``.
        ``piceli secrets status`` and ``setup`` work with it, and so does
        ``piceli infra plan`` of the bucket; publishing, ``dev nix gc`` and
        a host's module refuse (``dev-nix-cache-key-unset``), and the
        controller keeps a ``NixRelease`` of it ``rejected``, until the
        public key ``setup`` prints is in ``public_key=``. Given with
        ``public_key=``, it must be that key's name.
    :param signing_key: The local credential holding the secret key
        (default: the key's name).
    :param write_credentials: The local credential with S3 access keys
        that may **put and delete** objects (``piceli secrets s3 NAME
        --prompt``): only the publishing side (``dev nix build --publish``,
        ``dev nix gc``) uses them; required for ``s3://``. Default (0.25.0):
        the ``S3Key(access="write")`` of ``bucket=``, which ``piceli infra
        apply`` creates and stores. Hosts read with separate keys: the
        bucket's ``S3Key(access="read")``, which ``piceli host secrets``
        puts on the host (0.25.0), or keys in sops-nix on the host
        (``NixHost(read_credentials_file=)``).
    :param endpoint: The S3 endpoint host (``s3.example.net``).
    :param region: The S3 region signatures name.
    """

    public_key: str | None = None
    bucket: Any = None
    url: str | None = None
    endpoint: str | None = None
    region: str = "us-east-1"
    signing_key: str | None = None
    write_credentials: str | None = None
    key_name: str | None = None

    def __post_init__(self) -> None:
        self._check_key()
        if (self.bucket is None) == (self.url is None):
            raise _invalid("NixCache: give one of bucket= or url=")
        if self.bucket is not None:
            from piceli.infra.machines.model import Bucket

            if not isinstance(self.bucket, Bucket):
                raise _invalid("NixCache(bucket=) is a piceli.infra.Bucket")
            write = self.bucket.key("write")
            if self.write_credentials is None and write is not None:
                object.__setattr__(self, "write_credentials", write.name)
            endpoint = self.endpoint or S3_ENDPOINTS.get(
                str(getattr(self.bucket.provider, "preset", "") or "")
            )
            if endpoint is None:
                raise _invalid("NixCache: endpoint= names the bucket's S3 endpoint")
            object.__setattr__(self, "endpoint", endpoint)
        else:
            self._check_url(str(self.url))
        if self.endpoint is not None and not _HOST.fullmatch(self.endpoint):
            raise _invalid("NixCache(endpoint=) is a host name (and port)")
        if not _REGION.fullmatch(self.region):
            raise _invalid("NixCache(region=) is a region name")
        for value in (self.signing_key, self.write_credentials):
            if value is not None:
                from piceli.profiles import ProfileError, check_name

                try:
                    check_name(value)
                except ProfileError:
                    raise _invalid(
                        "NixCache credentials are credential names"
                    ) from None
        if self.scheme == "s3" and self.write_credentials is None:
            raise _invalid(
                "NixCache: an s3 cache needs write_credentials= (S3 keys that put)"
            )

    def _check_key(self) -> None:
        if self.public_key is None:
            if self.key_name is None or not _KEY_NAME.fullmatch(str(self.key_name)):
                raise _invalid(
                    "NixCache: give public_key= (NAME:base64), or key_name= "
                    "(letters, digits, . _ + -) until piceli secrets setup "
                    "prints the public key"
                )
            return
        try:
            object.__setattr__(self, "public_key", check_public_key(self.public_key))
        except DevError:
            raise _invalid("NixCache(public_key=) is NAME:base64 of 32 bytes") from None
        name = key_name(self.public_key)
        if self.key_name is not None and self.key_name != name:
            raise _invalid("NixCache(key_name=) is the name of public_key=")
        object.__setattr__(self, "key_name", name)

    @property
    def key_set(self) -> bool:
        """Whether the declaration holds the public key (not only its name)."""
        return self.public_key is not None

    def trusted_key(self) -> str:
        """The public key hosts trust.

        :raises DevError: ``dev-nix-cache-key-unset``: the declaration names
            the key (``key_name=``) without its public key yet."""
        if self.public_key is None:
            raise DevError(
                "dev-nix-cache-key-unset",
                f"NixCache {self.key_name} has no public key yet: run piceli "
                "secrets setup MODULE (or piceli secrets nix-cache "
                f"{self.signing_credential} --generate) and put the public key "
                "it prints into NixCache(public_key=)",
            )
        return self.public_key

    def _check_url(self, url: str) -> None:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme == "file":
            if parsed.netloc or not parsed.path.startswith("/") or parsed.query:
                raise _invalid("NixCache(url=) file:///absolute/path")
        elif parsed.scheme == "s3":
            query = dict(urllib.parse.parse_qsl(parsed.query))
            if not parsed.netloc or parsed.path not in ("", "/"):
                raise _invalid("NixCache(url=) s3://BUCKET?endpoint=HOST&region=R")
            if "secret-key" in query or "@" in parsed.netloc:
                raise _invalid("NixCache(url=) never carries a key or credentials")
            object.__setattr__(self, "endpoint", query.get("endpoint", self.endpoint))
            object.__setattr__(self, "region", query.get("region", self.region))
            object.__setattr__(self, "url", f"s3://{parsed.netloc}")
        elif parsed.scheme == "https":
            if "@" in parsed.netloc or parsed.query:
                raise _invalid("NixCache(url=) https://host/path without credentials")
        else:
            raise _invalid("NixCache(url=) is s3://, file:// or https://")

    @property
    def scheme(self) -> str:
        if self.bucket is not None:
            return "s3"
        return urllib.parse.urlsplit(str(self.url)).scheme

    @property
    def bucket_name(self) -> str | None:
        if self.bucket is not None:
            return str(self.bucket.name)
        if self.scheme == "s3":
            return urllib.parse.urlsplit(str(self.url)).netloc
        return None

    @property
    def read_credentials(self) -> str | None:
        """The credential of the bucket's ``S3Key(access="read")`` that
        ``piceli infra apply`` creates (``None``: hosts get keys another way)."""
        if self.bucket is None:
            return None
        found = self.bucket.key("read")
        return found.name if found is not None else None

    def creates(self, credential: str) -> bool:
        """Whether ``piceli infra apply`` creates ``credential`` (a bucket key)."""
        return self.bucket is not None and any(
            key.name == credential for key in self.bucket.keys
        )

    @property
    def signing_credential(self) -> str:
        return self.signing_key or str(self.key_name)

    @property
    def store_url(self) -> str:
        """The store URL ``nix copy --to`` and substituters use (no secret)."""
        if self.scheme != "s3":
            return str(self.url)
        query = urllib.parse.urlencode(
            {"endpoint": self.endpoint, "region": self.region}
            if self.endpoint
            else {"region": self.region}
        )
        return f"s3://{self.bucket_name}?{query}"

    def manifest_url(self, name: str, commit: str) -> str:
        """Where the manifest of ``name`` at ``commit`` lives (``https://`` for S3)."""
        key = manifest_key(name, commit)
        if self.scheme == "s3":
            return f"https://{self.endpoint or 's3.amazonaws.com'}/{self.bucket_name}/{key}"
        return f"{str(self.url).rstrip('/')}/{key}"

    def describe(self) -> dict[str, Any]:
        return {
            "store_url": self.store_url,
            "public_key": self.trusted_key(),
            "signing_key": self.signing_credential,
            "write_credentials": self.write_credentials,
            **(
                {"read_credentials": self.read_credentials}
                if self.read_credentials
                else {}
            ),
        }


def manifest_key(name: str, commit: str) -> str:
    return f"piceli/{name}/{commit}.json"


def pointer_key(name: str, kind: str, ref: str) -> str:
    """``piceli/NAME/refs/tags/REF.json`` (``kind="tag"``) or ``.../refs/heads/REF.json``."""
    return f"piceli/{name}/refs/{'tags' if kind == 'tag' else 'heads'}/{ref}.json"


def ref_name_ok(ref: str) -> bool:
    """Whether a tag or branch name can be a pointer's key (no ``..``, no ``//``)."""
    return bool(_REF.fullmatch(ref)) and ".." not in ref and "//" not in ref


# ------------------------------------------------------------------ build


def installable(text: str) -> str:
    """``.#ATTR`` for ``ATTR`` or ``.#ATTR`` (only this source's flake)."""
    attr = text[2:] if text.startswith(".#") else text
    if not _ATTR.fullmatch(attr) or ".." in attr:
        raise DevError(
            "dev-nix-invalid",
            "the output is a flake attribute of this source (.#ATTR: letters, "
            "digits, . _ + -)",
        )
    return f".#{attr}"


def default_name(target: str, image: bool) -> str | None:
    """``HOST`` (or ``HOST-image``) for ``.#nixosConfigurations.HOST.…``."""
    parts = target[2:].split(".")
    if (
        len(parts) > 2
        and parts[0] == "nixosConfigurations"
        and _LABEL.fullmatch(parts[1])
    ):
        return f"{parts[1]}-image" if image else parts[1]
    return None


def check_name(name: str) -> str:
    if not _LABEL.fullmatch(name):
        raise DevError("dev-nix-invalid", "--name is a DNS label (my-host)")
    return name


def build_argv(target: str) -> list[str]:
    """The run's command: build ``target``, link it, print its store path."""
    return [
        "nix", *FEATURES, "build", "--print-build-logs",
        "--out-link", OUT_LINK, "--print-out-paths", target,
    ]  # fmt: skip


def image_patterns() -> list[str]:
    """``--artifact`` globs of a disk image under the out-link."""
    return [
        f"{OUT_LINK}/{depth}*.{ext}" for depth in ("", "*/") for ext in IMAGE_FORMATS
    ]


def store_path_of(lines: Sequence[str]) -> str | None:
    """The last store path ``--print-out-paths`` printed."""
    for line in reversed(list(lines)):
        if _STORE_PATH.fullmatch(line.strip()):
            return line.strip()
    return None


def image_of(paths: Sequence[str]) -> dict[str, Any] | None:
    """The image file among the run's artifacts: path, format, size, sha256."""
    for text in paths:
        path = Path(text)
        ext = path.suffix.lstrip(".")
        if ext in IMAGE_FORMATS and path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(chunk)
            return {
                "file": str(path),
                "format": "raw" if ext == "img" else ext,
                "bytes": path.stat().st_size,
                "sha256": digest.hexdigest(),
            }
    return None


# ------------------------------------------------------------------ plan


def publish_plan(
    *,
    target: str,
    name: str,
    commit: str,
    tag: str | None,
    host: str,
    profile: str,
    image: bool,
    cache: NixCache,
    branch: str | None = None,
) -> dict[str, Any]:
    """What ``--publish`` will do; its digest is what the owner approves.

    ``pointers``: the ref pointers it writes (the tag at the commit, the
    branch ``--ref`` or ``--branch`` names), skipped for an image."""
    if not _REV.fullmatch(commit):
        raise DevError("dev-nix-invalid", "--publish builds a commit (--ref)")
    pointers = [
        pointer_key(name, kind, ref)
        for kind, ref in (("tag", tag), ("branch", branch))
        if ref is not None and ref_name_ok(ref) and not image
    ]
    return {
        "schema": PLAN_SCHEMA,
        "attribute": target,
        "name": name,
        "commit": commit,
        "tag": tag,
        "host": host,
        "profile": profile,
        "image": image,
        "cache": cache.describe(),
        "manifest": manifest_key(name, commit),
        "branch": branch,
        "pointers": pointers,
    }


def plan_digest(plan: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical(plan)).hexdigest()


# ------------------------------------------------------------------ manifest


def manifest(
    *,
    name: str,
    commit: str,
    tag: str | None,
    target: str,
    published: Mapping[str, Any],
    host: str,
    key: str,
    image: Mapping[str, Any] | None = None,
    now: _dt.datetime | None = None,
    pinned: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The build manifest, unsigned (:func:`sign_manifest` adds ``signature``).

    ``pinned`` (0.25.0, a gated release): the promotion gate's result. The
    field is present only then, so older manifests verify unchanged."""
    moment = (now or _dt.datetime.now(_dt.UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    closure = published.get("closure_size")
    extra = {"pinned": dict(pinned)} if pinned is not None else {}
    return extra | {
        "schema": MANIFEST_SCHEMA,
        "name": name,
        "commit": commit,
        "tag": tag,
        "attribute": target,
        "store_path": str(published["store_path"]),
        "system": published.get("system"),
        "nar_hash": nar_hash(str(published["nar_hash"])),
        "nar_size": int(published["nar_size"]),
        "closure_size": None if closure is None else int(closure),
        "built_at": moment,
        "host": host,
        "image": None
        if image is None
        else {k: image[k] for k in ("format", "bytes", "sha256")}
        | {"file": Path(str(image["file"])).name},
        "signed_by": key,
    }


def sign_manifest(body: Mapping[str, Any], secret: str) -> dict[str, Any]:
    """``body`` with ``signature``: the cache key's Ed25519 signature over the
    canonical JSON of every other field (:func:`verify_manifest` and
    :func:`verify_pointer` check it, standard library only)."""
    unsigned = {k: v for k, v in body.items() if k != "signature"}
    return {**unsigned, "signature": sign(secret, canonical(unsigned))}


def _stamp(moment: _dt.datetime) -> str:
    return moment.astimezone(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def pointer(
    signed: Mapping[str, Any],
    key: str,
    *,
    previous: Mapping[str, Any] | None = None,
    now: _dt.datetime | None = None,
) -> dict[str, Any]:
    """The pointer ``key`` (:func:`pointer_key`) to the signed manifest
    ``signed``, unsigned. ``published_at`` (UTC, microseconds, compared as
    text) is later than ``previous``'s, even when this clock is behind:
    hosts refuse a pointer older than one they have seen."""
    moment = now or _dt.datetime.now(_dt.UTC)
    stamp = _stamp(moment)
    before = str((previous or {}).get("published_at") or "")
    if before >= stamp:
        try:
            last = _dt.datetime.strptime(before, "%Y-%m-%dT%H:%M:%S.%fZ")
            stamp = _stamp(last.replace(tzinfo=_dt.UTC) + _dt.timedelta(microseconds=1))
        except ValueError:
            pass
    ref = key.split("/refs/", 1)[1][: -len(".json")]
    return {
        "schema": POINTER_SCHEMA,
        "name": signed["name"],
        "ref": f"refs/{ref}",
        "commit": signed["commit"],
        "manifest": manifest_key(str(signed["name"]), str(signed["commit"])),
        "manifest_sha256": manifest_digest(signed),
        "store_path": signed["store_path"],
        "system": signed.get("system"),
        "published_at": stamp,
        "signed_by": signed["signed_by"],
    }


# ------------------------------------------------------------------ cache objects


_sigv4 = sigv4_headers


Opener = Callable[[Any], Any]


class CacheObjects:
    """The objects of a cache: a ``file://`` directory or an S3 bucket
    (SigV4, path-style). Errors never carry a server message or a key."""

    def __init__(
        self,
        cache: NixCache,
        s3_keys: tuple[str, str] | None = None,
        *,
        opener: Opener | None = None,
        clock: Callable[[], _dt.datetime] = lambda: _dt.datetime.now(_dt.UTC),
    ) -> None:
        if cache.scheme not in ("file", "s3") or (
            cache.scheme == "s3" and s3_keys is None
        ):
            raise DevError(
                "dev-nix-cache-invalid",
                "this needs an s3:// cache with its write credentials, or file://",
            )
        self.cache = cache
        self.keys = s3_keys
        self.opener = opener
        self.clock = clock
        self.root = (
            Path(urllib.parse.urlsplit(str(cache.url)).path)
            if cache.scheme == "file"
            else None
        )

    def url(self, key: str) -> str:
        if self.root is not None:
            return f"file://{self.root / key}"
        base = f"https://{self.cache.endpoint or 's3.amazonaws.com'}"
        return (
            f"{base}/{self.cache.bucket_name}/{urllib.parse.quote(key, safe='/-_.~')}"
        )

    def _call(
        self, method: str, url: str, data: bytes = b"", content_type: str | None = None
    ) -> tuple[int, bytes]:
        from urllib import error as urlerror
        from urllib import request as urlrequest

        assert self.keys is not None
        headers = _sigv4(
            method, url, data, region=self.cache.region, access_key=self.keys[0],
            secret_key=self.keys[1], now=self.clock(), content_type=content_type,
        )  # fmt: skip
        headers.pop("host")
        request = urlrequest.Request(
            url, data=data if method == "PUT" else None, method=method, headers=headers
        )
        try:
            with (self.opener or urlrequest.urlopen)(request) as response:
                return int(getattr(response, "status", 200)), response.read()
        except urlerror.HTTPError as refused:
            return int(refused.code), b""
        except Exception:
            return 0, b""

    @staticmethod
    def _gc_failed(what: str, status: int) -> DevError:
        return DevError(
            "dev-nix-gc-failed", f"{what} failed (HTTP {status or 'error'})"
        )

    def put(self, key: str, data: bytes, content_type: str = "application/json") -> str:
        if self.root is not None:
            target = self.root / key
            target.parent.mkdir(parents=True, exist_ok=True)
            staged = target.with_name(target.name + ".tmp")
            staged.write_bytes(data)
            staged.replace(target)
            return self.url(key)
        status, _ = self._call("PUT", self.url(key), data, content_type)
        if not 200 <= status < 300:
            raise DevError(
                "dev-nix-manifest-failed",
                f"storing the manifest failed (HTTP {status or 'error'})",
            )
        return self.url(key)

    def get(self, key: str) -> bytes | None:
        if self.root is not None:
            path = self.root / key
            return path.read_bytes() if path.is_file() else None
        status, body = self._call("GET", self.url(key))
        if status == 404:
            return None
        if not 200 <= status < 300:
            raise self._gc_failed("reading the cache", status)
        return body

    def list(self, prefix: str = "") -> list[tuple[str, float]]:
        """``(key, last modified, epoch seconds)`` of every object under ``prefix``."""
        if self.root is not None:
            found = []
            for path in sorted(self.root.rglob("*")):
                key = path.relative_to(self.root).as_posix()
                if (
                    path.is_file()
                    and key.startswith(prefix)
                    and not key.endswith(".tmp")
                ):
                    found.append((key, path.stat().st_mtime))
            return found
        import xml.etree.ElementTree as ElementTree

        out: list[tuple[str, float]] = []
        token: str | None = None
        while True:
            query = {"list-type": "2", "prefix": prefix}
            if token:
                query["continuation-token"] = token
            base = f"https://{self.cache.endpoint or 's3.amazonaws.com'}/{self.cache.bucket_name}"
            status, body = self._call("GET", f"{base}?{urllib.parse.urlencode(query)}")
            if not 200 <= status < 300:
                raise self._gc_failed("listing the cache", status)
            try:
                root = ElementTree.fromstring(body)
            except ElementTree.ParseError:
                raise self._gc_failed("listing the cache", 0) from None

            def child(node: Any, name: str) -> str:
                found = next(
                    (c for c in node if c.tag.rsplit("}", 1)[-1] == name), None
                )
                return "" if found is None or found.text is None else found.text

            for node in root:
                if node.tag.rsplit("}", 1)[-1] == "Contents":
                    moment = child(node, "LastModified").replace("Z", "+00:00")
                    try:
                        stamp = _dt.datetime.fromisoformat(moment).timestamp()
                    except ValueError:
                        stamp = 0.0
                    out.append((child(node, "Key"), stamp))
            token = child(root, "NextContinuationToken")
            if child(root, "IsTruncated") != "true" or not token:
                return out

    def delete(self, key: str) -> None:
        if self.root is not None:
            (self.root / key).unlink(missing_ok=True)
            return
        status, _ = self._call("DELETE", self.url(key))
        if not (200 <= status < 300 or status == 404):
            raise self._gc_failed("deleting from the cache", status)


def upload_manifest(
    cache: NixCache,
    body: Mapping[str, Any],
    *,
    s3_keys: tuple[str, str] | None,
    opener: Opener | None = None,
    now: _dt.datetime | None = None,
) -> str:
    """Put the manifest into the cache; its URL (``dev-nix-manifest-failed``)."""
    data = json.dumps(body, sort_keys=True, indent=2).encode() + b"\n"
    objects = CacheObjects(
        cache, s3_keys, opener=opener,
        clock=(lambda: now) if now is not None else lambda: _dt.datetime.now(_dt.UTC),
    )  # fmt: skip
    return objects.put(manifest_key(str(body["name"]), str(body["commit"])), data)


def upload_pointers(
    cache: NixCache,
    signed: Mapping[str, Any],
    keys: Sequence[str],
    secret: str,
    *,
    s3_keys: tuple[str, str] | None,
    opener: Opener | None = None,
    now: _dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """Sign and put each ref pointer, after its manifest (a host never finds
    a pointer to a manifest that is not there yet)."""
    objects = CacheObjects(cache, s3_keys, opener=opener)
    written = []
    for key in keys:
        try:
            old = json.loads(objects.get(key) or b"{}")
        except (ValueError, DevError):
            old = {}
        body = sign_manifest(
            pointer(signed, key, previous=old if isinstance(old, dict) else None,
                    now=now),
            secret,
        )  # fmt: skip
        objects.put(key, json.dumps(body, sort_keys=True, indent=2).encode() + b"\n")
        written.append(body)
    return written


def publish_closure(
    cache: NixCache,
    plan: Mapping[str, Any],
    store_path: str,
    host: str,
    image: Mapping[str, Any] | None,
    secret: str,
    s3_keys: tuple[str, str] | None,
    *,
    say: Callable[[str], None],
    push: Callable[[Mapping[str, Any], Callable[[str], None]], Mapping[str, Any]],
) -> dict[str, Any]:
    """Publish a built store path as ``plan`` says: ``push`` signs and pushes
    the closure on the build host (the keys on its stdin only, see
    :func:`run_publisher`), then the signed manifest and the plan's ref
    pointers go into the cache. :raises DevError: a step failed."""
    say(f"publishing {store_path} to {cache.store_url} (on {host})")
    request = {
        "store_path": store_path,
        "store_url": cache.store_url,
        "signing_key": secret,
        "s3": None
        if s3_keys is None
        else {"access_key_id": s3_keys[0], "secret_access_key": s3_keys[1]},
    }
    outcome = push(request, say)
    del request
    signed = sign_manifest(
        manifest(
            name=plan["name"], commit=plan["commit"], tag=plan["tag"],
            target=plan["attribute"], published=outcome, host=host,
            key=str(cache.key_name), image=image, pinned=plan.get("pinned"),
        ),
        secret,
    )  # fmt: skip
    url = upload_manifest(cache, signed, s3_keys=s3_keys)
    pointers = upload_pointers(
        cache, signed, plan.get("pointers") or [], secret, s3_keys=s3_keys
    )
    say(f"published; manifest {url}")
    for body in pointers:
        say(f"  {body['ref']} -> {str(body['commit'])[:12]} ({body['published_at']})")
    return {
        "cache": cache.store_url,
        "public_key": cache.trusted_key(),
        "nar_hash": outcome["nar_hash"],
        "nar_size": outcome["nar_size"],
        "signatures": outcome.get("signatures") or [],
        "system": outcome.get("system"),
        "closure_size": signed.get("closure_size"),
        "manifest": plan["manifest"],
        "manifest_url": url,
        "manifest_body": signed,
        "pointers": [
            {k: body[k] for k in ("ref", "commit", "published_at")} for body in pointers
        ],
    }


# ------------------------------------------------------------------ git


def _git(repo: Path, *args: str) -> str:
    import subprocess

    done = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    return done.stdout.strip() if done.returncode == 0 else ""


def tag_of(
    repo: Path, ref: str | None, commit: str, pattern: str | None = None
) -> str | None:
    """The tag being built: ``--ref`` when it is a tag, else a tag at the commit.

    With ``pattern`` (a ``NixHost``'s ``Tag`` pattern, ``--tag-pattern``)
    only a tag whose version after the pattern's prefix parses counts
    (:func:`piceli.hosts.agent.tag_version`): ``--ref`` when it is one, else
    the highest one at the commit, else ``None``; a ``--ref`` tag of another
    pattern is refused (``dev-nix-invalid``)."""
    from piceli.hosts.agent import tag_version

    is_tag = bool(ref and _git(repo, "rev-parse", "--verify", "-q", f"refs/tags/{ref}"))
    if pattern is None:
        if is_tag:
            return ref
        tags = sorted(_git(repo, "tag", "--points-at", commit).split())
        return tags[0] if tags else None
    if is_tag and ref is not None:
        if tag_version(pattern, ref) is None:
            raise DevError(
                "dev-nix-invalid",
                f"--ref {ref} is not a version tag of --tag-pattern {pattern}",
            )
        return ref
    ranked = [
        (version, tag)
        for tag in _git(repo, "tag", "--points-at", commit).split()
        if (version := tag_version(pattern, tag)) is not None
    ]
    return max(ranked)[1] if ranked else None


def branch_of(repo: Path, ref: str) -> str | None:
    """The branch ``--ref`` names (``main``, ``origin/main``), else ``None``."""
    if not ref_name_ok(ref):
        return None
    if _git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{ref}"):
        return ref
    if "/" in ref and _git(repo, "rev-parse", "--verify", "-q", f"refs/remotes/{ref}"):
        return ref.split("/", 1)[1]
    return None


def kept_commits(
    repo: Path, keep_tags: int, main: str, patterns: Sequence[str] = ()
) -> dict[str, list[str]]:
    """The commits whose builds stay: the newest ``keep_tags`` tags (by tag or
    commit date) and ``main``; each with the refs that name it.

    With ``patterns`` (each host's ``Tag`` pattern, ``--tag-pattern``), the
    highest ``keep_tags`` versions of each pattern instead (the version
    after its prefix, :func:`piceli.hosts.agent.tag_version`): one target's
    releases never push another's out."""
    from piceli.hosts.agent import tag_version

    kept: dict[str, list[str]] = {}
    tags = _git(
        repo, "for-each-ref", "--sort=-creatordate", "--format=%(refname:short)",
        "refs/tags",
    ).split()  # fmt: skip
    if patterns:
        chosen: list[str] = []
        for pattern in patterns:
            ranked = sorted(
                (
                    (version, tag)
                    for tag in tags
                    if (version := tag_version(pattern, tag)) is not None
                ),
                reverse=True,
            )
            chosen += [tag for _, tag in ranked[: max(0, keep_tags)]]
        tags = list(dict.fromkeys(chosen))
        keep_tags = len(tags)
    for tag in tags[: max(0, keep_tags)]:
        commit = _git(repo, "rev-parse", "--verify", "-q", f"{tag}^{{commit}}")
        if commit:
            kept.setdefault(commit, []).append(tag)
    head = _git(repo, "rev-parse", "--verify", "-q", f"{main}^{{commit}}")
    if not head:
        raise DevError("dev-nix-invalid", f"--main {main}: no such branch here")
    kept.setdefault(head, []).append(main)
    return kept


# ------------------------------------------------------------------ retention


GC_SCHEMA = "piceli.nix-gc-plan.v1"
_MANIFEST_KEY = re.compile(r"piceli/([a-z0-9-]{1,63})/([0-9a-f]{40,64})\.json")
_POINTER_KEY = re.compile(r"piceli/([a-z0-9-]{1,63})/refs/(tags|heads)/(.+)\.json")


def gc_plan(
    objects: CacheObjects,
    kept: Mapping[str, Sequence[str]],
    *,
    keep_tags: int,
    main: str,
    grace_seconds: float,
    now: float,
    tag_patterns: Sequence[str] = (),
) -> dict[str, Any]:
    """What ``piceli dev nix gc`` would delete: tag pointers of tags not
    kept, manifests of other commits that no remaining pointer names, then
    the ``.narinfo`` (and ``.ls``) of every path no kept manifest's closure
    reaches, then NAR files no kept ``.narinfo`` names. Branch pointers stay
    (each names the branch's current build). Objects younger than
    ``grace_seconds`` stay (a publish in progress)."""
    listed = objects.list()
    young = {key for key, stamp in listed if now - stamp < grace_seconds}
    manifests: dict[str, tuple[str, str]] = {}
    for key, _ in listed:
        match = _MANIFEST_KEY.fullmatch(key)
        if match:
            manifests[key] = (match.group(1), match.group(2))
    kept_refs = {ref for refs in kept.values() for ref in refs}
    named: set[str] = set()
    drop_pointers: list[str] = []
    for key, _ in listed:
        match = _POINTER_KEY.fullmatch(key)
        if not match:
            continue
        try:
            body = json.loads(objects.get(key) or b"{}")
        except ValueError:
            body = {}
        commit = body.get("commit") if isinstance(body, dict) else None
        if (
            match.group(2) == "heads"
            or match.group(3) in kept_refs
            or commit in kept
            or key in young
        ):
            if isinstance(body, dict) and isinstance(body.get("manifest"), str):
                named.add(body["manifest"])
        else:
            drop_pointers.append(key)
    keep_manifests = sorted(
        key
        for key, (_, commit) in manifests.items()
        if commit in kept or key in young or key in named
    )
    roots: list[str] = []
    for key in keep_manifests:
        try:
            body = json.loads(objects.get(key) or b"{}")
        except ValueError:
            body = {}
        path = body.get("store_path") if isinstance(body, dict) else None
        if isinstance(path, str) and _STORE_PATH.fullmatch(path):
            roots.append(Path(path).name.split("-", 1)[0])
    narinfos = {
        key[: -len(".narinfo")]: key
        for key, _ in listed
        if "/" not in key and key.endswith(".narinfo")
    }
    reach: set[str] = set()
    nars: set[str] = set()
    pending = list(roots) + [h for h, key in narinfos.items() if key in young]
    while pending:
        part = pending.pop()
        if part in reach:
            continue
        reach.add(part)
        if part not in narinfos:
            continue
        fields = narinfo_fields((objects.get(narinfos[part]) or b"").decode())
        if fields.get("URL"):
            nars.add(fields["URL"])
        for ref in fields.get("References", "").split():
            pending.append(ref.split("-", 1)[0])
    delete = sorted(
        drop_pointers
        + [key for key in manifests if key not in keep_manifests]
        + [
            key
            for part, key in narinfos.items()
            if part not in reach and key not in young
        ]
        + [
            key
            for key, _ in listed
            if "/" not in key
            and key.endswith(".ls")
            and key[: -len(".ls")] not in reach
            and key not in young
        ]
        + [
            key
            for key, _ in listed
            if key.startswith("nar/") and key not in nars and key not in young
        ]
    )
    return {
        "schema": GC_SCHEMA,
        "cache": objects.cache.store_url,
        "keep_tags": keep_tags,
        # Only when given: other plans (and their digests) are unchanged.
        **({"tag_patterns": list(tag_patterns)} if tag_patterns else {}),
        "main": main,
        "kept": [
            {
                "commit": commit,
                "refs": list(refs),
                "manifests": [k for k in keep_manifests if manifests[k][1] == commit],
            }
            for commit, refs in sorted(kept.items())
        ],
        "kept_paths": len(reach & set(narinfos)),
        "delete": delete,
    }


def gc_apply(objects: CacheObjects, plan: Mapping[str, Any]) -> int:
    """Delete the plan's objects: pointers first, then manifests, NAR files
    last (a host never finds a pointer or manifest whose paths are gone)."""

    def rank(key: str) -> int:
        if _POINTER_KEY.fullmatch(key):
            return 0
        if key.startswith("piceli/"):
            return 1
        return 3 if key.startswith("nar/") else 2

    keys = sorted(plan["delete"], key=lambda key: (rank(key), key))
    for key in keys:
        objects.delete(key)
    return len(keys)


# ------------------------------------------------------------------ publish


def publisher_source() -> str:
    """The stdlib publisher (:mod:`piceli.dev.nix_publish`) as one script."""
    from piceli.dev import nix_publish

    return Path(nix_publish.__file__).read_text()


def publisher_argv(host: Any, image: str) -> list[str]:
    """How the publisher runs on ``host``, after the build: a native host's
    Python, or a short container of the run image on a docker host with the
    host's Nix mounted as for the run (never on a cluster node)."""
    import sys

    source = publisher_source()
    if host.kind == "native":
        return [sys.executable, "-I", "-c", source]
    if host.kind == "docker":
        from piceli.dev.hosts import NIX_MOUNT, ContainerCli

        return ContainerCli(host).argv(
            "run", "--rm", "-i", "--user", "65532:65532", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges", *NIX_MOUNT, "-e", "HOME=/tmp",
            "--entrypoint", "python3", host.image or image, "-I", "-c", source,
        )  # fmt: skip
    raise DevError(
        "dev-nix-host-unsupported",
        f"build host {host.name} is a cluster node; Nix builds run on docker "
        "or native hosts",
    )


def run_publisher(
    argv: list[str],
    request: Mapping[str, Any],
    *,
    runner: Callable[..., Any] | None = None,
    say: Callable[[str], None] = lambda _line: None,
) -> dict[str, Any]:
    """Run the publisher with ``request`` on its stdin; its result line.

    Its output is masked (the key and the S3 secret never reach ``say``);
    :class:`DevError` ``dev-nix-publish-failed`` when it reports no success.
    """
    import subprocess

    from piceli.dev.nix_publish import COPY_SECONDS, RESULT_MARKER, mask

    s3 = request.get("s3") or {}
    secrets = [
        str(request.get("signing_key") or ""),
        str(s3.get("secret_access_key") or ""),
    ]
    try:
        done = (runner or subprocess.run)(
            argv,
            input=json.dumps(dict(request)).encode(),
            capture_output=True,
            timeout=COPY_SECONDS + 1800,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise DevError(
            "dev-nix-publish-failed", "the publisher could not run on the host"
        ) from None
    for line in (done.stderr or b"").decode("utf-8", "replace").splitlines()[-40:]:
        say(f"  | {mask(line, secrets)}")
    outcome: dict[str, Any] = {}
    for line in (done.stdout or b"").decode("utf-8", "replace").splitlines():
        if line.startswith(RESULT_MARKER):
            try:
                outcome = json.loads(line[len(RESULT_MARKER) :])
            except ValueError:
                outcome = {}
    if outcome.get("state") != "published":
        raise DevError(
            "dev-nix-publish-failed",
            "signing or pushing the closure failed"
            + (f" ({outcome['reason']})" if outcome.get("reason") else ""),
        )
    return outcome
