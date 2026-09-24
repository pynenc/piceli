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
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from piceli.k8s.ops.plan import DeploymentComposition
from piceli.k8s.ops.provider_factory import KubeconfigTarget, NodeExpectation
from piceli.k8s.ops.secret_versions import SecretVersionRef

BUILD_RECEIPT_REVISION = "piceli.build-receipt.v1"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_IMAGE_NAME = re.compile(r"[a-z][a-z0-9_-]{0,62}")
_SECRET_NAME = re.compile(r"[a-z][a-z0-9-]{0,62}")
_DNS = re.compile(
    r"(?:\*\.)?(?:[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?"
)
_RELEASE_PREFIX = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,40}[a-z0-9])?")
_KIND = re.compile(r"(?:[a-z0-9.-]+/)?[a-z][a-z0-9]*/[A-Za-z][A-Za-z0-9]*")


class ReleaseSpecError(ValueError):
    """The release spec, build receipt or composition entry point is invalid."""


_ADOPT_ENTRY = re.compile(
    r"(?:(?P<api>(?:[a-z0-9.-]+/)?v[0-9][a-z0-9]*)/)?"
    r"(?P<kind>[A-Z][A-Za-z0-9]*)/(?P<name>[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?)"
)


def parse_adopt_entry(value: str) -> tuple[str | None, str, str]:
    """``Kind/name`` or ``apiVersion/Kind/name`` → (api_version, kind, name)."""
    match = _ADOPT_ENTRY.fullmatch(value) if isinstance(value, str) else None
    if match is None or len(match["name"]) > 253:
        raise ReleaseSpecError(
            f"adopt entry must be 'Kind/name' or 'apiVersion/Kind/name', got {value!r}"
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

    @field_validator("adopt")
    @classmethod
    def _adopt(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            parse_adopt_entry(item)
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
    ref: str | None = None
    digest: str

    @field_validator("digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if not _DIGEST.fullmatch(value):
            raise ValueError("image digest must be sha256:<64 hex>")
        return value


class RandomSecretSpec(_Strict):
    type: Literal["random"]
    bytes: int = Field(default=32, ge=16, le=512)
    encoding: Literal["base64", "raw"] = "base64"


class TlsSelfSignedSpec(_Strict):
    type: Literal["tls-self-signed"]
    dns_names: tuple[str, ...] = Field(min_length=1, max_length=32)
    ip_addresses: tuple[str, ...] = ()
    days: int = Field(default=365, ge=1, le=3650)
    rsa_bits: Literal[2048, 3072, 4096] = 2048
    encoding: Literal["base64", "raw"] = "base64"
    openssl: Path = Path("/usr/bin/openssl")
    openssl_sha256: str | None = None

    @field_validator("dns_names")
    @classmethod
    def _dns(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            if len(item) > 253 or not _DNS.fullmatch(item):
                raise ValueError(f"invalid DNS name {item!r}")
        return value

    @field_validator("ip_addresses")
    @classmethod
    def _ips(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        import ipaddress

        for item in value:
            ipaddress.ip_address(item)
        return value

    @field_validator("openssl")
    @classmethod
    def _tool(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("openssl must be an absolute path (a pinned tool)")
        return value

    @field_validator("openssl_sha256")
    @classmethod
    def _tool_digest(cls, value: str | None) -> str | None:
        if value is not None and not _DIGEST.fullmatch(value):
            raise ValueError("openssl_sha256 must be sha256:<64 hex>")
        return value


SecretSpec = Annotated[
    RandomSecretSpec | TlsSelfSignedSpec, Field(discriminator="type")
]


class ReleaseSpecModel(_Strict):
    target: TargetSpec
    release: ReleaseSettings
    execution: ExecutionSpec = ExecutionSpec()
    discovery: DiscoverySpec = DiscoverySpec()
    images: dict[str, str | ImageSpec] = Field(default_factory=dict)
    images_from: Path | None = None
    secrets: dict[str, SecretSpec] = Field(default_factory=dict)
    values: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _names(self) -> ReleaseSpecModel:
        for name in self.images:
            if not _IMAGE_NAME.fullmatch(name):
                raise ValueError(f"invalid image name {name!r}")
        for name in self.secrets:
            if not _SECRET_NAME.fullmatch(name):
                raise ValueError(f"invalid secret name {name!r}")
        if not self.images and self.images_from is None:
            raise ValueError("declare [images] or images_from")
        return self


@dataclass(frozen=True)
class ImageRef:
    """One pinned image as seen by the composition function.

    ``identity`` is the digest recorded in the release (the registry digest
    when known, otherwise the local image ID from the build receipt).
    """

    name: str
    identity: str
    repository: str | None = None
    tag: str | None = None
    digest: str | None = None
    image_id: str | None = None
    platform: str | None = None
    ref: str | None = None

    @property
    def reference(self) -> str:
        """The pull reference: ``repository@digest`` when possible, else ``ref``."""
        if self.repository and self.digest:
            return f"{self.repository}@{self.digest}"
        if self.ref:
            return self.ref
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


def _image_from_spec(name: str, value: str | ImageSpec) -> ImageRef:
    if isinstance(value, ImageSpec):
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
    """Read ``outputs.images`` from a ``piceli.build-receipt.v1`` receipt."""
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ReleaseSpecError(f"cannot read build receipt {path}: {error}") from None
    if (
        not isinstance(document, dict)
        or document.get("revision") != BUILD_RECEIPT_REVISION
    ):
        raise ReleaseSpecError(
            f"build receipt must have revision {BUILD_RECEIPT_REVISION!r}"
        )
    outputs = document.get("outputs")
    images = outputs.get("images") if isinstance(outputs, dict) else None
    if not isinstance(images, dict) or not images:
        raise ReleaseSpecError("build receipt has no outputs.images")
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
        )
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
        try:
            model = ReleaseSpecModel.model_validate(dict(value))
        except ValidationError as error:
            problems = "; ".join(
                f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
                for item in error.errors()
            )
            raise ReleaseSpecError(f"invalid release spec: {problems}") from None
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
        """Declared images plus the build receipt's, rejecting name collisions."""
        result = {
            name: _image_from_spec(name, value)
            for name, value in self.model.images.items()
        }
        if self.model.images_from is not None:
            for name, image in load_build_receipt(
                self.resolve(self.model.images_from)
            ).items():
                if name in result:
                    raise ReleaseSpecError(
                        f"image {name!r} is declared in both [images] and images_from"
                    )
                result[name] = image
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
