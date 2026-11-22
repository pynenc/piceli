"""Copy an image by digest between registries, with its referrers (0.74.0).

``piceli artifacts copy --from oci://host/repo@sha256:… --to oci://host/repo:tag``
promotes an image a pipeline already built and tested: the release runs the
same bytes, it builds nothing. Every digest is kept:

1. **Plan** (:class:`CopyPlan`, no network): the source repository and
   digest, the target repository and optional tag, whether referrers are
   copied and whether the tag may move, and a digest over all of it.
   Nothing is sent without an approval of that digest.
2. **The image** (:func:`piceli.artifacts.mirror.mirror_image`, every
   platform): the index or manifest by digest, each platform's manifest,
   and each missing blob, streamed and hashed. Blobs the target has are
   skipped.
3. **The referrers** (OCI 1.1: SBOM, provenance, signatures) of the index,
   of each platform manifest and, in turn, of each referrer (a signature of
   an attestation), up to :data:`MAX_DEPTH` levels. They are read from the
   referrers API, else from the fallback tag ``sha256-<digest>``, and
   written the same way: a target without the referrers API gets the
   fallback tag.
4. **The tag**, last, so a client never pulls a tag whose referrers are not
   there yet. A tag that names another digest is refused
   (``copy-tag-exists``) before anything is pushed, unless ``move_tag``.
5. **Read back**: the tag names the digest, the bytes hash to it, each
   platform manifest is there and each referrer is listed.

``skopeo copy --all --preserve-digests`` copies 2 but not 3.

The receipt (``piceli.copy-receipt.v1``) gives the digest, its media type,
each platform's manifest, the blobs uploaded and skipped and each referrer.
Credentials are never printed. Importing this module runs nothing.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from piceli.artifacts.image_manifest import INDEX_TYPES, MANIFEST_TYPES
from piceli.artifacts.mirror import (
    MirrorError,
    MirrorSource,
    _Copy,
    _descriptor,
    _media_type,
    _sha,
    mirror_image,
)
from piceli.artifacts.plan import canonical, digest, validate_digest
from piceli.artifacts.publish import _extend_fallback, referrers
from piceli.artifacts.registry import (
    RegistryCredentials,
    RegistryEndpoint,
    RegistryError,
    RegistryTarget,
    StreamedOciRegistryClient,
)

COPY_PLAN_REVISION = "piceli.copy-plan.v1"
COPY_RECEIPT_REVISION = "piceli.copy-receipt.v1"
#: Levels of referrers copied: the image's, theirs (a signature of an
#: SBOM), and one more.
MAX_DEPTH = 3
#: Most referrers one copy takes (every level together).
MAX_REFERRERS = 256
_FROM = re.compile(r"(?P<base>[^@?#]+)@(?P<digest>sha256:[0-9a-f]{64})(?P<query>\?.*)?")


class CopyError(ValueError):
    """A copy input or step failed. ``code`` is a registered error code."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class CopySource:
    """Where the image is: a repository and the digest pinned in it."""

    repository: RegistryTarget
    digest: str

    @classmethod
    def parse(cls, text: str) -> CopySource:
        """``[oci://]host[:port]/repository@sha256:<hex>[?tls=false]``.

        :raises CopyError: ``copy-invalid`` (a tag instead of a digest, a
            malformed reference).
        """
        if not isinstance(text, str) or len(text) > 1024:
            raise CopyError("copy-invalid", "invalid --from reference")
        value = text if text.startswith("oci://") else f"oci://{text}"
        found = _FROM.fullmatch(value)
        if found is None:
            raise CopyError(
                "copy-invalid",
                "--from must be pinned by digest: "
                "oci://host[:port]/repository@sha256:<64 hex>",
            )
        try:
            repository = RegistryTarget.parse(found["base"] + (found["query"] or ""))
        except ValueError:
            raise CopyError("copy-invalid", "invalid --from registry") from None
        if repository.tag is not None:
            raise CopyError("copy-invalid", "--from takes a digest, not a tag")
        return cls(repository, found["digest"])

    @property
    def identity(self) -> str:
        tls = "true" if self.repository.tls else "false"
        return (
            f"oci://{self.repository.registry}/{self.repository.repository}"
            f"@{self.digest}?tls={tls}"
        )


