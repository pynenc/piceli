"""Typed ``release.toml`` specification for the ``piceli release`` CLI.

The spec is declarative and validated strictly: unknown keys are rejected
everywhere except the free-form ``[values]`` table, which is handed to the
composition function unchanged. Relative paths resolve from the spec file's
directory. Nothing here contacts a cluster; importing the module has no side
effects (the composition module is imported only by :func:`load_composition`).
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import re
import sys
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from piceli.checks.model import Check, unique_names
from piceli.k8s.ops.plan import DeploymentComposition
from piceli.k8s.ops.provider_factory import KubeconfigTarget, NodeExpectation
from piceli.k8s.ops.secret_versions import SecretVersionRef
from piceli.k8s.release_secret_spec import (  # noqa: F401 (re-exported)
    GeneratorSpec,
    ImportSecretSpec,
    RandomSecretSpec,
    SecretSpec,
    StaticSecretSpec,
    TemplateSecretSpec,
    TlsCaSpec,
    TlsSelfSignedSpec,
    check_secrets,
)

BUILD_RECEIPT_REVISION = "piceli.build-receipt.v1"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_IMAGE_NAME = re.compile(r"[a-z][a-z0-9_-]{0,62}")
_SECRET_NAME = re.compile(r"[a-z][a-z0-9_-]{0,62}")
_RELEASE_PREFIX = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,40}[a-z0-9])?")
_KIND = re.compile(r"(?:[a-z0-9.-]+/)?[a-z][a-z0-9]*/[A-Za-z][A-Za-z0-9]*")


class ReleaseSpecError(ValueError):
    """The release spec, build receipt or composition entry point is invalid."""


_ADOPT_ENTRY = re.compile(
    r"(?:(?P<api>(?:[a-z0-9.-]+/)?v[0-9][a-z0-9]*)/)?"
    r"(?P<kind>[A-Z][A-Za-z0-9]*)/(?P<name>[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?)"
)


def parse_adopt_entry(
    value: str, *, what: str = "adopt"
) -> tuple[str | None, str, str]:
    """``Kind/name`` or ``apiVersion/Kind/name`` → (api_version, kind, name)."""
    match = _ADOPT_ENTRY.fullmatch(value) if isinstance(value, str) else None
    if match is None or len(match["name"]) > 253:
        raise ReleaseSpecError(
            f"{what} entry must be 'Kind/name' or 'apiVersion/Kind/name', got {value!r}"
        )
    return match["api"], match["kind"], match["name"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NodeSpec(_Strict):
    name: str
    uid: str | None = None


class TargetSpec(_Strict):
    kubeconfig: Path
    context: str = Field(min_length=1)
    namespace: str = Field(min_length=1, max_length=63)
    cluster_uid: str | None = None
    namespace_uid: str | None = None
    transport: Literal["https", "loopback-http"] = "https"
    request_seconds: float = Field(default=10.0, gt=0, le=60)
    nodes: dict[str, NodeSpec] = Field(default_factory=dict)


class ReleaseSettings(_Strict):
    name: str
    owner: str = Field(min_length=1, max_length=128)
    field_manager: str = Field(min_length=1, max_length=128)
    composition: str = Field(min_length=3)
    state_dir: Path
    catalog: Path | None = None
    journal: Path | None = None
    secret_store: Path | None = None
    approval_window_seconds: int = Field(default=900, ge=30, le=86400)
    prune: bool = False
    inherited_owners: tuple[str, ...] = ()
    # Existing objects this release may adopt: "Kind/name" or
    # "apiVersion/Kind/name", in the target namespace. See docs/release_cli.md.
    adopt: tuple[str, ...] = ()
    # Existing unmanaged objects this release may delete and recreate (with a
    # backup first), same entry syntax. Never retained or managed objects.
    replace: tuple[str, ...] = ()
    # After a release's checks fail, re-apply the previous ready release
    # automatically (journaled like any rollback). See docs/checks.md.
    rollback_on_failed_checks: bool = False

    @field_validator("adopt")
    @classmethod
    def _adopt(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            parse_adopt_entry(item)
        return value

    @field_validator("replace")
    @classmethod
    def _replace(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            parse_adopt_entry(item, what="replace")
        return value

    @field_validator("name")
    @classmethod
    def _prefix(cls, value: str) -> str:
        if not _RELEASE_PREFIX.fullmatch(value):
            raise ValueError("release name must be a DNS label of at most 42 chars")
        return value

    @field_validator("composition")
    @classmethod
    def _entry(cls, value: str) -> str:
        module, _, function = value.rpartition(":")
        if not module or not function.isidentifier():
            raise ValueError(
                "composition must be 'module:function' or 'file.py:function'"
            )
        return value


class ExecutionSpec(_Strict):
    max_actions: int = Field(default=256, gt=0, le=4096)
    max_seconds: float = Field(default=600, gt=0, le=3600)
    readiness_seconds: float = Field(default=300, gt=0, le=3600)
    poll_seconds: float = Field(default=1.0, gt=0, le=60)
    max_polls: int = Field(default=1000, gt=0, le=10000)


class DiscoverySpec(_Strict):
    kinds: tuple[str, ...] = ()
    max_seconds: float = Field(default=30, gt=0, le=600)
    call_seconds: float = Field(default=5, gt=0, le=60)

    @field_validator("kinds")
    @classmethod
    def _kinds(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            if not _KIND.fullmatch(item):
                raise ValueError(f"kind must be 'apiVersion/Kind', got {item!r}")
        return value


class ImageSpec(_Strict):
    """A pinned image: ``digest`` (+ optional ``ref``), or a delivery ``receipt``.

    ``receipt`` points at a receipt from ``piceli artifacts deliver``. For
    ``piceli.registry-delivery.v1`` its ``pull_ref`` (the registry manifest
    digest as seen from the node) becomes the image reference, and ``digest``
    pins the expected manifest digest. For ``piceli.node-delivery.v1`` its node
    reference (a content tag of the config digest) becomes the reference, and
    ``digest`` pins the expected config digest.
    """

    ref: str | None = None
    digest: str | None = None
    receipt: Path | None = None

    @field_validator("digest")
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is not None and not _DIGEST.fullmatch(value):
            raise ValueError("image digest must be sha256:<64 hex>")
        return value

    @model_validator(mode="after")
    def _source(self) -> ImageSpec:
        if self.receipt is None and self.digest is None:
            raise ValueError("declare digest or receipt")
        if self.receipt is not None and self.ref is not None:
            raise ValueError("ref comes from the receipt; do not declare both")
        return self


class ReleaseSpecModel(_Strict):
    target: TargetSpec
    release: ReleaseSettings
    execution: ExecutionSpec = ExecutionSpec()
    discovery: DiscoverySpec = DiscoverySpec()
    images: dict[str, str | ImageSpec] = Field(default_factory=dict)
    # one receipt, or a list of build and delivery receipts (merged)
    images_from: Path | tuple[Path, ...] | None = None
    secrets: dict[str, SecretSpec] = Field(default_factory=dict)
    values: dict[str, Any] = Field(default_factory=dict)
    # [[checks]]: post-deploy checks run after readiness (piceli.checks).
    checks: tuple[Check, ...] = ()

    @model_validator(mode="after")
    def _names(self) -> ReleaseSpecModel:
        unique_names(self.checks)
        for name in self.images:
            if not _IMAGE_NAME.fullmatch(name):
                raise ValueError(f"invalid image name {name!r}")
        for name in self.secrets:
            if not _SECRET_NAME.fullmatch(name):
                raise ValueError(f"invalid secret name {name!r}")
        if not self.images and self.images_from is None:
            raise ValueError("declare [images] or images_from")
        if self.images_from == ():
            raise ValueError("images_from lists no receipt")
        return self


REGISTRY_DELIVERY_SCHEMA = "piceli.registry-delivery.v1"
NODE_DELIVERY_SCHEMA = "piceli.node-delivery.v1"
DELIVERY_RECEIPT_SCHEMA = REGISTRY_DELIVERY_SCHEMA  # the 0.2.0 name
DELIVERY_SCHEMAS = (REGISTRY_DELIVERY_SCHEMA, NODE_DELIVERY_SCHEMA)
_CONTENT_TAG = re.compile(r"sha256-([0-9a-f]{12,64})")


class ImageHandoffError(ReleaseSpecError):
    """An image cannot be pinned immutably. ``code`` is one fixed word.

    Codes: ``image-not-immutable``, ``receipt-invalid``,
    ``delivery-not-succeeded``, ``image-digest-mismatch``,
    ``image-declared-twice`` and ``receipt-unmatched``.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def content_tag(config_digest: str, length: int = 12) -> str:
    """The node-import tag derived from a config digest: ``sha256-<hex prefix>``.

    Import an image into a node under ``repository:<content tag>`` (``piceli
    artifacts deliver --ref``) so that the node-side name can only ever point
    at this configuration.
    """
    if not _DIGEST.fullmatch(config_digest) or not 12 <= length <= 64:
        raise ValueError("content_tag needs sha256:<64 hex> and 12..64 characters")
    return "sha256-" + config_digest[7 : 7 + length]


