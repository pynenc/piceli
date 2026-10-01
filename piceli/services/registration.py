"""Trusted, explicit local registrations. No browser-controlled code loading."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from typing import Literal

from piceli.k8s.ops.provider_factory import KubeconfigTarget
from piceli.k8s.release_spec import ReleaseSpec
from piceli.services.contracts import SourceRevision, Target


@dataclass(frozen=True)
class Registration:
    """A CLI-configured scope, retaining private inputs outside public DTOs."""

    id: str
    name: str
    target: KubeconfigTarget
    definition_kind: Literal["inventory", "release", "pipeline"] = "inventory"
    ownership: Literal["native", "external", "inventory"] = "inventory"
    release_spec: ReleaseSpec | None = None
    source: SourceRevision | None = None
    kinds: tuple[tuple[str, str], ...] = (
        ("apps/v1", "Deployment"),
        ("apps/v1", "ReplicaSet"),
        ("apps/v1", "StatefulSet"),
        ("apps/v1", "DaemonSet"),
        ("v1", "Pod"),
        ("v1", "Service"),
        ("v1", "ConfigMap"),
        ("v1", "Secret"),
        ("v1", "PersistentVolumeClaim"),
        ("batch/v1", "Job"),
        ("batch/v1", "CronJob"),
    )

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", self.id):
            raise ValueError("invalid registration identifier")
        if not self.name or len(self.name) > 256:
            raise ValueError("invalid registration name")
        if self.definition_kind == "release" and self.release_spec is None:
            raise ValueError("release registration requires an explicit definition")
        if self.release_spec is not None:
            expected = self.release_spec.kubeconfig_target()
            without_pins = replace(
                self.target,
                cluster_uid=expected.cluster_uid,
                namespace_uid=expected.namespace_uid,
            )
            if expected != without_pins or any(
                pin is not None and pin != actual
                for pin, actual in (
                    (expected.cluster_uid, self.target.cluster_uid),
                    (expected.namespace_uid, self.target.namespace_uid),
                )
            ):
                raise ValueError("definition and registration targets differ")

    @classmethod
    def from_release(cls, id: str, name: str, spec: ReleaseSpec) -> Registration:
        """Register an already loaded definition without executing its source."""
        # Configuration identity only: source evaluation later pins code content.
        identity = spec.model.model_dump_json()
        return cls(
            id=id,
            name=name,
            target=spec.kubeconfig_target(),
            definition_kind="release",
            ownership="native",
            release_spec=spec,
            source=SourceRevision(
                kind="local",
                revision=hashlib.sha256(identity.encode()).hexdigest(),
                entrypoint="registered definition (configuration identity; source not evaluated)",
            ),
        )

    def public_target(self) -> Target:
        """Opaque installation binding; context labels are never cluster identity."""
        target = self.target
        material = "\0".join(
            (
                str(target.kubeconfig.absolute()),
                target.context,
                target.namespace,
                target.cluster_uid or "",
                target.namespace_uid or "",
            )
        )
        return Target(
            id=hashlib.sha256(material.encode()).hexdigest()[:24],
            name=target.context,
            namespace=target.namespace,
            cluster_uid=target.cluster_uid,
            namespace_uid=target.namespace_uid,
        )
