"""Publish built images to a hosted registry, for other people's clusters.

``piceli artifacts publish`` takes a build receipt (``build-spec run``, one or
more platforms, Docker or host builder) and pushes each image to
``<registry>/<prefix>/<key>`` (``key``: the last path segment of the image's
repository, the key a Helm chart made by ``piceli chart`` uses for it):

1. **Plan** (:meth:`PublishPlan.from_receipt`, no network): the images, their
   per-platform config digests, the version tag, the attestations and the
   signing key, and a digest over all of it. Nothing is sent without an
   approval of that digest.
2. **Per-platform manifests by digest**
   (:class:`~piceli.artifacts.registry_delivery.RegistryDelivery`): the config
   digest recomputed from the archive or the local engine must equal the
   receipt's; only blobs the registry lacks are uploaded.
3. **The image index** (:func:`~piceli.artifacts.multi_platform.image_index`)
   by digest. A version tag that already names another index is refused
   (``publish-tag-exists``) unless ``move_tag``.
4. **Attestations**: each platform's SBOM (SPDX) and provenance (in-toto)
   are pushed as OCI 1.1 referrers of that platform's manifest (an artifact
   manifest whose ``subject`` is the image). A registry without the referrers
   API gets the fallback tag ``sha256-<digest>`` (an index of the referrers),
   as the OCI distribution spec describes.
5. **Signing** (optional, :mod:`piceli.artifacts.signing`): the index, every
   platform manifest and every attestation are signed before the tag moves.
6. **The tag** last, so a client never pulls a tag whose signature or SBOM is
   not there yet; then everything is read back and checked.

The receipt (``piceli.publish-receipt.v1``) gives, per image, ``repository``
(with the registry), ``tag``, ``digest`` (the index), each platform's
manifest digest, the blobs uploaded and skipped, the attestations and the
signatures; ``values`` is the ``images:`` fragment of Helm values.
Credentials are never printed. Importing this module runs nothing.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from piceli.artifacts.build_spec import BuildReceipt, _now
from piceli.artifacts.delivery import ArchiveSource, DeliveryGrant, DockerImageSource
from piceli.artifacts.image_manifest import (
    INDEX_TYPES,
    MANIFEST_TYPES,
    OCI_INDEX,
    OCI_MANIFEST,
    manifest_config_digest,
)
from piceli.artifacts.multi_platform import IndexEntry, image_index
from piceli.artifacts.plan import canonical, digest, validate_digest
from piceli.artifacts.process import ProcessLimits, ToolPin
from piceli.artifacts.registry import (
    TAG,
    RegistryCredentials,
    RegistryEndpoint,
    RegistryError,
    RegistryTarget,
    StreamedOciRegistryClient,
)
from piceli.artifacts.registry_delivery import RegistryDelivery

PUBLISH_PLAN_REVISION = "piceli.publish-plan.v1"
PUBLISH_RECEIPT_REVISION = "piceli.publish-receipt.v1"
#: ``artifactType`` and layer media type of each attachment.
ATTESTATION_TYPES = {
    "sbom": "application/spdx+json",
    "provenance": "application/vnd.in-toto+json",
}
EMPTY_CONFIG = b"{}"
EMPTY_MEDIA = "application/vnd.oci.empty.v1+json"
_MAX_ATTESTATION = 64 * 1024 * 1024


class PublishError(ValueError):
    """A publish input or step failed. ``code`` is a registered error code."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


class Signer(Protocol):
    """Signs digests pushed to one repository (see :mod:`piceli.artifacts.signing`)."""

    def public(self) -> dict[str, Any]: ...

    def sign(
        self, target: RegistryTarget, repository: str, digests: Sequence[str]
    ) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class Attestation:
    kind: str  # "sbom" or "provenance"
    path: Path = field(repr=False)
    sha256: str

    def public(self) -> dict[str, Any]:
        return {"kind": self.kind, "sha256": self.sha256}