@dataclass(frozen=True)
class CopyGrant:
    """Approval of exactly one copy plan digest, until ``expires_at``."""

    digest: str
    expires_at: float

    def __post_init__(self) -> None:
        validate_digest(self.digest)


@dataclass(frozen=True)
class CopyPlan:
    """What a copy does (see the module docstring); its digest is approved."""

    source: CopySource
    target: RegistryTarget
    referrers: bool = True
    move_tag: bool = False

    @classmethod
    def parse(
        cls, source: str, to: str, *, referrers: bool = True, move_tag: bool = False
    ) -> CopyPlan:
        """The plan for ``--from`` and ``--to``. No network.

        :raises CopyError: ``copy-invalid``.
        """
        found = CopySource.parse(source)
        try:
            target = RegistryTarget.parse(to)
        except ValueError:
            raise CopyError(
                "copy-invalid", "--to must be oci://host[:port]/repository[:tag]"
            ) from None
        if move_tag and target.tag is None:
            raise CopyError("copy-invalid", "--move-tag needs a tag in --to")
        if (
            target.registry == found.repository.registry
            and target.repository == found.repository.repository
            and target.tag is None
        ):
            raise CopyError("copy-invalid", "--to is the --from repository")
        return cls(found, target, referrers, move_tag)

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": COPY_PLAN_REVISION,
            "source": self.source.identity,
            "digest": self.source.digest,
            "target": self.target.identity,
            "registry": self.target.registry,
            "repository": self.target.repository,
            "tag": self.target.tag,
            "referrers": self.referrers,
            "move_tag": self.move_tag,
        }

    @property
    def digest(self) -> str:
        return digest(canonical(self.to_dict()))

    def preview(self) -> dict[str, Any]:
        return {**self.to_dict(), "plan_digest": self.digest}


ClientFactory = Callable[..., StreamedOciRegistryClient]


