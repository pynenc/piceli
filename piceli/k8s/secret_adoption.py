"""Keep generated secret values that an earlier release already wrote.

Generated Secrets are retained: Piceli never rewrites them. When the release
state has no record of a generator (a new or emptied state directory, a GitOps
controller that moved to another node) but its values are live in Secrets an
earlier release of the same app wrote, the plan **adopts** them: the values
are read from the cluster and recorded in the state exactly as if they had
been generated, so nothing is rotated and nothing is deleted.

A live Secret is a source only when it carries what Piceli writes on the
objects of this release: the ``piceli.io/owner`` annotation equal to the
release owner (never an inherited owner), a ``managedFields`` entry of the
release's field manager, and it lives in the target namespace (discovery is
namespaced). Anything else is not adopted and is refused as before.

Adoption applies to the generators that make values themselves (``random``,
``tls-self-signed``, ``tls-ca``) and only when no catalogued release records
the generator. The live values must meet the generator's constraints, or the
plan is refused with ``secret-adoption-invalid``; a generator only partly
present in live Secrets is refused with ``secret-adoption-incomplete``.

A ``tls-ca`` CA key is internal: it is never written to a Secret, so an
adopted CA has no key. Its certificate and every leaf are kept; signing a new
leaf later needs a rotation of the CA.
"""

from __future__ import annotations

import base64
import binascii
import math
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from piceli.k8s.ops.plan import (
    DeploymentComposition,
    ObservedResource,
    ObservedSnapshot,
    Ownership,
    ResourceRef,
    SecretAdoption,
)
from piceli.k8s.ops.secret_versions import SecretVersionRef, pointer_parts
from piceli.k8s.release_secret_spec import (
    GeneratorSpec,
    Output,
    RandomSecretSpec,
    SecretError,
    TlsCaSpec,
    TlsSelfSignedSpec,
    outputs,
)

__all__ = [
    "ADOPTABLE",
    "LiveSecrets",
    "owned_by_release",
]

#: Generators whose values Piceli made itself, so they can be adopted back.
ADOPTABLE = (RandomSecretSpec, TlsSelfSignedSpec, TlsCaSpec)

_URLSAFE = re.compile(r"[A-Za-z0-9_-]+")
_CERTIFICATE = b"-----BEGIN CERTIFICATE-----"
_KEY = re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_RELEASE_NAMESPACE = "piceli.io/namespace"


def owned_by_release(
    observed: ObservedResource, *, owner: str, field_manager: str, namespace: str
) -> bool:
    """Whether a live object carries this release's own ownership markers."""
    annotations = observed.intent.manifest.get("metadata", {}).get("annotations")
    marked = (
        annotations.get(_RELEASE_NAMESPACE, namespace)
        if isinstance(annotations, Mapping)
        else namespace
    )
    return (
        observed.ownership is Ownership.MANAGED
        and observed.retained
        and observed.owner == owner
        and marked == namespace
        and any(entry.manager == field_manager for entry in observed.field_managers)
    )


@dataclass(frozen=True)
class _Source:
    """One live Secret key holding one generator output."""

    resource: ResourceRef
    uid: str
    value: str | None = field(repr=False)
    problem: str | None = None


