"""Python-first durable deployment sessions without provider construction.

The session boundary owns input materialization and stable durable identity.  A
composition factory receives only opaque ``SecretVersionRef`` values, while
planning consumes a supplied immutable snapshot.  No session constructor imports
or invokes a Kubernetes provider.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

from piceli.k8s.ops.bounds import timestamp
from piceli.k8s.ops.execution_journal import ExecutionJournal
from piceli.k8s.ops.executor import ExecutionAuthorization, PlanExecutor
from piceli.k8s.ops.plan import (
    DeploymentComponent,
    DeploymentComposition,
    DeploymentPlan,
    ObservedSnapshot,
    PlanAuthorization,
    ResourceIntent,
    ResourceRef,
    build_plan,
)
from piceli.k8s.ops.revision import DeploymentRevision, ExecutionBundle
from piceli.k8s.ops.secret_versions import SecretVersionRef, SecretVersionStore

DEPLOYMENT_SESSION_SCHEMA_VERSION = 1
CompositionFactory = Callable[[Mapping[str, SecretVersionRef]], DeploymentComposition]
AuthorizationFactory = Callable[
    [DeploymentPlan, ObservedSnapshot], ExecutionAuthorization
]


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _composition_material(composition: DeploymentComposition) -> list[dict[str, Any]]:
    """Return only redacted composition data, including opaque input bindings."""
    return [
        {
            "name": component.name,
            "dependencies": list(component.dependencies),
            "resources": [
                {
                    "resource": resource.ref.__dict__,
                    "manifest": resource.redacted_manifest(),
                    "dependencies": [item.__dict__ for item in resource.dependencies],
                    "private_bindings": [
                        {
                            "pointer": binding.json_pointer,
                            "reference": binding.reference.__dict__,
                        }
                        for binding in resource.secret_bindings
                    ],
                }
                for resource in component.resources
            ],
        }
        for component in composition.components
    ]


def _composition_references(
    composition: DeploymentComposition,
) -> set[SecretVersionRef]:
    return {
        binding.reference
        for component in composition.components
        for resource in component.resources
        for binding in resource.secret_bindings
    }


def composition_from_archive(
    archive: DeploymentSessionArchive,
) -> DeploymentComposition:
    """Reconstruct the exact redacted composition stored in a session archive."""
    components = []
    for raw_component in archive.to_dict()["composition"]:
        if (
            not isinstance(raw_component, dict)
            or set(raw_component) != {"name", "dependencies", "resources"}
            or not isinstance(raw_component["name"], str)
            or not isinstance(raw_component["dependencies"], list)
            or not isinstance(raw_component["resources"], list)
        ):
            raise ValueError("deployment session composition is invalid")
        resources = []
        for raw_resource in raw_component["resources"]:
            if not isinstance(raw_resource, dict) or set(raw_resource) != {
                "resource",
                "manifest",
                "dependencies",
                "private_bindings",
            }:
                raise ValueError("deployment session resource is invalid")
            dependencies = tuple(
                ResourceRef(**item) for item in raw_resource["dependencies"]
            )
            resource = ResourceIntent.from_manifest(
                raw_resource["manifest"], dependencies
            )
            if resource.ref != ResourceRef(**raw_resource["resource"]):
                raise ValueError("deployment session resource identity changed")
            for binding in raw_resource["private_bindings"]:
                if not isinstance(binding, dict) or set(binding) != {
                    "pointer",
                    "reference",
                }:
                    raise ValueError("deployment session private binding is invalid")
                resource = resource.with_secret(
                    binding["pointer"], SecretVersionRef(**binding["reference"])
                )
            resources.append(resource)
        components.append(
            DeploymentComponent(
                raw_component["name"],
                tuple(resources),
                tuple(raw_component["dependencies"]),
            )
        )
    composition = DeploymentComposition(tuple(components))
    if _composition_material(composition) != archive.to_dict()["composition"]:
        raise ValueError("deployment session composition changed during recovery")
    return composition


@dataclass(frozen=True)
class DeploymentSessionArchive:
    """Canonical interchange for one session; opaque IDs are not public reports."""

    _encoded: str

    @classmethod
    def create(
        cls,
        *,
        session_id: str,
        inputs: Mapping[str, SecretVersionRef],
        composition: DeploymentComposition,
        bundle: ExecutionBundle,
    ) -> DeploymentSessionArchive:
        if not re.fullmatch(r"[0-9a-f]{32}", session_id):
            raise ValueError("invalid deployment session identity")
        value = {
            "schema_version": DEPLOYMENT_SESSION_SCHEMA_VERSION,
            "session_id": session_id,
            "private_inputs": [
                {"name": name, "reference": reference.__dict__}
                for name, reference in sorted(inputs.items())
            ],
            "composition": _composition_material(composition),
            "revision": bundle.revision.to_dict(),
            "bundle": bundle.to_dict(),
        }
        return cls(_canonical(value))

    @classmethod
    def from_json(cls, encoded: str | Mapping[str, Any]) -> DeploymentSessionArchive:
        value = json.loads(encoded) if isinstance(encoded, str) else dict(encoded)
        canonical = _canonical(value)
        if isinstance(encoded, str) and canonical != encoded:
            raise ValueError("deployment session archive must use canonical JSON")
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != DEPLOYMENT_SESSION_SCHEMA_VERSION
            or not re.fullmatch(r"[0-9a-f]{32}", str(value.get("session_id", "")))
            or set(value)
            != {
                "schema_version",
                "session_id",
                "private_inputs",
                "composition",
                "revision",
                "bundle",
            }
        ):
            raise ValueError("deployment session archive is invalid")
        if not isinstance(value["private_inputs"], list):
            raise ValueError("deployment session private inputs are invalid")
        return cls(canonical)

    def to_dict(self) -> dict[str, Any]:
        value = json.loads(self._encoded)
        assert isinstance(value, dict)
        return value

    def to_json(self) -> str:
        return self._encoded

    @property
    def session_id(self) -> str:
        return str(self.to_dict()["session_id"])

    def inputs(self) -> dict[str, SecretVersionRef]:
        result: dict[str, SecretVersionRef] = {}
        for item in self.to_dict()["private_inputs"]:
            if (
                not isinstance(item, dict)
                or set(item) != {"name", "reference"}
                or not isinstance(item["name"], str)
                or not isinstance(item["reference"], dict)
                or item["name"] in result
            ):
                raise ValueError("deployment session private inputs are invalid")
            result[item["name"]] = SecretVersionRef(**item["reference"])
        return result

    def report(self) -> dict[str, Any]:
        """Return stable public identity without exposing opaque input references."""
        value = self.to_dict()
        bundle = value["bundle"]
        revision = value["revision"]
        desired = revision["desired_state"]
        return {
            "schema_version": value["schema_version"],
            "session_id": value["session_id"],
            "execution_id": bundle["execution_id"],
            "revision_id": revision["revision_id"],
            "action_count": len(bundle["action_ids"]),
            "private_input_count": len(value["private_inputs"]),
            "target": desired["target"],
        }

    def preview(self) -> dict[str, Any]:
        """Return the archived redacted plan without renewing write authority."""
        composition_from_archive(self)
        return self.report() | {"plan": self.to_dict()["revision"]["desired_state"]}


@dataclass(frozen=True)
class DeploymentSession:
    """One exact deployment identity across preview, apply, stop and recovery."""

    archive: DeploymentSessionArchive
    composition: DeploymentComposition
    revision: DeploymentRevision
    bundle: ExecutionBundle
    journal: ExecutionJournal
    secrets: SecretVersionStore = field(repr=False)

    @classmethod
    def create(
        cls,
        *,
        private_inputs: Mapping[str, Any],
        composition_factory: CompositionFactory,
        snapshot: ObservedSnapshot,
        plan_authorization: PlanAuthorization,
        authorization_factory: AuthorizationFactory,
        journal: ExecutionJournal,
        secrets: SecretVersionStore,
        session_id: str | None = None,
        execution_id: str | None = None,
    ) -> DeploymentSession:
        """Materialize caller values once and create a provider-free durable session."""
        identifier = session_id or uuid.uuid4().hex
        if not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise ValueError("invalid deployment session identity")
        if cls._has_session(journal, identifier):
            raise ValueError("deployment session exists; open it or rotate explicitly")
        inputs = {
            name: secrets.put_once(snapshot.target, identifier, name, value)
            for name, value in sorted(private_inputs.items())
        }
        return cls._build(
            identifier,
            inputs,
            composition_factory,
            snapshot,
            plan_authorization,
            authorization_factory,
            journal,
            secrets,
            execution_id=execution_id,
        )

    @staticmethod
    def _has_session(journal: ExecutionJournal, session_id: str) -> bool:
        try:
            journal.session(session_id)
        except ValueError as error:
            if str(error) == "deployment session missing":
                return False
            raise
        return True

    @classmethod
    def open(
        cls,
        archive: str | Mapping[str, Any],
        *,
        composition_factory: CompositionFactory,
        snapshot: ObservedSnapshot,
        plan_authorization: PlanAuthorization,
        authorization_factory: AuthorizationFactory,
        journal: ExecutionJournal,
        secrets: SecretVersionStore,
    ) -> DeploymentSession:
        """Rebuild an existing session and reject every identity-affecting drift."""
        parsed = DeploymentSessionArchive.from_json(archive)
        persisted = DeploymentSessionArchive.from_json(
            journal.session(parsed.session_id)
        )
        if persisted.to_json() != parsed.to_json():
            raise ValueError("deployment session archive does not match journal")
        result = cls._build(
            parsed.session_id,
            parsed.inputs(),
            composition_factory,
            snapshot,
            plan_authorization,
            authorization_factory,
            journal,
            secrets,
            existing=parsed,
        )
        return result

    def rotate(
        self,
        *,
        private_inputs: Mapping[str, Any],
        composition_factory: CompositionFactory,
        snapshot: ObservedSnapshot,
        plan_authorization: PlanAuthorization,
        authorization_factory: AuthorizationFactory,
        session_id: str | None = None,
        execution_id: str | None = None,
    ) -> DeploymentSession:
        """Create a new immutable session for deliberately rotated inputs."""
        identifier = session_id or uuid.uuid4().hex
        if identifier == self.archive.session_id:
            raise ValueError("private rotation requires a new deployment session")
        return self.create(
            private_inputs=private_inputs,
            composition_factory=composition_factory,
            snapshot=snapshot,
            plan_authorization=plan_authorization,
            authorization_factory=authorization_factory,
            journal=self.journal,
            secrets=self.secrets,
            session_id=identifier,
            execution_id=execution_id,
        )

    @classmethod
    def _build(
        cls,
        session_id: str,
        inputs: Mapping[str, SecretVersionRef],
        composition_factory: CompositionFactory,
        snapshot: ObservedSnapshot,
        plan_authorization: PlanAuthorization,
        authorization_factory: AuthorizationFactory,
        journal: ExecutionJournal,
        secrets: SecretVersionStore,
        *,
        execution_id: str | None = None,
        existing: DeploymentSessionArchive | None = None,
    ) -> DeploymentSession:
        if plan_authorization.target != snapshot.target:
            raise ValueError("session plan authorization target mismatch")
        for name, reference in inputs.items():
            if (
                secrets.session_reference(snapshot.target, session_id, name)
                != reference
            ):
                raise ValueError("deployment session private reference/store mismatch")
        composition = composition_factory(MappingProxyType(dict(inputs)))
        if not isinstance(composition, DeploymentComposition):
            raise TypeError("composition factory must return DeploymentComposition")
        if _composition_references(composition) != set(inputs.values()):
            raise ValueError("deployment session composition/private input mismatch")
        plan = build_plan(composition, snapshot, plan_authorization)
        authorization = authorization_factory(plan, snapshot)
        if timestamp(authorization.expires_at, allow_future=True) <= datetime.now(UTC):
            raise ValueError("deployment session authorization expired")
        revision = DeploymentRevision.create(plan, snapshot, authorization)
        if existing is None:
            bundle = ExecutionBundle.create(revision, execution_id=execution_id)
            archive = DeploymentSessionArchive.create(
                session_id=session_id,
                inputs=inputs,
                composition=composition,
                bundle=bundle,
            )
            journal.store_session(session_id, archive.to_dict())
        else:
            raw = existing.to_dict()
            if raw["composition"] != _composition_material(composition):
                raise ValueError("deployment session composition changed")
            if raw["revision"] != revision.to_dict():
                raise ValueError("deployment session revision or authorization changed")
            bundle = ExecutionBundle.from_json(raw["bundle"], revision=revision)
            archive = existing
        return cls(archive, composition, revision, bundle, journal, secrets)

    def preview(self) -> dict[str, Any]:
        """Return a redacted provider-free preview."""
        return self.report() | {"plan": self.revision.plan.summary()}

    def apply(self, executor: PlanExecutor) -> dict[str, Any]:
        self._executor(executor)
        return executor.run_bundle(self.bundle)

    def resume(self, executor: PlanExecutor) -> dict[str, Any]:
        self._executor(executor)
        self._unambiguous_execution()
        return executor.run_bundle(self.bundle, resume=True)

    def stop(self, executor: PlanExecutor) -> dict[str, Any]:
        """Cancel only the exact owner-bound session execution."""
        self._executor(executor)
        if executor.provider.owner_id != self.bundle.authorization.owner_id:
            raise ValueError("deployment session owner mismatch")
        return executor.cancel(self.bundle.execution_id)

    def _executor(self, executor: PlanExecutor) -> None:
        if (
            executor.journal.path != self.journal.path
            or executor.secrets.store_id != self.secrets.store_id
        ):
            raise ValueError("deployment session executor/private store mismatch")
        if executor.provider.target != self.revision.plan.target:
            raise ValueError("deployment session target drift")

    def _unambiguous_execution(self) -> None:
        record = self.journal.export_execution(self.bundle.execution_id)
        allowed = {"pending", "failed", "blocked", "ready"}
        if record["cancelled"] or record["state"] not in allowed:
            raise ValueError("deployment session journal state is ambiguous")
        if len(record["actions"]) != len(self.bundle.action_ids) or any(
            row["operation_id"] != operation_id
            for row, operation_id in zip(
                record["actions"], self.bundle.action_ids, strict=True
            )
        ):
            raise ValueError("deployment session journal action identity changed")

    def report(self) -> dict[str, Any]:
        """Human/API safe report: no secret values, hashes or opaque references."""
        return self.archive.report()