def _is_content_tag(tag: str | None, config_digest: str | None) -> bool:
    match = _CONTENT_TAG.fullmatch(tag) if tag else None
    return bool(
        match
        and config_digest
        and _DIGEST.fullmatch(config_digest)
        and config_digest[7:].startswith(match[1])
    )


@dataclass(frozen=True)
class ImageRef:
    """One pinned image as seen by the composition function.

    ``identity`` is the digest recorded in the release: the registry manifest
    digest when known, otherwise the config digest (``image_id``). ``source``
    names the document the image came from (a receipt schema, or ``None`` for
    ``[images]``).
    """

    name: str
    identity: str
    repository: str | None = None
    tag: str | None = None
    digest: str | None = None
    image_id: str | None = None
    platform: str | None = None
    ref: str | None = None
    source: str | None = None

    @property
    def immutable(self) -> bool:
        """Whether :attr:`reference` names exactly this image and nothing else."""
        if self.repository and self.digest:
            return True
        return self.source == NODE_DELIVERY_SCHEMA and _is_content_tag(
            self.tag, self.image_id
        )

    @property
    def reference(self) -> str:
        """The pull reference, always immutable.

        ``repository@digest`` when a registry digest is known; otherwise the
        node-import content tag proven by a ``piceli.node-delivery.v1`` receipt.
        A movable tag is refused with ``image-not-immutable``.
        """
        if self.repository and self.digest:
            return f"{self.repository}@{self.digest}"
        if self.immutable and self.ref:
            return self.ref
        if self.ref:
            hint = (
                f"{self.repository}:{content_tag(self.image_id)}"
                if self.repository
                and self.image_id
                and _DIGEST.fullmatch(self.image_id)
                else "<repository>:sha256-<12 hex of the config digest>"
            )
            raise ImageHandoffError(
                "image-not-immutable",
                f"image {self.name!r} has no registry digest and {self.ref!r} is "
                "a tag another build can move. Deliver it to a registry "
                "(`piceli artifacts deliver --to oci://…`) or import it into the "
                f"node under a content tag (`--ref {hint}`), then list the "
                "delivery receipt in images_from or [images."
                f"{self.name}] receipt",
            )
        raise ReleaseSpecError(
            f"image {self.name!r} has no repository; declare it as "
            "'repository@sha256:...' or use ImageRef.identity"
        )

    def to_dict(self) -> dict[str, str]:
        return {
            key: value
            for key, value in {
                "identity": self.identity,
                "repository": self.repository,
                "tag": self.tag,
                "digest": self.digest,
                "image_id": self.image_id,
                "platform": self.platform,
                "ref": self.ref,
                "source": self.source,
            }.items()
            if value is not None
        }