@dataclass
class Copier:
    """Runs an approved :class:`CopyPlan`. ``client_factory`` is a test seam."""

    source_credentials: RegistryCredentials | None = field(default=None, repr=False)
    credentials: RegistryCredentials | None = field(default=None, repr=False)
    source_ca_file: Path | None = field(default=None, repr=False)
    ca_file: Path | None = field(default=None, repr=False)
    client_factory: ClientFactory = field(default=StreamedOciRegistryClient, repr=False)
    timeout: float = 1800.0

    def copy(
        self,
        plan: CopyPlan,
        grant: CopyGrant,
        *,
        cancel: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Copy the image of ``plan``; return the receipt (a registry failure
        is ``state: failed`` with a ``reason``, never raised).

        :raises CopyError: ``copy-not-approved``, ``grant-expired``.
        """
        if grant.digest != plan.digest:
            raise CopyError("copy-not-approved", "copy plan digest not approved")
        if grant.expires_at <= time.time():
            raise CopyError("grant-expired", "copy grant expired")
        started, clock = _now(), time.monotonic()
        target = plan.target
        receipt: dict[str, Any] = {
            "revision": COPY_RECEIPT_REVISION,
            "plan_digest": plan.digest,
            "source": {
                "registry": plan.source.repository.registry,
                "repository": plan.source.repository.repository,
                "digest": plan.source.digest,
            },
            "target": {
                "registry": target.registry,
                "repository": target.repository,
                "tag": target.tag,
            },
            "digest": plan.source.digest,
            "media_type": None,
            "platforms": {},
            "blobs": {},
            "referrers": [],
            "previous": None,
            "pull_ref": None,
            "result": None,
            "state": "copied",
            "reason": None,
            "started_at": started,
        }
        try:
            self._copy(plan, receipt, time.time() + self.timeout, cancel)
        except CopyError as error:
            receipt.update(state="failed", reason=error.code)
        except MirrorError as error:
            receipt.update(state="failed", reason=error.code)
        except RegistryError as error:
            receipt.update(state="failed", reason=error.reason)
        receipt["finished_at"] = _now()
        receipt["seconds"] = round(time.monotonic() - clock, 3)
        return receipt

    def _client(
        self,
        repository: RegistryTarget,
        credentials: RegistryCredentials | None,
        ca_file: Path | None,
        actions: str,
    ) -> StreamedOciRegistryClient:
        endpoint: RegistryEndpoint = repository.endpoint(
            credentials=credentials, ca_file=ca_file, timeout=120.0
        )
        return self.client_factory(endpoint, actions=actions)

    def _copy(
        self,
        plan: CopyPlan,
        receipt: dict[str, Any],
        deadline: float,
        cancel: threading.Event | None,
    ) -> None:
        source_repo = plan.source.repository
        target = plan.target
        wanted = plan.source.digest
        source = self._client(
            source_repo, self.source_credentials, self.source_ca_file, "pull"
        )
        sink = self._client(target, self.credentials, self.ca_file, "pull,push")
        sink.authenticate(target.repository)
        previous = (
            sink.manifest_digest(target.repository, target.tag)
            if target.tag is not None
            else None
        )
        if previous is not None and previous != wanted:
            if not plan.move_tag:
                raise CopyError("copy-tag-exists", "the tag names another image")
            receipt["previous"] = previous
        # 2. The image, every platform (the mirror's copy, platform None).
        mirrored = mirror_image(
            MirrorSource(source_repo.registry, source_repo.repository, wanted),
            source,
            sink,
            target.repository,
            platform=None,
            target_registry=target.registry,
            node_registry=target.registry,
            max_seconds=max(1.0, deadline - time.time()),
            cancel=cancel,
        )
        if mirrored.get("state") != "succeeded":
            raise CopyError(str(mirrored.get("reason") or "copy-failed"))
        blobs = dict(mirrored["blobs"])
        blobs.pop("items", None)
        receipt["blobs"] = blobs
        body, header = sink.get_manifest(target.repository, wanted)
        if _sha(body) != wanted:
            raise CopyError("copy-verification-failed")
        media, document = _media_type(body, header)
        receipt["media_type"] = media
        children = _children(document) if media in INDEX_TYPES else {}
        receipt["platforms"] = dict(sorted(children.items()))
        copy = _Copy(
            source,
            sink,
            source_repo.repository,
            target.repository,
            deadline,
            cancel,
            {
                "uploaded": 0,
                "skipped": 0,
                "uploaded_bytes": 0,
                "skipped_bytes": 0,
                "items": [],
            },
        )
        if media not in INDEX_TYPES:
            config, size, _ = _descriptor(document.get("config"))
            seen = copy.config_platform(config, size)
            if seen:
                receipt["platforms"] = {seen: wanted}
        # 3. The referrers, level by level.
        if plan.referrers:
            subjects = list(dict.fromkeys([wanted, *children.values()]))
            receipt["referrers"] = self._referrers(copy, subjects)
            for key in ("uploaded", "skipped", "uploaded_bytes", "skipped_bytes"):
                receipt["blobs"][key] = receipt["blobs"].get(key, 0) + copy.blobs[key]
        # 4. The tag, last.
        if target.tag is not None and previous != wanted:
            sink.push_manifest(target.repository, target.tag, body, media)
        changed = (
            mirrored.get("result") == "mirrored"
            or previous != wanted
            or any(item["result"] == "copied" for item in receipt["referrers"])
        )
        receipt["result"] = "copied" if changed else "already-present"
        self._verify(sink, plan, body, receipt)
        receipt["pull_ref"] = f"{target.registry}/{target.repository}@{wanted}"

    def _referrers(self, copy: _Copy, subjects: list[str]) -> list[dict[str, Any]]:
        """Copy the referrers of ``subjects`` and theirs; one entry each."""
        found: list[dict[str, Any]] = []
        seen: set[str] = set(subjects)
        level = list(subjects)
        for _ in range(MAX_DEPTH):
            following: list[str] = []
            for subject in level:
                for descriptor in referrers(
                    copy.source, copy.source_repository, subject
                ):
                    value = descriptor.get("digest")
                    if not isinstance(value, str) or value in seen:
                        continue
                    validate_digest(value)
                    if len(found) >= MAX_REFERRERS:
                        raise CopyError(
                            "copy-failed", f"more than {MAX_REFERRERS} referrers"
                        )
                    seen.add(value)
                    found.append(self._referrer(copy, subject, value, descriptor))
                    following.append(value)
            if not following:
                break
            level = following
        return found

    @staticmethod
    def _referrer(
        copy: _Copy, subject: str, value: str, descriptor: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Copy one referrer manifest (its blobs, then it) and index it."""
        body, media, document = copy.manifest(value)
        named = document.get("subject")
        if media not in MANIFEST_TYPES or not (
            isinstance(named, Mapping) and named.get("digest") == subject
        ):
            raise MirrorError("mirror-manifest-invalid")
        repository = copy.target_repository
        present = copy.target.manifest_digest(repository, value) == value
        if present:
            indexed = copy.target.referrers(repository, subject) is not None
        else:
            config_digest, config_size, config_media = _descriptor(
                document.get("config")
            )
            copy.blob(config_digest, config_size, config_media)
            for layer in document.get("layers") or ():
                layer_digest, layer_size, layer_media = _descriptor(layer)
                copy.blob(layer_digest, layer_size, layer_media)
            copy.check()
            pushed, indexed = copy.target.put_manifest(repository, value, body, media)
            if pushed != value:
                raise MirrorError("mirror-digest-mismatch")
        artifact_type = document.get("artifactType") or descriptor.get("artifactType")
        if not indexed:
            entry: dict[str, Any] = {
                "mediaType": media,
                "digest": value,
                "size": len(body),
            }
            if artifact_type:
                entry["artifactType"] = artifact_type
            annotations = descriptor.get("annotations")
            if isinstance(annotations, Mapping) and annotations:
                entry["annotations"] = dict(annotations)
            _extend_fallback(copy.target, repository, subject, entry)
        return {
            "subject": subject,
            "digest": value,
            "artifact_type": str(artifact_type) if artifact_type else None,
            "result": "already-present" if present else "copied",
            "fallback_tag": None if indexed else subject.replace(":", "-"),
        }

    @staticmethod
    def _verify(
        sink: StreamedOciRegistryClient,
        plan: CopyPlan,
        body: bytes,
        receipt: Mapping[str, Any],
    ) -> None:
        """Read back what the target serves (module docstring, step 5)."""
        target, wanted = plan.target, plan.source.digest
        repository = target.repository
        if target.tag is not None and (
            sink.manifest_digest(repository, target.tag) != wanted
        ):
            raise CopyError("copy-verification-failed")
        served, _ = sink.get_manifest(repository, wanted)
        if served != body:
            raise CopyError("copy-verification-failed")
        for child in receipt["platforms"].values():
            if sink.manifest_digest(repository, child) != child:
                raise CopyError("copy-verification-failed")
        listed: dict[str, set[str]] = {}
        for item in receipt["referrers"]:
            subject = item["subject"]
            if subject not in listed:
                listed[subject] = {
                    str(entry.get("digest"))
                    for entry in referrers(sink, repository, subject)
                }
            if item["digest"] not in listed[subject]:
                raise CopyError("copy-verification-failed")


def _children(document: Mapping[str, Any]) -> dict[str, str]:
    """``{platform: manifest digest}`` of an index's image manifests."""
    found: dict[str, str] = {}
    for position, entry in enumerate(document.get("manifests") or ()):
        if not isinstance(entry, Mapping):
            continue
        described = entry.get("platform") or {}
        name = "/".join(
            str(described[key])
            for key in ("os", "architecture", "variant")
            if isinstance(described, Mapping) and described.get(key)
        )
        if not name or name in found:
            name = f"{name or 'unknown'}#{position}"
        found[name] = str(entry.get("digest"))
    return found


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def summary(receipt: Mapping[str, Any]) -> str:
    """One line for stderr."""
    target = receipt["target"]
    tag = f":{target['tag']}" if target.get("tag") else ""
    platforms = ", ".join(sorted(receipt.get("platforms") or {})) or "one platform"
    return (
        f"copied {target['registry']}/{target['repository']}{tag}@{receipt['digest']} "
        f"({platforms}; {len(receipt.get('referrers') or ())} referrer(s))"
    )


__all__ = [
    "COPY_PLAN_REVISION",
    "COPY_RECEIPT_REVISION",
    "Copier",
    "CopyError",
    "CopyGrant",
    "CopyPlan",
    "CopySource",
    "summary",
]