@dataclass(frozen=True)
class PlatformImage:
    """One platform's image as a build produced it."""

    platform: str
    config_digest: str
    manifest_digest: str | None
    archive: Path | None = field(default=None, repr=False)
    local_image: str | None = None
    attestations: tuple[Attestation, ...] = ()

    @property
    def source(self) -> ArchiveSource | DockerImageSource:
        if self.archive is not None:
            return ArchiveSource(self.archive)
        assert self.local_image is not None
        return DockerImageSource(self.local_image)

    def public(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "config_digest": self.config_digest,
            "manifest_digest": self.manifest_digest,
            "source": "archive" if self.archive is not None else "docker",
            "attestations": [item.public() for item in self.attestations],
        }


@dataclass(frozen=True)
class ImagePublication:
    """One image, every platform, to one repository."""

    name: str
    key: str
    repository: str
    platforms: tuple[PlatformImage, ...]
    index_digest: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "key": self.key,
            "repository": self.repository,
            "index_digest": self.index_digest,
            "platforms": [item.public() for item in self.platforms],
        }


@dataclass(frozen=True)
class PublishGrant:
    """Approval of exactly one publish plan digest, until ``expires_at``."""

    digest: str
    expires_at: float

    def __post_init__(self) -> None:
        validate_digest(self.digest)


@dataclass(frozen=True)
class PublishPlan:
    target: RegistryTarget
    tag: str
    images: tuple[ImagePublication, ...]
    attach: bool = True
    move_tag: bool = False
    signing: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.tag, str) or not TAG.fullmatch(self.tag):
            raise PublishError("publish-invalid", "invalid version tag")
        if self.target.tag is not None:
            raise PublishError(
                "publish-invalid", "give the version with --tag, not in --to"
            )
        keys = [item.key for item in self.images]
        if not keys or len(set(keys)) != len(keys):
            raise PublishError(
                "publish-invalid",
                "every image needs its own repository name (last path segment)",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": PUBLISH_PLAN_REVISION,
            "target": self.target.identity,
            "registry": self.target.registry,
            "tag": self.tag,
            "images": [item.public() for item in self.images],
            "attach": self.attach,
            "move_tag": self.move_tag,
            "signing": dict(self.signing) if self.signing else None,
        }

    @property
    def digest(self) -> str:
        return digest(canonical(self.to_dict()))

    def preview(self) -> dict[str, Any]:
        return {**self.to_dict(), "digest": self.digest}

    @classmethod
    def from_receipt(
        cls,
        receipt: BuildReceipt,
        output_dir: Path,
        to: str,
        tag: str,
        *,
        images: Sequence[str] | None = None,
        attach: bool = True,
        move_tag: bool = False,
        signing: Mapping[str, Any] | None = None,
    ) -> PublishPlan:
        """The plan for ``receipt`` (paths relative to ``output_dir``). No network."""
        try:
            target = RegistryTarget.parse(to)
        except ValueError as error:
            code = getattr(error, "code", "publish-invalid")
            raise PublishError(code, "invalid --to registry target") from None
        grouped = _grouped(receipt, output_dir, attach)
        wanted = list(images) if images else list(grouped)
        unknown = sorted(set(wanted) - set(grouped))
        if unknown:
            raise PublishError("publish-invalid", f"the build has no image {unknown}")
        indexes = receipt.data["outputs"].get("indexes") or {}
        chosen = []
        for name in wanted:
            source_repository, platforms = grouped[name]
            key = source_repository.rsplit("/", 1)[-1]
            index = indexes.get(name) if isinstance(indexes, Mapping) else None
            chosen.append(
                ImagePublication(
                    name,
                    key,
                    f"{target.repository}/{key}",
                    platforms,
                    index.get("digest") if isinstance(index, Mapping) else None,
                )
            )
        return cls(target, tag, tuple(chosen), attach, move_tag, signing)