def _split_ref(ref: str) -> tuple[str, str | None, str | None]:
    """Split ``repo[:tag][@digest]`` without guessing registries."""
    digest = None
    if "@" in ref:
        ref, digest = ref.split("@", 1)
    tag = None
    slash = ref.rfind("/")
    colon = ref.rfind(":")
    if colon > slash:
        ref, tag = ref[:colon], ref[colon + 1 :]
    if not ref:
        raise ReleaseSpecError("image reference has no repository")
    return ref, tag, digest


def _read_document(path: Path, what: str) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ImageHandoffError(
            "receipt-invalid", f"cannot read {what} {path}: {error}"
        ) from None
    if not isinstance(document, dict):
        raise ImageHandoffError("receipt-invalid", f"{what} {path} is not an object")
    return document


def load_delivery_receipt(name: str, path: Path, pin: str | None = None) -> ImageRef:
    """Read the image reference from a delivery receipt.

    ``piceli.registry-delivery.v1``: the digest-pinned ``pull_ref``; ``pin``
    is the expected manifest digest. ``piceli.node-delivery.v1``: the node
    reference, which must be a content tag of the config digest; ``pin`` is the
    expected config digest.
    """
    return _delivery_image(name, _read_document(path, "delivery receipt"), pin)


def _delivery_image(name: str, document: dict[str, Any], pin: str | None) -> ImageRef:
    schema = document.get("schema")
    if schema not in DELIVERY_SCHEMAS:
        raise ImageHandoffError(
            "receipt-invalid",
            f"image {name!r}: receipt must have schema "
            f"{REGISTRY_DELIVERY_SCHEMA!r} or {NODE_DELIVERY_SCHEMA!r}",
        )
    accepted = (
        {"pushed", "already-present"}
        if schema == REGISTRY_DELIVERY_SCHEMA
        else {"imported", "already-present"}
    )
    if document.get("result") not in accepted:
        raise ImageHandoffError(
            "delivery-not-succeeded",
            f"image {name!r}: delivery did not succeed ({document.get('result')!r})",
        )
    image = document.get("image")
    image = image if isinstance(image, dict) else {}
    config = image.get("config_digest")
    if not (isinstance(config, str) and _DIGEST.fullmatch(config)):
        raise ImageHandoffError(
            "receipt-invalid", f"image {name!r}: receipt digests are invalid"
        )
    if schema == NODE_DELIVERY_SCHEMA:
        return _node_image(name, document, image, config, pin)
    manifest = image.get("manifest_digest")
    pull_ref = document.get("pull_ref")
    if not (isinstance(manifest, str) and _DIGEST.fullmatch(manifest)):
        raise ImageHandoffError(
            "receipt-invalid", f"image {name!r}: receipt digests are invalid"
        )
    if not isinstance(pull_ref, str):
        raise ImageHandoffError(
            "receipt-invalid", f"image {name!r}: receipt has no pull_ref"
        )
    repository, tag, embedded = _split_ref(pull_ref)
    if embedded != manifest:
        raise ImageHandoffError(
            "image-not-immutable",
            f"image {name!r}: receipt pull_ref is not pinned to its manifest digest",
        )
    if pin is not None and pin != manifest:
        raise ImageHandoffError(
            "image-digest-mismatch",
            f"image {name!r}: receipt manifest digest does not match the pinned digest",
        )
    return ImageRef(
        name,
        manifest,
        repository,
        tag,
        manifest,
        image_id=config,
        ref=pull_ref,
        source=REGISTRY_DELIVERY_SCHEMA,
    )