def _value_at(pointer: str, data: Mapping[str, Any]) -> str | None:
    """The value a release placed at ``pointer``, read from live ``data``.

    ``/data/KEY`` holds the encoded value as written; ``/stringData/KEY`` was
    stored base64-encoded by the API server, so it is decoded back to text.
    """
    parts = pointer_parts(pointer)
    if len(parts) != 2 or parts[0] not in {"data", "stringData"}:
        return None
    value = data.get(parts[1])
    if not isinstance(value, str):
        return None
    if parts[0] == "data":
        return value
    try:
        return base64.b64decode(value, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None


class LiveSecrets:
    """Generator outputs found in live Secrets this release owns.

    ``recorded`` names the generators some catalogued release already
    records: those are never adopted (their values come from the state, or a
    changed generator makes new ones). Calling the object adopts one
    generator (see :func:`piceli.k8s.release_secrets.materialize`).
    """

    def __init__(
        self,
        composition: DeploymentComposition,
        snapshot: ObservedSnapshot,
        inputs: Mapping[str, SecretVersionRef],
        *,
        owner: str,
        field_manager: str,
        recorded: Iterable[str],
    ) -> None:
        self.recorded = frozenset(recorded)
        self.adopted: dict[str, tuple[_Source, ...]] = {}
        by_ref = {ref: name for name, ref in inputs.items()}
        observed = {item.intent.ref: item for item in snapshot.resources}
        incomplete = set(snapshot.incomplete_content)
        namespace = snapshot.target.namespace
        self._sources: dict[str, list[_Source]] = {}
        for component in composition.components:
            for resource in component.resources:
                ref = resource.ref
                current = observed.get(ref)
                if (
                    (ref.api_version, ref.kind) != ("v1", "Secret")
                    or current is None
                    or ref in incomplete
                    or not owned_by_release(
                        current,
                        owner=owner,
                        field_manager=field_manager,
                        namespace=namespace,
                    )
                ):
                    continue
                bound = [
                    (binding.json_pointer, by_ref[binding.reference])
                    for binding in resource.secret_bindings
                    if binding.reference in by_ref
                ]
                if not bound:
                    continue
                desired = resource.manifest
                declared = set(desired.get("data") or {}) | set(
                    desired.get("stringData") or {}
                )
                data = current.intent.manifest.get("data") or {}
                problem = (
                    None
                    if set(data) == declared
                    else f"live Secret {ref.name!r} has keys {sorted(data)}, the "
                    f"release declares {sorted(declared)}"
                )
                for pointer, output in bound:
                    self._sources.setdefault(output, []).append(
                        _Source(
                            ref,
                            current.precondition.uid or "",
                            _value_at(pointer, data),
                            problem,
                        )
                    )

    def candidates(self, secrets: Mapping[str, GeneratorSpec]) -> dict[str, list[str]]:
        """Generators that would be adopted, with their Secrets (no value read)."""
        result: dict[str, list[str]] = {}
        for name, spec in sorted(secrets.items()):
            if name in self.recorded or not isinstance(spec, ADOPTABLE):
                continue
            names = sorted(
                {
                    source.resource.name
                    for item in outputs(name, spec)
                    for source in self._sources.get(item.name, ())
                }
            )
            if names:
                result[name] = names
        return result

    def __call__(
        self, name: str, spec: GeneratorSpec, items: tuple[Output, ...]
    ) -> dict[str, str] | None:
        """The encoded live values of every exposed output, or ``None``.

        ``None`` when the generator is recorded, not adoptable, or none of its
        outputs is live. Refused when only some are live
        (``secret-adoption-incomplete``) or a value breaks the generator's
        constraints (``secret-adoption-invalid``).
        """
        if name in self.recorded or not isinstance(spec, ADOPTABLE):
            return None
        exposed = [item.name for item in items if not item.internal]
        found = {output: self._sources.get(output, []) for output in exposed}
        if not any(found.values()):
            return None
        missing = sorted(output for output, sources in found.items() if not sources)
        if missing:
            raise SecretError(
                "secret-adoption-incomplete",
                f"secret {name!r} is in live Secrets of an earlier release but "
                f"{missing} is not, so its values cannot be kept: rotate it "
                f"(--rotate {name}) into Secrets with new names",
            )
        values: dict[str, str] = {}
        for output, sources in found.items():
            for source in sources:
                if source.problem is not None:
                    raise SecretError(
                        "secret-adoption-invalid",
                        f"cannot adopt secret {name!r}: {source.problem}",
                    )
                if source.value is None:
                    raise SecretError(
                        "secret-adoption-invalid",
                        f"cannot adopt secret {name!r}: live Secret "
                        f"{source.resource.name!r} holds no readable value for "
                        f"{output!r}",
                    )
            distinct = {source.value for source in sources}
            if len(distinct) != 1:
                raise SecretError(
                    "secret-adoption-invalid",
                    f"cannot adopt secret {name!r}: live Secrets "
                    f"{sorted(s.resource.name for s in sources)} hold different "
                    f"values for {output!r}",
                )
            values[output] = str(distinct.pop())
        _check(name, spec, values)
        self.adopted[name] = tuple(
            source for output in exposed for source in found[output]
        )
        return values

    def evidence(self) -> tuple[SecretAdoption, ...]:
        """What was adopted, one entry per generator and Secret (plan-bound)."""
        return tuple(
            sorted(
                {
                    SecretAdoption(name, source.resource, source.uid)
                    for name, sources in self.adopted.items()
                    for source in sources
                }
            )
        )


def _check(name: str, spec: GeneratorSpec, values: Mapping[str, str]) -> None:
    """Refuse live values this generator could not have produced."""

    def refuse(output: str, reason: str) -> None:
        raise SecretError(
            "secret-adoption-invalid",
            f"cannot adopt secret {name!r}: {output!r} {reason}",
        )

    raw: dict[str, bytes] = {}
    for output, value in values.items():
        try:
            raw[output] = (
                base64.b64decode(value, validate=True)
                if spec.encoding == "base64"
                else value.encode()
            )
        except (binascii.Error, ValueError):
            refuse(output, "is not valid base64")
    checks: dict[str, Callable[[bytes], bool]] = {}
    if isinstance(spec, RandomSecretSpec):
        length = math.ceil(spec.bytes * 4 / 3)
        checks[name] = lambda value: (
            len(value) == length
            and _URLSAFE.fullmatch(value.decode("ascii", "replace")) is not None
        )
    else:
        for item in outputs(name, spec):
            if item.internal:
                continue
            if item.name.endswith(".crt"):
                checks[item.name] = lambda value: value.lstrip().startswith(
                    _CERTIFICATE
                )
            else:
                checks[item.name] = lambda value: _KEY.search(value) is not None
    for output, check in checks.items():
        if not check(raw[output]):
            refuse(output, f"does not look like a value of a {spec.type} generator")