def _grouped(
    receipt: BuildReceipt, output_dir: Path, attach: bool
) -> dict[str, tuple[str, tuple[PlatformImage, ...]]]:
    """``{image name: (source repository, per-platform images)}`` of a receipt."""
    result: dict[str, tuple[str, list[PlatformImage]]] = {}
    for key, entry in sorted(receipt.images.items()):
        name = key.split("/", 1)[0]
        try:
            platform = entry["platform"]
            config = validate_digest(entry["image_id"])
            ref = str(entry["ref"])
            manifest = entry.get("digest")
            if manifest is not None:
                validate_digest(manifest)
        except (KeyError, TypeError, ValueError):
            raise PublishError("invalid-receipt", "incomplete build receipt") from None
        repository = ref.split("@", 1)[0]
        head, _, last = repository.rpartition("/")
        repository = (head + "/" if head else "") + last.split(":", 1)[0]
        archive = entry.get("archive")
        attestations = []
        for kind in ATTESTATION_TYPES:
            value = entry.get(kind)
            if attach and isinstance(value, Mapping):
                attestations.append(
                    Attestation(
                        kind,
                        _inside(output_dir, value["path"]),
                        validate_digest(value["sha256"]),
                    )
                )
        item = PlatformImage(
            platform,
            config,
            manifest,
            _inside(output_dir, archive) if isinstance(archive, str) else None,
            None if isinstance(archive, str) else config,
            tuple(attestations),
        )
        known = result.setdefault(name, (repository, []))
        if known[0] != repository or any(
            other.platform == platform for other in known[1]
        ):
            raise PublishError("invalid-receipt", "inconsistent build receipt")
        known[1].append(item)
    if not result:
        raise PublishError("invalid-receipt", "the build produced no image")
    return {
        name: (repository, tuple(sorted(items, key=lambda item: item.platform)))
        for name, (repository, items) in result.items()
    }


def _inside(root: Path, relative: Any) -> Path:
    """``root / relative``, refusing a path that leaves ``root``."""
    base = root.absolute()
    path = (base / str(relative)).absolute()
    if not isinstance(relative, str) or ".." in Path(relative).parts:
        raise PublishError("invalid-receipt", "receipt path leaves its directory")
    if base not in path.parents:
        raise PublishError("invalid-receipt", "receipt path leaves its directory")
    return path


# ---------------------------------------------------------------- execution