def _node_image(
    name: str,
    document: dict[str, Any],
    image: dict[str, Any],
    config: str,
    pin: str | None,
) -> ImageRef:
    reference = image.get("reference")
    if document.get("approved_digest") != config:
        raise ImageHandoffError(
            "receipt-invalid",
            f"image {name!r}: receipt config digest is not the approved digest",
        )
    if not isinstance(reference, str) or "@" in reference:
        raise ImageHandoffError(
            "receipt-invalid", f"image {name!r}: receipt has no node reference"
        )
    repository, tag, _ = _split_ref(reference)
    if not _is_content_tag(tag, config):
        raise ImageHandoffError(
            "image-not-immutable",
            f"image {name!r}: node reference {reference!r} is a tag another "
            "import can move; import it under a content tag of its config "
            f"digest (`--ref {repository}:{content_tag(config)}`)",
        )
    if pin is not None and pin != config:
        raise ImageHandoffError(
            "image-digest-mismatch",
            f"image {name!r}: receipt config digest does not match the pinned digest",
        )
    return ImageRef(
        name,
        config,
        repository,
        tag,
        None,
        image_id=config,
        ref=reference,
        source=NODE_DELIVERY_SCHEMA,
    )


def _image_from_spec(
    name: str, value: str | ImageSpec, resolve: Callable[[Path], Path] | None = None
) -> ImageRef:
    if isinstance(value, ImageSpec) and value.receipt is not None:
        path = resolve(value.receipt) if resolve else value.receipt
        return load_delivery_receipt(name, path, value.digest)
    if isinstance(value, ImageSpec):
        assert value.digest is not None
        repository, tag = (None, None)
        if value.ref:
            repository, tag, embedded = _split_ref(value.ref)
            if embedded is not None and embedded != value.digest:
                raise ReleaseSpecError(f"image {name!r}: ref digest != digest")
        return ImageRef(
            name, value.digest, repository, tag, value.digest, ref=value.ref
        )
    if _DIGEST.fullmatch(value):
        return ImageRef(name, value, digest=value)
    repository, tag, digest = _split_ref(value)
    if digest is None or not _DIGEST.fullmatch(digest):
        raise ReleaseSpecError(
            f"image {name!r} must be pinned by digest (repository@sha256:...)"
        )
    return ImageRef(name, digest, repository, tag, digest, ref=value)


