"""Owner-declared approval policy: which plans may apply without a human hash.

The owner declares the policy in reviewed code, never on the command line:
``Pipeline(..., auto_approve=ApprovalPolicy(...))`` or ``[release]
auto_approve = {...}`` in a ``release.toml``. ``piceli deploy
--approve-if-policy`` and ``piceli release apply --approve-if-policy`` then
apply a plan without the human hash only when **every** action of the plan is
inside the policy; any other plan still needs the owner's approval of its
hash (``approval-policy-exceeded``, exit ``3``).

The policy can only narrow what applies automatically:

* ``delete``, ``replace`` and ``adopt`` actions are never inside a policy
  (:data:`ALWAYS_REVIEWED`); declaring them in ``allow`` is refused
  (``approval-policy-invalid``);
* ``cluster_scoped`` actions and ``drift`` (a desired field another manager
  wrote, which the apply overwrites) are outside unless ``allow`` names them;
* ``deny`` removes classes from ``allow``; ``max_objects`` caps the number of
  changed objects.

The policy is part of the plan hash (the release plan hash and the deploy
combined hash), so changing it makes earlier plans unapprovable.

Importing this module is side-effect free.

Example::

    from piceli.approval_policy import ApprovalPolicy

    policy = ApprovalPolicy(allow={"create", "apply", "no-op"}, max_objects=20)
    decision = policy.evaluate([{"operation": "delete", "kind": "Service", "name": "web"}])
    assert not decision.allowed
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

#: Action classes that always need the owner's approval of the hash.
ALWAYS_REVIEWED = frozenset({"delete", "replace", "adopt"})
#: Action classes an owner may allow to apply automatically.
ELIGIBLE = frozenset({"create", "apply", "no-op", "cluster_scoped", "drift"})
#: Every class a policy may name (in ``allow`` or ``deny``).
CLASSES = ELIGIBLE | ALWAYS_REVIEWED
#: ``allow`` when the policy does not name it.
DEFAULT_ALLOW = frozenset({"create", "apply", "no-op"})
#: Upper bound of ``max_objects``.
MAX_OBJECTS = 4096
SCHEMA = "piceli.approval-policy.v1"


class ApprovalPolicyError(ValueError):
    """The declared policy is invalid (``approval-policy-invalid``)."""

    code = "approval-policy-invalid"


def _classes(value: Any, name: str) -> frozenset[str]:
    if isinstance(value, str) or not isinstance(value, Iterable):
        raise ApprovalPolicyError(
            f"{name} must be a set of action classes, not {type(value).__name__}"
        )
    items = frozenset(value)
    unknown = sorted(str(item) for item in items if item not in CLASSES)
    if unknown:
        raise ApprovalPolicyError(
            f"{name} names unknown action classes {unknown}; known: {sorted(CLASSES)}"
        )
    return items


@dataclass(frozen=True)
class PolicyDecision:
    """Whether a plan is inside a policy, and why not."""

    policy: ApprovalPolicy
    violations: tuple[str, ...]
    changes: int

    @property
    def allowed(self) -> bool:
        return not self.violations

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "violations": list(self.violations),
            "changes": self.changes,
            "policy": self.policy.identity(),
        }


@dataclass(frozen=True)
class ApprovalPolicy:
    """Which plans may apply without the owner approving their hash.

    :param allow: Action classes that may apply automatically: ``create``,
        ``apply``, ``no-op``, ``cluster_scoped`` (a cluster-wide object such as
        a ClusterRole) and ``drift`` (overwrite fields another manager
        wrote). Default ``{"create", "apply", "no-op"}``. ``delete``,
        ``replace`` and ``adopt`` are never allowed.
    :param deny: Classes removed from ``allow`` (any class, for clarity).
    :param max_objects: At most this many changed (not ``no-op``) objects.
    :raises ApprovalPolicyError: an unknown class, an always-reviewed class
        in ``allow``, or an invalid ``max_objects``.
    """

    allow: frozenset[str] = field(default=DEFAULT_ALLOW)
    deny: frozenset[str] = field(default=frozenset())
    max_objects: int | None = None

    def __post_init__(self) -> None:
        allow = _classes(self.allow, "allow")
        deny = _classes(self.deny, "deny")
        widened = sorted(allow & ALWAYS_REVIEWED)
        if widened:
            raise ApprovalPolicyError(
                f"allow names {widened}, which always need the owner's approval "
                "of the plan hash; a policy can only narrow what applies "
                "automatically"
            )
        if self.max_objects is not None and (
            isinstance(self.max_objects, bool)
            or not isinstance(self.max_objects, int)
            or not 0 <= self.max_objects <= MAX_OBJECTS
        ):
            raise ApprovalPolicyError(
                f"max_objects must be an integer from 0 to {MAX_OBJECTS}"
            )
        object.__setattr__(self, "allow", allow)
        object.__setattr__(self, "deny", deny)

    @classmethod
    def from_value(cls, value: Any) -> ApprovalPolicy | None:
        """A policy from a declaration: a policy, a mapping (TOML) or ``None``."""
        if value is None or isinstance(value, ApprovalPolicy):
            return value
        if not isinstance(value, Mapping):
            raise ApprovalPolicyError(
                "auto_approve must be an ApprovalPolicy or a table with allow, "
                "deny and max_objects"
            )
        unknown = sorted(set(value) - {"allow", "deny", "max_objects"})
        if unknown:
            raise ApprovalPolicyError(f"auto_approve has unknown keys {unknown}")
        return cls(
            allow=value.get("allow", DEFAULT_ALLOW),
            deny=value.get("deny", ()),
            max_objects=value.get("max_objects"),
        )

    @classmethod
    def from_identity(cls, value: Mapping[str, Any]) -> ApprovalPolicy:
        """The policy an :meth:`identity` describes."""
        if value.get("schema") != SCHEMA:
            raise ApprovalPolicyError("unknown approval policy schema")
        return cls(
            allow=value["allow"], deny=value["deny"], max_objects=value["max_objects"]
        )

    @property
    def effective(self) -> frozenset[str]:
        """The classes that apply automatically: ``allow`` minus ``deny``."""
        return self.allow - self.deny

    def identity(self) -> dict[str, Any]:
        """The canonical form bound into plan hashes."""
        return {
            "schema": SCHEMA,
            "allow": sorted(self.allow),
            "deny": sorted(self.deny),
            "max_objects": self.max_objects,
        }

    def evaluate(
        self,
        actions: Iterable[Mapping[str, Any]],
        *,
        drift: Iterable[Mapping[str, Any]] = (),
    ) -> PolicyDecision:
        """Check a plan's actions (``operation``, ``kind``, ``name``,
        ``cluster_scoped``) and its drift (``kind``, ``name``, or a
        ``resource`` with them). ``no-op`` actions change nothing and are
        always inside; an unknown operation is always outside.
        """
        effective = self.effective
        violations: list[str] = []
        changes = 0
        for action in actions:
            operation = str(action.get("operation"))
            if operation == "no-op":
                continue  # changes nothing
            where = f"{action.get('kind')}/{action.get('name')}"
            changes += 1
            classes = [operation]
            if action.get("cluster_scoped"):
                classes.append("cluster_scoped")
            violations.extend(
                f"{name} {where}" for name in classes if name not in effective
            )
        if "drift" not in effective:
            for item in drift:
                resource = item.get("resource", item)
                violations.append(
                    f"drift {resource.get('kind')}/{resource.get('name')}"
                )
        if self.max_objects is not None and changes > self.max_objects:
            violations.append(
                f"max_objects: {changes} changed objects > {self.max_objects}"
            )
        return PolicyDecision(self, tuple(violations), changes)


__all__ = [
    "ALWAYS_REVIEWED",
    "CLASSES",
    "DEFAULT_ALLOW",
    "ELIGIBLE",
    "ApprovalPolicy",
    "ApprovalPolicyError",
    "PolicyDecision",
]
