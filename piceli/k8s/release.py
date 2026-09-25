"""Durable, provider-free release records layered on Piceli deployment sessions.

The release catalog deliberately does not plan, execute, discover, or resolve
private values.  It binds one caller-owned source or artifact identity to a
canonical :class:`DeploymentSessionArchive`; callers reopen that exact session
through the existing session/executor API.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, TextIO

from piceli.k8s.ops.execution_journal import ExecutionJournal
from piceli.k8s.ops.executor import PlanExecutor
from piceli.k8s.ops.plan import (
    ObservedSnapshot,
    PlanAuthorization,
    build_plan,
    private_evidence,
)
from piceli.k8s.ops.revision import DeploymentRevision, ExecutionBundle
from piceli.k8s.ops.secret_versions import SecretVersionStore
from piceli.k8s.ops.session import (
    AuthorizationFactory,
    CompositionFactory,
    DeploymentSession,
    DeploymentSessionArchive,
    composition_from_archive,
)

RELEASE_CATALOG_SCHEMA_VERSION = 1
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_GIT = re.compile(r"[0-9a-f]{40}")
_DEFAULT_SOURCE_BYTES = 16 * 1024 * 1024


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


@dataclass(frozen=True)
class ReleaseSource:
    """Immutable release provenance without source content or secret material.

    Kinds: ``git`` (a commit), ``dirty`` (a checkout closure digest), ``oci``
    (one image digest) and ``oci-set``: several images, each built and
    delivered on its own. For ``oci-set``, ``images`` maps each image (a
    component or container name) to its digest and ``identity`` is the
    digest of that canonical map; build one with :meth:`image_set`.
    """

    kind: str
    identity: str
    dirty_closure_sha256: str | None = None
    artifact_digest: str | None = None
    artifact_archive_sha256: str | None = None
    images: Mapping[str, str] | None = field(default=None, hash=False)

    def __post_init__(self) -> None:
        if self.kind not in {"git", "dirty", "oci", "oci-set"}:
            raise ValueError("unsupported release source kind")
        if self.kind == "git" and not _GIT.fullmatch(self.identity):
            raise ValueError("Git release identity must be an immutable commit")
        if self.kind == "dirty" and not _SHA256.fullmatch(self.identity):
            raise ValueError("dirty release identity must be a sha256 closure")
        if self.kind == "oci" and not _SHA256.fullmatch(self.identity):
            raise ValueError("OCI release identity must be an immutable digest")
        if self.dirty_closure_sha256 and not _SHA256.fullmatch(
            self.dirty_closure_sha256
        ):
            raise ValueError("dirty source closure must be a sha256 digest")
        for value in (self.artifact_digest, self.artifact_archive_sha256):
            if value is not None and not _SHA256.fullmatch(value):
                raise ValueError("release artifact identity must be a sha256 digest")
        if self.kind == "oci" and self.artifact_digest != self.identity:
            raise ValueError("OCI release identity and artifact digest must match")
        if self.kind == "dirty" and self.dirty_closure_sha256 != self.identity:
            raise ValueError("dirty source identity and closure must match")
        if (self.images is not None) != (self.kind == "oci-set"):
            raise ValueError("an image set is recorded exactly for oci-set sources")
        if self.images is not None:
            images = dict(sorted(dict(self.images).items()))
            if not images or not all(
                isinstance(name, str) and name and _SHA256.fullmatch(str(value))
                for name, value in images.items()
            ):
                raise ValueError("an image set maps names to sha256 digests")
            if self.identity != "sha256:" + _digest(images):
                raise ValueError("oci-set identity must be the digest of its images")
            object.__setattr__(self, "images", MappingProxyType(images))

    @classmethod
    def image_set(cls, images: Mapping[str, str]) -> ReleaseSource:
        """An ``oci-set`` source for ``images`` (name → ``sha256:`` digest)."""
        ordered = dict(sorted(images.items()))
        return cls("oci-set", "sha256:" + _digest(ordered), images=ordered)

    def to_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "kind": self.kind,
                "identity": self.identity,
                "dirty_closure_sha256": self.dirty_closure_sha256,
                "artifact_digest": self.artifact_digest,
                "artifact_archive_sha256": self.artifact_archive_sha256,
                "images": dict(self.images) if self.images is not None else None,
            }.items()
            if value is not None
        }


def source_closure(
    root: Path,
    *,
    artifact_digest: str | None = None,
    artifact_archive_sha256: str | None = None,
    max_bytes: int = _DEFAULT_SOURCE_BYTES,
) -> ReleaseSource:
    """Pin one private Git checkout, including staged and untracked content."""
    root = root.resolve()

    def git(*arguments: str) -> bytes:
        return subprocess.check_output(
            ["git", "-C", str(root), *arguments], stderr=subprocess.DEVNULL
        )

    commit = git("rev-parse", "HEAD").decode().strip()
    if not _GIT.fullmatch(commit):
        raise ValueError("source checkout has no immutable Git commit")
    diff = git("diff", "--binary", "HEAD", "--")
    untracked = tuple(
        sorted(
            path.decode()
            for path in git("ls-files", "--others", "--exclude-standard", "-z").split(
                b"\0"
            )
            if path
        )
    )
    if not diff and not untracked:
        return ReleaseSource(
            "git",
            commit,
            artifact_digest=artifact_digest,
            artifact_archive_sha256=artifact_archive_sha256,
        )
    material = bytearray(commit.encode() + b"\0" + diff)
    for relative in untracked:
        path = root / relative
        if not path.is_file() or path.is_symlink():
            raise ValueError("dirty source closure contains a non-regular file")
        material.extend(relative.encode() + b"\0" + path.read_bytes() + b"\0")
        if len(material) > max_bytes:
            raise ValueError("dirty source closure exceeds its byte budget")
    if len(material) > max_bytes:
        raise ValueError("dirty source closure exceeds its byte budget")
    identity = "sha256:" + hashlib.sha256(material).hexdigest()
    return ReleaseSource(
        "dirty",
        identity,
        dirty_closure_sha256=identity,
        artifact_digest=artifact_digest,
        artifact_archive_sha256=artifact_archive_sha256,
    )


@dataclass(frozen=True)
class ReleaseRecord:
    """A public record selecting one exact session-backed deployment release."""

    name: str
    source: ReleaseSource
    archive: DeploymentSessionArchive
    namespace: str
    ttl_seconds: int | None = None
    retained_pvc_policy: str = "retain"

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", self.name):
            raise ValueError("invalid release name")
        if not self.namespace or len(self.namespace) > 63:
            raise ValueError("invalid release namespace")
        if self.ttl_seconds is not None and self.ttl_seconds <= 0:
            raise ValueError("preview TTL must be positive")
        if self.retained_pvc_policy not in {"retain", "delete-on-expiry"}:
            raise ValueError("invalid retained PVC policy")
        if self.retained_pvc_policy == "delete-on-expiry":
            raise ValueError("retained PVCs cannot be deleted by release expiry")

    @property
    def release_id(self) -> str:
        return _digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source": self.source.to_dict(),
            "archive": self.archive.to_dict(),
            "namespace": self.namespace,
            "ttl_seconds": self.ttl_seconds,
            "retained_pvc_policy": self.retained_pvc_policy,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ReleaseRecord:
        expected = {
            "name",
            "source",
            "archive",
            "namespace",
            "ttl_seconds",
            "retained_pvc_policy",
        }
        if set(value) != expected or not isinstance(value["source"], Mapping):
            raise ValueError("invalid release record")
        return cls(
            name=str(value["name"]),
            source=ReleaseSource(**dict(value["source"])),
            archive=DeploymentSessionArchive.from_json(dict(value["archive"])),
            namespace=str(value["namespace"]),
            ttl_seconds=value["ttl_seconds"],
            retained_pvc_policy=str(value["retained_pvc_policy"]),
        )


class ReleaseCatalog:
    """Locked atomic catalog of public release records for one local operator."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "schema_version": RELEASE_CATALOG_SCHEMA_VERSION,
                "releases": [],
                "selected": None,
            }
        value = json.loads(self.path.read_text())
        if (
            set(value) != {"schema_version", "releases", "selected"}
            or value["schema_version"] != RELEASE_CATALOG_SCHEMA_VERSION
        ):
            raise ValueError("unsupported release catalog")
        if not isinstance(value["releases"], list):
            raise ValueError("release catalog is malformed")
        return value

    def _write(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        encoded = _canonical(value)
        descriptor, name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent
        )
        temporary = Path(name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w") as output:
                output.write(encoded + "\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        descriptor = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _locked(self) -> TextIO:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock = self.path.with_suffix(self.path.suffix + ".lock")
        handle = lock.open("a+")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle

    def records(self) -> tuple[ReleaseRecord, ...]:
        # Atomic replace gives readers a complete version without creating locks
        # or directories during pure preview.
        value = self._read()
        return tuple(ReleaseRecord.from_dict(item) for item in value["releases"])

    def get(self, name: str) -> ReleaseRecord:
        """Read a release without changing the selected deployment."""
        for record in self.records():
            if record.name == name:
                return record
        raise ValueError("unknown release")

    def add(self, record: ReleaseRecord, *, select: bool = True) -> ReleaseRecord:
        with self._locked() as handle:
            value = self._read()
            existing = {item["name"]: item for item in value["releases"]}
            encoded = record.to_dict()
            if record.name in existing and existing[record.name] != encoded:
                raise ValueError(
                    "release name already selects another immutable record"
                )
            if record.name not in existing:
                value["releases"].append(encoded)
                value["releases"].sort(key=lambda item: item["name"])
            if select:
                value["selected"] = record.name
            self._write(value)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return record

    def select(self, name: str) -> ReleaseRecord:
        with self._locked() as handle:
            value = self._read()
            records = {item["name"]: item for item in value["releases"]}
            if name not in records:
                raise ValueError("unknown release")
            value["selected"] = name
            self._write(value)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return ReleaseRecord.from_dict(records[name])

    def selected(self) -> ReleaseRecord:
        value = self._read()
        if value["selected"] is None:
            raise ValueError("no release selected")
        return ReleaseRecord.from_dict(
            next(
                item for item in value["releases"] if item["name"] == value["selected"]
            )
        )


@dataclass(frozen=True)
class ReleaseWorkflow:
    """Public release lifecycle backed exclusively by a durable session.

    A workflow never constructs a provider or resolves a private value.  The
    caller supplies a frozen discovery snapshot and the same factories used by
    :class:`DeploymentSession`; this makes Python composition and safe
    JSON/YAML inputs converge before any executor is involved.
    """

    catalog: ReleaseCatalog
    namespace: str
    composition_factory: CompositionFactory
    snapshot: ObservedSnapshot
    plan_authorization: PlanAuthorization
    authorization_factory: AuthorizationFactory
    journal: ExecutionJournal
    secrets: SecretVersionStore

    def __post_init__(self) -> None:
        if self.namespace != self.snapshot.target.namespace:
            raise ValueError("release namespace must match the bound target")

    def create(
        self,
        *,
        name: str,
        source: ReleaseSource,
        private_inputs: Mapping[str, Any],
        ttl_seconds: int | None = None,
        retained_pvc_policy: str = "retain",
        session_id: str | None = None,
        execution_id: str | None = None,
    ) -> ReleaseRecord:
        """Materialize inputs once and record the resulting immutable session."""
        if any(record.name == name for record in self.catalog.records()):
            raise ValueError("release already exists; reopen its session")
        if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", name):
            raise ValueError("invalid release name")
        if ttl_seconds is not None and (
            not isinstance(ttl_seconds, int)
            or isinstance(ttl_seconds, bool)
            or ttl_seconds <= 0
        ):
            raise ValueError("preview TTL must be a positive integer")
        if retained_pvc_policy != "retain":
            raise ValueError("retained PVCs cannot be deleted by release expiry")
        session = DeploymentSession.create(
            private_inputs=private_inputs,
            composition_factory=self.composition_factory,
            snapshot=self.snapshot,
            plan_authorization=self.plan_authorization,
            authorization_factory=self.authorization_factory,
            journal=self.journal,
            secrets=self.secrets,
            session_id=session_id,
            execution_id=execution_id,
        )
        record = ReleaseRecord(
            name=name,
            source=source,
            archive=session.archive,
            namespace=self.namespace,
            ttl_seconds=ttl_seconds,
            retained_pvc_policy=retained_pvc_policy,
        )
        return self.catalog.add(record)

    def reopen(self, name: str | None = None) -> DeploymentSession:
        """Reconstruct exactly one catalogued session without rotating inputs."""
        record = self.catalog.get(name) if name is not None else self.catalog.selected()
        if record.namespace != self.namespace:
            raise ValueError("release record namespace differs from target")
        session = DeploymentSession.open(
            record.archive.to_json(),
            composition_factory=self.composition_factory,
            snapshot=self.snapshot,
            plan_authorization=self.plan_authorization,
            authorization_factory=self.authorization_factory,
            journal=self.journal,
            secrets=self.secrets,
        )
        if session.archive.to_json() != record.archive.to_json():
            raise ValueError("release archive changed while reopening")
        return session

    def preview(self, name: str | None = None) -> dict[str, Any]:
        """Provider-free preview of a catalogued immutable release."""
        record = self.catalog.get(name) if name is not None else self.catalog.selected()
        if record.namespace != self.namespace:
            raise ValueError("release record namespace differs from target")
        return record.archive.preview()

    def inspect(self, name: str | None = None) -> dict[str, Any]:
        """Return public release provenance and its exact session report."""
        record = self.catalog.get(name) if name is not None else self.catalog.selected()
        if record.namespace != self.namespace:
            raise ValueError("release record namespace differs from target")
        return {
            "release_id": record.release_id,
            "name": record.name,
            "source": record.source.to_dict(),
            "namespace": record.namespace,
            "ttl_seconds": record.ttl_seconds,
            "retained_pvc_policy": record.retained_pvc_policy,
            "session": record.archive.report(),
        }

    def select(self, name: str) -> dict[str, Any]:
        """Select an existing immutable release without executing provider writes."""
        record = self.catalog.get(name)
        if record.namespace != self.namespace:
            raise ValueError("release record namespace differs from target")
        composition_from_archive(record.archive)
        self.catalog.select(name)
        return self.inspect(name)

    def apply(self, executor: PlanExecutor, name: str | None = None) -> dict[str, Any]:
        """Apply exactly the selected release through the supplied executor."""
        return self.reopen(name).apply(executor)

    def resume(self, executor: PlanExecutor, name: str | None = None) -> dict[str, Any]:
        """Resume exactly the selected release without new private inputs."""
        return self.reopen(name).resume(executor)

    def stop(self, executor: PlanExecutor, name: str | None = None) -> dict[str, Any]:
        """Stop only the owner-bound execution selected by this catalog record."""
        record = self.catalog.get(name) if name is not None else self.catalog.selected()
        value = record.archive.to_dict()
        authorization = value["revision"]["authorization"]
        target = value["revision"]["desired_state"]["target"]
        if target != executor.provider.target.__dict__:
            raise ValueError("release record namespace differs from target")
        if authorization["owner_id"] != executor.provider.owner_id:
            raise ValueError("deployment session owner mismatch")
        if (
            executor.journal.path != self.journal.path
            or executor.secrets.store_id != self.secrets.store_id
        ):
            raise ValueError("deployment session executor/private store mismatch")
        return executor.cancel(value["bundle"]["execution_id"])

    def rollback(
        self,
        executor: PlanExecutor,
        name: str,
        *,
        execution_id: str | None = None,
    ) -> dict[str, Any]:
        """Reapply a prior archive against current evidence as a new execution."""
        record = self.catalog.get(name)
        if record.namespace != self.namespace:
            raise ValueError("release record namespace differs from target")
        if (
            executor.provider.target != self.snapshot.target
            or executor.journal.path != self.journal.path
            or executor.secrets.store_id != self.secrets.store_id
        ):
            raise ValueError("rollback executor/session boundary mismatch")
        for input_name, reference in record.archive.inputs().items():
            if (
                self.secrets.session_reference(
                    self.snapshot.target, record.archive.session_id, input_name
                )
                != reference
            ):
                raise ValueError("rollback private reference/store mismatch")
        composition = composition_from_archive(record.archive)
        expected = set(record.archive.inputs().values())
        actual = {
            binding.reference
            for component in composition.components
            for resource in component.resources
            for binding in resource.secret_bindings
        }
        if actual != expected:
            raise ValueError("rollback composition/private input mismatch")
        plan = build_plan(
            composition,
            self.snapshot,
            self.plan_authorization,
            private=private_evidence(
                composition,
                self.snapshot,
                lambda reference: self.secrets.resolve(self.snapshot.target, reference),
            ),
        )
        authorization = self.authorization_factory(plan, self.snapshot)
        revision = DeploymentRevision.create(plan, self.snapshot, authorization)
        bundle = ExecutionBundle.create(revision, execution_id=execution_id)
        result = executor.run_bundle(bundle)
        if result.get("state") != "ready":
            return result | {
                "rollback_target": name,
                "rollback_execution_id": bundle.execution_id,
                "selected": False,
            }
        self.catalog.select(name)
        return result | {
            "rollback_target": name,
            "rollback_execution_id": bundle.execution_id,
            "selected": True,
        }


def load_release_input(value: str | bytes | Mapping[str, Any]) -> dict[str, Any]:
    """Load JSON or safe YAML release input into the same validated mapping."""
    if isinstance(value, Mapping):
        return dict(value)
    text = value.decode() if isinstance(value, bytes) else value
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml
        except ImportError as error:
            raise ValueError("YAML input requires PyYAML") from error
        parsed = yaml.safe_load(text)
    if not isinstance(parsed, dict):
        raise ValueError("release input must be an object")
    return parsed