def load_build_receipt(path: Path) -> dict[str, ImageRef]:
    """Read ``outputs.images`` from a ``piceli.build-receipt.v1`` receipt.

    An entry without a registry ``digest`` loads, but its
    :attr:`ImageRef.reference` is refused (``image-not-immutable``) until a
    delivery receipt supplies an immutable reference.
    """
    return _build_images(_read_document(path, "build receipt"))


def _build_images(document: dict[str, Any]) -> dict[str, ImageRef]:
    if document.get("revision") != BUILD_RECEIPT_REVISION:
        raise ImageHandoffError(
            "receipt-invalid",
            f"build receipt must have revision {BUILD_RECEIPT_REVISION!r}",
        )
    outputs = document.get("outputs")
    images = outputs.get("images") if isinstance(outputs, dict) else None
    if not isinstance(images, dict) or not images:
        raise ImageHandoffError(
            "receipt-invalid", "build receipt has no outputs.images"
        )
    result: dict[str, ImageRef] = {}
    for name, entry in images.items():
        if not isinstance(name, str) or not _IMAGE_NAME.fullmatch(name):
            raise ReleaseSpecError(f"invalid image name in receipt: {name!r}")
        if not isinstance(entry, dict):
            raise ReleaseSpecError(f"receipt image {name!r} must be an object")
        image_id = entry.get("image_id")
        digest = entry.get("digest")
        platform = entry.get("platform")
        ref = entry.get("ref")
        if not isinstance(image_id, str) or not _DIGEST.fullmatch(image_id):
            raise ReleaseSpecError(f"receipt image {name!r} needs a sha256 image_id")
        if digest is not None and (
            not isinstance(digest, str) or not _DIGEST.fullmatch(digest)
        ):
            raise ReleaseSpecError(f"receipt image {name!r} has an invalid digest")
        if platform is not None and not isinstance(platform, str):
            raise ReleaseSpecError(f"receipt image {name!r} has an invalid platform")
        if ref is not None and not isinstance(ref, str):
            raise ReleaseSpecError(f"receipt image {name!r} has an invalid ref")
        repository = tag = None
        if ref:
            repository, tag, embedded = _split_ref(ref)
            if embedded is not None and digest is not None and embedded != digest:
                raise ReleaseSpecError(f"receipt image {name!r}: ref digest != digest")
        result[name] = ImageRef(
            name,
            digest or image_id,
            repository,
            tag,
            digest,
            image_id,
            platform,
            ref,
            BUILD_RECEIPT_REVISION,
        )
    return result


def _same_image(built: ImageRef, delivered: ImageRef) -> bool:
    """A delivery is of a built image when the config digests (or, for a
    containerd-store build, the manifest digests) are equal."""
    if delivered.image_id is not None and delivered.image_id == built.image_id:
        return True
    return built.digest is not None and built.digest == delivered.digest


def _delivered(name: str, built: ImageRef, delivered: ImageRef) -> ImageRef:
    if not _same_image(built, delivered):
        raise ImageHandoffError(
            "image-digest-mismatch",
            f"image {name!r}: the delivery receipt's config digest "
            f"{delivered.image_id} is not the built image {built.image_id}",
        )
    return replace(delivered, name=name, platform=built.platform)


