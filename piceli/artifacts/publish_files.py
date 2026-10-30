"""Publish local files as one OCI artifact, and fetch one back by digest (0.32.0).

``piceli artifacts publish-files`` gives release downloads (archives, a
checksum list, its signature) a content-addressed home in a registry the
images already use:

1. **Plan** (:meth:`FilesPlan.from_paths`, no network): each file's name,
   size and sha256, the target repository, the tag, the artifact type and
   the manifest digest, and a digest over all of it. Nothing is sent without
   an approval of that digest.
2. **Blobs**: each file is one layer, stored as it is (no compression, no
   tar), so its digest is the file's own sha256. Each is hashed again just
   before it is sent; a file that changed since the plan is refused
   (``publish-input-changed``). Only blobs the registry lacks are uploaded.
3. **The manifest** by digest: an OCI image manifest with ``artifactType``,
   the empty config (``application/vnd.oci.empty.v1+json``) and one layer per
   file, annotated ``org.opencontainers.image.title`` with its name. It holds
   no time, so the same files give the same manifest digest every time.
4. **The tag** last. A tag that already names another manifest is refused
   before anything is pushed (``publish-tag-exists``) unless ``move_tag``;
   then everything is read back.

Anyone who can pull the repository fetches one file by its digest:
``GET /v2/<repository>/blobs/<sha256>`` (:func:`fetch_file`,
``piceli artifacts fetch-file``). The receipt
(``piceli.publish-files-receipt.v1``) lists the manifest digest and every
file's digest and URL. Credentials are never printed. Importing this module
runs nothing.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from piceli.artifacts.build_spec import _now
from piceli.artifacts.image_manifest import OCI_MANIFEST
from piceli.artifacts.plan import canonical, digest, validate_digest
from piceli.artifacts.publish import (
    EMPTY_CONFIG,
    EMPTY_MEDIA,
    PublishError,
    PublishGrant,
)
from piceli.artifacts.registry import (
    TAG,
    RegistryCredentials,
    RegistryEndpoint,
    RegistryError,
    RegistryTarget,
    StreamedOciRegistryClient,
)

__all__ = [
    "FILES_PLAN_REVISION",
    "FILES_RECEIPT_REVISION",
    "FILE_MEDIA",
    "FilePublisher",
    "FilesPlan",
    "PublishedFile",
    "blob_url",
    "fetch_file",
]

FILES_PLAN_REVISION = "piceli.publish-files-plan.v1"
FILES_RECEIPT_REVISION = "piceli.publish-files-receipt.v1"
#: Each file's layer media type: the bytes as they are.
FILE_MEDIA = "application/octet-stream"
TITLE = "org.opencontainers.image.title"
#: A file name in the artifact: one path segment, no hidden or odd names.
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,254}")
#: ``type/subtype`` (RFC 6838 restricted names), as ``artifactType`` is.
_MEDIA_TYPE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}"
)
MAX_FILES = 256
_BLOCK = 1024 * 1024


@dataclass(frozen=True)
class PublishedFile:
    """One local file of the artifact: its name in it, size and sha256."""

    name: str
    path: Path = field(repr=False)
    size: int
    sha256: str

    def descriptor(self) -> dict[str, Any]:
        return {
            "mediaType": FILE_MEDIA,
            "digest": self.sha256,
            "size": self.size,
            "annotations": {TITLE: self.name},
        }

    def public(self) -> dict[str, Any]:
        return {"name": self.name, "size": self.size, "digest": self.sha256}


def _hash_file(path: Path) -> tuple[int, str]:
    """``(size, sha256 digest)`` of a regular file, read in blocks."""
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while block := stream.read(_BLOCK):
            hasher.update(block)
            size += len(block)
    return size, "sha256:" + hasher.hexdigest()


@dataclass(frozen=True)
class FilesPlan:
    """What ``publish-files`` pushes: these files, as one artifact, under ``tag``."""

    target: RegistryTarget
    tag: str
    artifact_type: str
    files: tuple[PublishedFile, ...]
    move_tag: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.tag, str) or not TAG.fullmatch(self.tag):
            raise PublishError("publish-invalid", "invalid tag")
        if self.target.tag is not None:
            raise PublishError(
                "publish-invalid", "give the tag with --tag, not in --to"
            )
        if not isinstance(self.artifact_type, str) or not _MEDIA_TYPE.fullmatch(
            self.artifact_type
        ):
            raise PublishError(
                "publish-invalid", "--artifact-type is a media type (type/subtype)"
            )
        names = [item.name for item in self.files]
        if not names or len(names) > MAX_FILES:
            raise PublishError("publish-invalid", f"give 1..{MAX_FILES} files")
        if len(set(names)) != len(names):
            raise PublishError("publish-invalid", "two files have the same name")
        for item in self.files:
            if not _NAME.fullmatch(item.name):
                raise PublishError("publish-invalid", "unusable file name")
            validate_digest(item.sha256)

    @classmethod
    def from_paths(
        cls,
        paths: Sequence[Path],
        to: str,
        tag: str,
        artifact_type: str,
        *,
        move_tag: bool = False,
        names: Sequence[str] | None = None,
    ) -> FilesPlan:
        """The plan for ``paths`` (each named by its file name, or by
        ``names``). No network."""
        try:
            target = RegistryTarget.parse(to)
        except ValueError as error:
            code = getattr(error, "code", "publish-invalid")
            raise PublishError(code, "invalid --to registry target") from None
        files = []
        for position, path in enumerate(paths):
            path = Path(path).absolute()
            if path.is_symlink() or not path.is_file():
                raise PublishError("publish-invalid", "every input is a regular file")
            size, sha = _hash_file(path)
            name = names[position] if names is not None else path.name
            files.append(PublishedFile(name, path, size, sha))
        files.sort(key=lambda item: item.name)
        return cls(target, tag, artifact_type, tuple(files), move_tag)

    @classmethod
    def from_receipt(
        cls,
        receipt: Path,
        to: str,
        tag: str,
        artifact_type: str,
        *,
        output_dir: Path | None = None,
        move_tag: bool = False,
    ) -> FilesPlan:
        """0.39: the plan for the ``[[output.files]]`` a host build receipt lists.

        Each file is read from ``output_dir`` (default: the receipt's
        directory, where ``build-spec run --out`` puts the outputs) and must
        still have the size and sha256 the receipt recorded
        (``publish-input-changed``). No network.
        """
        from piceli.artifacts.build_spec import BuildReceipt, BuildSpecError

        try:
            data = BuildReceipt.from_json(Path(receipt).read_text()).data
        except (OSError, UnicodeDecodeError, BuildSpecError):
            raise PublishError("publish-invalid", "not a build receipt") from None
        details = (data.get("outputs") or {}).get("file_details") or {}
        if not isinstance(details, dict) or not details:
            raise PublishError(
                "publish-invalid", "the receipt lists no [[output.files]]"
            )
        root = (output_dir or Path(receipt).absolute().parent).absolute()
        plan = cls.from_paths(
            [root.joinpath(*Path(path).parts) for path in sorted(details)],
            to,
            tag,
            artifact_type,
            move_tag=move_tag,
            names=[str(details[path]["file"]) for path in sorted(details)],
        )
        by_name = {str(entry["file"]): entry for entry in details.values()}
        for item in plan.files:
            entry = by_name[item.name]
            if item.sha256 != entry.get("sha256") or item.size != entry.get("size"):
                raise PublishError("publish-input-changed", item.name)
        return plan

    @property
    def manifest(self) -> bytes:
        """The artifact manifest's exact bytes (no time in it: reproducible)."""
        return canonical(
            {
                "schemaVersion": 2,
                "mediaType": OCI_MANIFEST,
                "artifactType": self.artifact_type,
                "config": {
                    "mediaType": EMPTY_MEDIA,
                    "digest": digest(EMPTY_CONFIG),
                    "size": len(EMPTY_CONFIG),
                    "data": "e30=",
                },
                "layers": [item.descriptor() for item in self.files],
            }
        )

    @property
    def manifest_digest(self) -> str:
        return digest(self.manifest)

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": FILES_PLAN_REVISION,
            "target": self.target.identity,
            "registry": self.target.registry,
            "repository": self.target.repository,
            "tag": self.tag,
            "artifact_type": self.artifact_type,
            "manifest_digest": self.manifest_digest,
            "files": [item.public() for item in self.files],
            "move_tag": self.move_tag,
        }

    @property
    def digest(self) -> str:
        return digest(canonical(self.to_dict()))

    def preview(self) -> dict[str, Any]:
        return {**self.to_dict(), "digest": self.digest}