@dataclass
class Publisher:
    """Runs an approved :class:`PublishPlan` (see the module docstring).

    ``docker`` and ``docker_socket`` are needed only for images still in the
    local engine (a Docker build). ``client_factory`` and ``delivery_factory``
    are seams for tests.
    """

    credentials: RegistryCredentials | None = field(default=None, repr=False)
    ca_file: Path | None = field(default=None, repr=False)
    docker: ToolPin | None = None
    docker_socket: Path | None = field(default=None, repr=False)
    signer: Signer | None = None
    runner: Any = field(default=None, repr=False)
    """Process runner seam for ``docker image save`` (tests)."""
    client_factory: Callable[[RegistryEndpoint], StreamedOciRegistryClient] = field(
        default=StreamedOciRegistryClient, repr=False
    )
    timeout: float = 1800.0

    def publish(
        self,
        plan: PublishPlan,
        grant: PublishGrant,
        *,
        cancel: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Publish every image of ``plan``; return the receipt (never raises for a
        registry failure: ``state`` is ``failed`` with a ``reason``)."""
        if grant.digest != plan.digest:
            raise PublishError("publish-not-approved", "publish digest not approved")
        if grant.expires_at <= time.time():
            raise PublishError("grant-expired", "publish grant expired")
        if (self.signer is None) != (plan.signing is None) or (
            self.signer is not None and self.signer.public() != plan.signing
        ):
            raise PublishError("publish-not-approved", "signing differs from the plan")
        for image in plan.images:
            for item in image.platforms:
                if item.archive is None and (
                    self.docker is None or self.docker_socket is None
                ):
                    raise PublishError("docker-tool-required")
                for attestation in item.attestations:
                    _read_attestation(attestation)
        started, started_at = time.monotonic(), _now()
        deadline = min(grant.expires_at, time.time() + self.timeout)
        receipt: dict[str, Any] = {
            "revision": PUBLISH_RECEIPT_REVISION,
            "digest": plan.digest,
            "registry": plan.target.registry,
            "tag": plan.tag,
            "images": {},
            "state": "published",
            "reason": None,
            "started_at": started_at,
        }
        endpoint = plan.target.endpoint(
            credentials=self.credentials, ca_file=self.ca_file, timeout=120.0
        )
        try:
            for image in plan.images:
                receipt["images"][image.name] = self._image(
                    plan, image, endpoint, deadline, cancel
                )
        except PublishError as error:
            receipt.update(state="failed", reason=error.code)
        except RegistryError as error:
            receipt.update(state="failed", reason=error.reason)
        receipt["values"] = values(receipt) if receipt["state"] == "published" else None
        receipt["finished_at"] = _now()
        receipt["seconds"] = round(time.monotonic() - started, 3)
        return receipt

    # -- one image ----------------------------------------------------------

    def _image(
        self,
        plan: PublishPlan,
        image: ImagePublication,
        endpoint: RegistryEndpoint,
        deadline: float,
        cancel: threading.Event | None,
    ) -> dict[str, Any]:
        target = plan.target
        repository = image.repository
        client = self.client_factory(endpoint)
        client.authenticate(repository)
        previous = client.manifest_digest(repository, plan.tag)
        entry: dict[str, Any] = {
            "repository": f"{target.registry}/{repository}",
            "key": image.key,
            "tag": plan.tag,
            "digest": None,
            "platforms": {},
            "blobs": {
                "uploaded": 0,
                "skipped": 0,
                "uploaded_bytes": 0,
                "skipped_bytes": 0,
            },
            "attestations": [],
            "signatures": [],
            "previous": None,
            "result": None,
        }
        if (
            previous is not None
            and image.index_digest is not None
            and previous != image.index_digest
            and not plan.move_tag
        ):
            # Known before any push (host builds): refuse before sending anything.
            raise PublishError("publish-tag-exists", "the tag names another image")
        entries = []
        for item in image.platforms:
            pushed = self._platform(target, repository, item, deadline, cancel)
            for key in entry["blobs"]:
                entry["blobs"][key] += pushed["blobs"][key]
            manifest, media = client.get_manifest(repository, pushed["digest"])
            if (
                digest(manifest) != pushed["digest"]
                or media.split(";")[0].strip() not in MANIFEST_TYPES
                or manifest_config_digest(manifest) != item.config_digest
            ):
                raise PublishError("publish-verification-failed")
            entries.append(
                IndexEntry(item.platform, pushed["digest"], len(manifest), media)
            )
            entry["platforms"][item.platform] = {
                "digest": pushed["digest"],
                "config_digest": item.config_digest,
                "size": len(manifest),
                "media_type": media,
                "result": pushed["result"],
            }
        body = image_index(entries)
        index = digest(body)
        if image.index_digest is not None and index != image.index_digest:
            raise PublishError(
                "publish-verification-failed", "index differs from the build's"
            )
        if previous is not None and previous != index:
            if not plan.move_tag:
                raise PublishError("publish-tag-exists", "the tag names another image")
            entry["previous"] = previous
        entry["digest"] = index
        present = client.manifest_digest(repository, index) == index
        if not present:
            client.push_manifest(repository, index, body, OCI_INDEX)
        signed: list[str] = [index, *(item.digest for item in entries)]
        if plan.attach:
            for item, platform in zip(image.platforms, entries, strict=True):
                for attestation in item.attestations:
                    attached = attach(
                        client,
                        repository,
                        platform,
                        attestation.kind,
                        _read_attestation(attestation),
                        f"{image.name}.{attestation.kind}.json",
                    )
                    entry["attestations"].append(
                        {"platform": item.platform, **attached}
                    )
                    signed.append(attached["digest"])
        if self.signer is not None:
            entry["signatures"] = self.signer.sign(target, repository, signed)
        if previous != index:
            client.push_manifest(repository, plan.tag, body, OCI_INDEX)
        changed = previous != index or not present
        changed = changed or any(
            item["result"] == "pushed" for item in entry["platforms"].values()
        )
        entry["result"] = "pushed" if changed else "already-present"
        self._verify(client, repository, plan.tag, index, body, entry)
        return entry

    def _platform(
        self,
        target: RegistryTarget,
        repository: str,
        item: PlatformImage,
        deadline: float,
        cancel: threading.Event | None,
    ) -> dict[str, Any]:
        """Push one platform's manifest by digest (only the missing blobs)."""
        one = RegistryTarget(target.host, target.port, repository, None, target.tls)
        docker = self.docker if item.archive is None else None
        delivery = RegistryDelivery(
            docker=docker,
            docker_socket=self.docker_socket if docker is not None else None,
            credentials=self.credentials,
            ca_file=self.ca_file,
            client_factory=self.client_factory,
            **({"runner": self.runner} if self.runner is not None else {}),
        )
        remaining = max(1.0, min(3600.0, deadline - time.time()))
        result = delivery.deliver(
            item.source,
            one,
            DeliveryGrant(item.config_digest, one.identity, deadline),
            limits=ProcessLimits(remaining, 1_048_576),
            cancel=cancel,
        )
        if result.get("state") != "succeeded":
            reason = result.get("reason")
            if not reason:
                raise PublishError("publish-failed")
            raise PublishError(str(reason))
        manifest = result["image"]["manifest_digest"]
        if item.manifest_digest is not None and manifest != item.manifest_digest:
            raise PublishError("publish-verification-failed")
        blobs = result["blobs"]
        return {
            "digest": manifest,
            "result": result["result"],
            "blobs": {
                key: blobs[key]
                for key in ("uploaded", "skipped", "uploaded_bytes", "skipped_bytes")
            },
        }

    @staticmethod
    def _verify(
        client: StreamedOciRegistryClient,
        repository: str,
        tag: str,
        index: str,
        body: bytes,
        entry: Mapping[str, Any],
    ) -> None:
        """Read back: the tag names the index, the index and each child exist."""
        if client.manifest_digest(repository, tag) != index:
            raise PublishError("publish-verification-failed")
        served, media = client.get_manifest(repository, index)
        if served != body or media.split(";")[0].strip() not in INDEX_TYPES:
            raise PublishError("publish-verification-failed")
        for platform in entry["platforms"].values():
            if client.manifest_digest(repository, platform["digest"]) is None:
                raise PublishError("publish-verification-failed")
        for attached in entry["attestations"]:
            found = referrers(client, repository, attached["subject"])
            if attached["digest"] not in {item.get("digest") for item in found}:
                raise PublishError("publish-verification-failed")


def _read_attestation(attestation: Attestation) -> bytes:
    """The attestation's bytes, refused when they differ from the receipt."""
    try:
        with attestation.path.open("rb") as stream:
            body = stream.read(_MAX_ATTESTATION + 1)
    except OSError:
        raise PublishError("publish-input-changed") from None
    if len(body) > _MAX_ATTESTATION or digest(body) != attestation.sha256:
        raise PublishError("publish-input-changed")
    return body


# ---------------------------------------------------------------- referrers


def _fallback_tag(value: str) -> str:
    return value.replace(":", "-")


def attach(
    client: StreamedOciRegistryClient,
    repository: str,
    subject: IndexEntry,
    kind: str,
    body: bytes,
    title: str,
) -> dict[str, Any]:
    """Push ``body`` as an OCI 1.1 referrer of ``subject``; its descriptor.

    Idempotent: the same body gives the same artifact manifest digest, and the
    fallback tag index is only extended with a missing entry.
    """
    artifact_type = ATTESTATION_TYPES[kind]
    blob = digest(body)
    empty = digest(EMPTY_CONFIG)
    for value, data in ((blob, body), (empty, EMPTY_CONFIG)):
        if client.blob_size(repository, value) is None:
            import io

            client.push_blob(repository, value, len(data), io.BytesIO(data))
    annotations = {"org.opencontainers.image.title": title}
    manifest = canonical(
        {
            "schemaVersion": 2,
            "mediaType": OCI_MANIFEST,
            "artifactType": artifact_type,
            "config": {
                "mediaType": EMPTY_MEDIA,
                "digest": empty,
                "size": len(EMPTY_CONFIG),
                "data": "e30=",
            },
            "layers": [
                {
                    "mediaType": artifact_type,
                    "digest": blob,
                    "size": len(body),
                    "annotations": annotations,
                }
            ],
            "subject": {
                "mediaType": subject.media_type,
                "digest": subject.digest,
                "size": subject.size,
            },
            "annotations": annotations,
        }
    )
    value = digest(manifest)
    if client.manifest_digest(repository, value) == value:
        # Already attached: only make sure a registry without the referrers
        # API still lists it under the fallback tag.
        indexed = client.referrers(repository, subject.digest) is not None
    else:
        value, indexed = client.put_manifest(repository, value, manifest, OCI_MANIFEST)
    descriptor = {
        "mediaType": OCI_MANIFEST,
        "digest": value,
        "size": len(manifest),
        "artifactType": artifact_type,
        "annotations": annotations,
    }
    if not indexed:
        _extend_fallback(client, repository, subject.digest, descriptor)
    return {
        "kind": kind,
        "digest": value,
        "artifact_type": artifact_type,
        "subject": subject.digest,
        "blob": blob,
        "size": len(body),
        "fallback_tag": None if indexed else _fallback_tag(subject.digest),
    }


def _extend_fallback(
    client: StreamedOciRegistryClient,
    repository: str,
    subject: str,
    descriptor: Mapping[str, Any],
) -> None:
    tag = _fallback_tag(subject)
    try:
        body, _ = client.get_manifest(repository, tag)
        current = json.loads(body)
        manifests = list(current.get("manifests") or [])
    except RegistryError as error:
        if error.reason != "manifest-not-found":
            raise
        manifests = []
    except (ValueError, AttributeError):
        manifests = []
    if any(
        isinstance(item, Mapping) and item.get("digest") == descriptor["digest"]
        for item in manifests
    ):
        return
    manifests.append(dict(descriptor))
    index = canonical(
        {"schemaVersion": 2, "mediaType": OCI_INDEX, "manifests": manifests}
    )
    client.push_manifest(repository, tag, index, OCI_INDEX)


def referrers(
    client: StreamedOciRegistryClient, repository: str, subject: str
) -> list[dict[str, Any]]:
    """The referrers of ``subject``: the referrers API, else the fallback tag."""
    found = client.referrers(repository, subject)
    if found is not None:
        return [dict(item) for item in found]
    try:
        body, _ = client.get_manifest(repository, _fallback_tag(subject))
    except RegistryError as error:
        if error.reason == "manifest-not-found":
            return []
        raise
    try:
        manifests = json.loads(body).get("manifests") or []
    except (ValueError, AttributeError):
        return []
    return [dict(item) for item in manifests if isinstance(item, Mapping)]


# ---------------------------------------------------------------- values


def values(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """The ``images:`` fragment of Helm values for a published receipt.

    ``images.<key>.{repository, tag, digest}``: ``key`` is the last path
    segment of the image repository, as ``piceli chart`` names its values;
    ``digest`` is the image index, so each node pulls its own platform.
    """
    return {
        "images": {
            entry["key"]: {
                "repository": entry["repository"],
                "tag": entry["tag"],
                "digest": entry["digest"],
            }
            for entry in receipt["images"].values()
        }
    }