def load_images_from(paths: Sequence[Path]) -> dict[str, ImageRef]:
    """Merge build and delivery receipts into one image per name.

    Build receipts name the images; a name may appear in only one of them.
    Each delivery receipt (``piceli.registry-delivery.v1`` or
    ``piceli.node-delivery.v1``) replaces every built image with its config
    digest, and must match at least one. An image may be delivered by only one
    listed receipt.
    """
    built: dict[str, ImageRef] = {}
    deliveries: list[tuple[str, dict[str, Any]]] = []
    for path in paths:
        document = _read_document(path, "receipt")
        if document.get("revision") == BUILD_RECEIPT_REVISION:
            for name, image in _build_images(document).items():
                if name in built:
                    raise ImageHandoffError(
                        "image-declared-twice",
                        f"image {name!r} appears in more than one build receipt",
                    )
                built[name] = image
        elif document.get("schema") in DELIVERY_SCHEMAS:
            deliveries.append((path.name, document))
        else:
            raise ImageHandoffError(
                "receipt-invalid",
                f"{path.name} is neither a {BUILD_RECEIPT_REVISION!r} nor a "
                f"{REGISTRY_DELIVERY_SCHEMA!r}/{NODE_DELIVERY_SCHEMA!r} receipt",
            )
    result = dict(built)
    delivered_by: dict[str, str] = {}
    for label, document in deliveries:
        image = _delivery_image(label, document, None)
        names = [name for name, item in built.items() if _same_image(item, image)]
        if not names:
            raise ImageHandoffError(
                "receipt-unmatched",
                f"delivery receipt {label} matches no image of the listed build "
                "receipts; name it with [images.<name>] receipt = …",
            )
        for name in names:
            if name in delivered_by:
                raise ImageHandoffError(
                    "image-declared-twice",
                    f"image {name!r} is delivered by both {delivered_by[name]} "
                    f"and {label}",
                )
            delivered_by[name] = label
            result[name] = _delivered(name, built[name], image)
    return result


@dataclass(frozen=True)
class NodeRef:
    """A verified node: name and server-observed UID."""

    name: str
    uid: str


@dataclass(frozen=True)
class ReleaseContext:
    """What a composition function receives.

    ``secrets`` holds opaque :class:`SecretVersionRef` values only; bind them
    with ``ResourceIntent.with_secret(pointer, ctx.secret(name))``. Values are
    resolved by the executor at apply time and never reach the composition.
    """

    namespace: str
    images: Mapping[str, ImageRef]
    secrets: Mapping[str, SecretVersionRef]
    values: Mapping[str, Any] = field(default_factory=dict)
    nodes: Mapping[str, NodeRef] = field(default_factory=dict)

    def image(self, name: str) -> str:
        """Pull reference for a declared image."""
        try:
            return self.images[name].reference
        except KeyError:
            raise ReleaseSpecError(
                f"unknown image {name!r}; declared: {sorted(self.images)}"
            ) from None

    def secret(self, name: str) -> SecretVersionRef:
        """Opaque reference for a generated secret input."""
        try:
            return self.secrets[name]
        except KeyError:
            raise ReleaseSpecError(
                f"unknown secret input {name!r}; declared: {sorted(self.secrets)}"
            ) from None


CompositionFunction = Callable[[ReleaseContext], DeploymentComposition]