def blob_url(target: RegistryTarget, value: str) -> str:
    """The URL that serves blob ``value`` of ``target``'s repository."""
    scheme = "https" if target.tls else "http"
    return f"{scheme}://{target.registry}/v2/{target.repository}/blobs/{value}"


@dataclass
class FilePublisher:
    """Runs an approved :class:`FilesPlan` (see the module docstring)."""

    credentials: RegistryCredentials | None = field(default=None, repr=False)
    ca_file: Path | None = field(default=None, repr=False)
    client_factory: Callable[[RegistryEndpoint], StreamedOciRegistryClient] = field(
        default=StreamedOciRegistryClient, repr=False
    )
    timeout: float = 1800.0

    def publish(
        self,
        plan: FilesPlan,
        grant: PublishGrant,
        *,
        cancel: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Push ``plan``; the receipt (a registry failure is ``state: failed``)."""
        if grant.digest != plan.digest:
            raise PublishError("publish-not-approved", "publish digest not approved")
        if grant.expires_at <= time.time():
            raise PublishError("grant-expired", "publish grant expired")
        started, started_at = time.monotonic(), _now()
        target = plan.target
        receipt: dict[str, Any] = {
            "revision": FILES_RECEIPT_REVISION,
            "digest": plan.digest,
            "registry": target.registry,
            "repository": f"{target.registry}/{target.repository}",
            "tag": plan.tag,
            "artifact_type": plan.artifact_type,
            "manifest_digest": plan.manifest_digest,
            "files": [
                {**item.public(), "url": blob_url(target, item.sha256), "result": None}
                for item in plan.files
            ],
            "blobs": {"uploaded": 0, "skipped": 0, "uploaded_bytes": 0},
            "previous": None,
            "result": None,
            "state": "published",
            "reason": None,
            "started_at": started_at,
        }
        endpoint = target.endpoint(
            credentials=self.credentials, ca_file=self.ca_file, timeout=120.0
        )
        try:
            self._push(plan, endpoint, receipt, cancel)
        except PublishError as error:
            receipt.update(state="failed", reason=error.code)
        except RegistryError as error:
            receipt.update(state="failed", reason=error.reason)
        receipt["finished_at"] = _now()
        receipt["seconds"] = round(time.monotonic() - started, 3)
        return receipt

    def _push(
        self,
        plan: FilesPlan,
        endpoint: RegistryEndpoint,
        receipt: dict[str, Any],
        cancel: threading.Event | None,
    ) -> None:
        repository = plan.target.repository
        manifest, value = plan.manifest, plan.manifest_digest
        client = self.client_factory(endpoint)
        client.authenticate(repository)
        previous = client.manifest_digest(repository, plan.tag)
        if previous is not None and previous != value:
            if not plan.move_tag:
                raise PublishError(
                    "publish-tag-exists", "the tag names another artifact"
                )
            receipt["previous"] = previous
        for item, entry in zip(plan.files, receipt["files"], strict=True):
            if cancel is not None and cancel.is_set():
                raise PublishError("publish-failed", "cancelled")
            if client.blob_size(repository, item.sha256) is not None:
                entry["result"] = "already-present"
                receipt["blobs"]["skipped"] += 1
                continue
            with _opened(item) as stream:
                client.push_blob(
                    repository, item.sha256, item.size, stream, cancel=cancel
                )
            entry["result"] = "pushed"
            receipt["blobs"]["uploaded"] += 1
            receipt["blobs"]["uploaded_bytes"] += item.size
        empty = digest(EMPTY_CONFIG)
        if client.blob_size(repository, empty) is None:
            client.push_blob(
                repository, empty, len(EMPTY_CONFIG), io.BytesIO(EMPTY_CONFIG)
            )
        present = client.manifest_digest(repository, value) == value
        if not present:
            client.push_manifest(repository, value, manifest, OCI_MANIFEST)
        if previous != value:
            client.push_manifest(repository, plan.tag, manifest, OCI_MANIFEST)
        changed = previous != value or not present or receipt["blobs"]["uploaded"]
        receipt["result"] = "pushed" if changed else "already-present"
        # Read back: the tag names the manifest, its bytes, every blob.
        if client.manifest_digest(repository, plan.tag) != value:
            raise PublishError("publish-verification-failed")
        served, media = client.get_manifest(repository, value)
        if served != manifest or media.split(";")[0].strip() != OCI_MANIFEST:
            raise PublishError("publish-verification-failed")
        for item in plan.files:
            if client.blob_size(repository, item.sha256) is None:
                raise PublishError("publish-verification-failed")


class _opened:
    """The file opened for upload, after checking it is still what was planned."""

    def __init__(self, item: PublishedFile) -> None:
        self.item = item
        self.stream: Any = None

    def __enter__(self) -> Any:
        try:
            if self.item.path.is_symlink():
                raise OSError("a link")
            size, sha = _hash_file(self.item.path)
            self.stream = self.item.path.open("rb")
        except OSError:
            raise PublishError("publish-input-changed") from None
        if (size, sha) != (self.item.size, self.item.sha256):
            self.stream.close()
            raise PublishError("publish-input-changed")
        return self.stream

    def __exit__(self, *exc: object) -> None:
        self.stream.close()


# ---------------------------------------------------------------- fetch


def fetch_file(
    source: str,
    value: str,
    out: Path,
    *,
    credentials: RegistryCredentials | None = None,
    ca_file: Path | None = None,
    client_factory: Callable[
        [RegistryEndpoint], StreamedOciRegistryClient
    ] = StreamedOciRegistryClient,
    max_size: int = 4 * 1024 * 1024 * 1024,
) -> dict[str, Any]:
    """Download blob ``value`` of ``source`` (``oci://host/repository``) to ``out``.

    Anonymous unless ``credentials`` (a public repository's anonymous token
    is fetched as the registry asks). The bytes are hashed as they arrive;
    ``out`` is written only when they match ``value`` (``blob-digest-mismatch``
    otherwise), atomically, and is never left half-written.
    """
    validate_digest(value)
    try:
        target = RegistryTarget.parse(source)
    except ValueError as error:
        code = getattr(error, "code", "publish-invalid")
        raise PublishError(code, "invalid --from registry repository") from None
    if target.tag is not None:
        raise PublishError("publish-invalid", "--from names a repository, not a tag")
    endpoint = target.endpoint(credentials=credentials, ca_file=ca_file, timeout=120.0)
    client = client_factory(endpoint)
    client.actions = "pull"
    out = out.absolute()
    out.parent.mkdir(parents=True, exist_ok=True)
    partial = out.with_name(f".{out.name}.{os.getpid()}.partial")
    hasher = hashlib.sha256()
    size = 0
    try:
        with client.open_blob(target.repository, value) as stream:
            descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(descriptor, "wb") as handle:
                while block := stream.read(_BLOCK):
                    size += len(block)
                    if size > max_size:
                        raise RegistryError("registry-response-too-large")
                    hasher.update(block)
                    handle.write(block)
        if "sha256:" + hasher.hexdigest() != value:
            raise RegistryError("blob-digest-mismatch")
        os.replace(partial, out)
    finally:
        partial.unlink(missing_ok=True)
    return {
        "state": "fetched",
        "repository": f"{target.registry}/{target.repository}",
        "digest": value,
        "size": size,
        "url": blob_url(target, value),
    }