@dataclass(frozen=True)
class ReleaseSpec:
    """A validated spec plus the directory its relative paths resolve from."""

    model: ReleaseSpecModel
    base: Path
    path: Path | None = None

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], base: Path) -> ReleaseSpec:
        for table, content in value.items():
            if isinstance(content, Mapping) and "images_from" in content:
                raise ReleaseSpecError(
                    f"invalid release spec: images_from is a top-level key but "
                    f"was found inside [{table}]; move it above the first "
                    "[table] of the spec"
                )
        try:
            model = ReleaseSpecModel.model_validate(dict(value))
        except ValidationError as error:
            problems = "; ".join(
                f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
                for item in error.errors()
            )
            raise ReleaseSpecError(f"invalid release spec: {problems}") from None
        check_secrets(model.secrets)
        return cls(model, base.resolve())

    @classmethod
    def from_toml(cls, path: Path) -> ReleaseSpec:
        try:
            with path.open("rb") as stream:
                document = tomllib.load(stream)
        except tomllib.TOMLDecodeError as error:
            raise ReleaseSpecError(f"invalid release spec TOML: {error}") from None
        except OSError as error:
            raise ReleaseSpecError(f"cannot read release spec: {error}") from None
        spec = cls.from_dict(document, path.resolve().parent)
        return cls(spec.model, spec.base, path.resolve())

    def resolve(self, path: Path) -> Path:
        path = path.expanduser()
        return path if path.is_absolute() else self.base / path

    @property
    def state_dir(self) -> Path:
        return self.resolve(self.model.release.state_dir)

    @property
    def catalog_path(self) -> Path:
        value = self.model.release.catalog
        return self.resolve(value) if value else self.state_dir / "catalog.json"

    @property
    def journal_path(self) -> Path:
        value = self.model.release.journal
        return self.resolve(value) if value else self.state_dir / "journal.sqlite"

    @property
    def secret_store_path(self) -> Path:
        value = self.model.release.secret_store
        return self.resolve(value) if value else self.state_dir / "secrets.sqlite"

    def kubeconfig_target(self) -> KubeconfigTarget:
        target = self.model.target
        return KubeconfigTarget(
            kubeconfig=self.resolve(target.kubeconfig),
            context=target.context,
            namespace=target.namespace,
            cluster_uid=target.cluster_uid,
            namespace_uid=target.namespace_uid,
            transport=target.transport,
            request_seconds=target.request_seconds,
            nodes={
                alias: NodeExpectation(node.name, node.uid)
                for alias, node in target.nodes.items()
            },
        )

    def images(self) -> dict[str, ImageRef]:
        """Declared images plus the ``images_from`` receipts' (merged).

        A name in both is refused, except ``[images.<name>] receipt = …``
        over a built image: the delivery receipt wins when its config digest
        matches the build, and is refused otherwise.
        """
        result = {
            name: _image_from_spec(name, value, self.resolve)
            for name, value in self.model.images.items()
        }
        sources = self.model.images_from
        if sources is not None:
            paths = [sources] if isinstance(sources, Path) else list(sources)
            loaded = load_images_from([self.resolve(path) for path in paths])
            for name, image in loaded.items():
                declared = self.model.images.get(name)
                if name not in result:
                    result[name] = image
                elif (
                    isinstance(declared, ImageSpec)
                    and declared.receipt is not None
                    and image.source == BUILD_RECEIPT_REVISION
                ):
                    result[name] = _delivered(name, image, result[name])
                else:
                    raise ImageHandoffError(
                        "image-declared-twice",
                        f"image {name!r} is declared in both [images] and images_from",
                    )
        return dict(sorted(result.items()))

    def load_composition(self) -> CompositionFunction:
        """Import ``module:function`` or ``path/to/file.py:function``."""
        entry = self.model.release.composition
        target, _, attribute = entry.rpartition(":")
        if target.endswith(".py") or "/" in target:
            path = self.resolve(Path(target))
            if not path.is_file():
                raise ReleaseSpecError(f"composition file not found: {path}")
            digest = hashlib.sha256(str(path).encode()).hexdigest()[:16]
            module_name = f"_piceli_release_composition_{digest}"
            module = sys.modules.get(module_name)
            if module is None:
                loader_spec = importlib.util.spec_from_file_location(module_name, path)
                if loader_spec is None or loader_spec.loader is None:
                    raise ReleaseSpecError(f"cannot import composition file {path}")
                module = importlib.util.module_from_spec(loader_spec)
                sys.modules[module_name] = module
                try:
                    loader_spec.loader.exec_module(module)
                except BaseException:
                    del sys.modules[module_name]
                    raise
        else:
            try:
                module = importlib.import_module(target)
            except ImportError as error:
                raise ReleaseSpecError(
                    f"cannot import composition module {target!r}: {error}"
                ) from None
        function = getattr(module, attribute, None)
        if not callable(function):
            raise ReleaseSpecError(f"composition entry {entry!r} is not callable")
        return function  # type: ignore[no-any-return]

    def context(
        self,
        images: Mapping[str, ImageRef],
        secrets: Mapping[str, SecretVersionRef],
        nodes: Mapping[str, NodeRef] | None = None,
    ) -> ReleaseContext:
        return ReleaseContext(
            namespace=self.model.target.namespace,
            images=MappingProxyType(dict(images)),
            secrets=MappingProxyType(dict(secrets)),
            values=MappingProxyType(dict(self.model.values)),
            nodes=MappingProxyType(dict(nodes or {})),
        )
