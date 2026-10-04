"""The safety gate of a client bundle: what a foreign cluster can expect.

A bundle is installed by someone else, in a cluster Piceli never sees, so it
must be safe to apply without reading every manifest. :func:`check` looks at
every object of the bundle and reports each break of a rule of
:data:`piceli.bundle.rules.RULES`:

``non-root``
    Every container (init containers and sidecars included) runs as a
    non-root user: ``runAsNonRoot: true`` or a non-zero ``runAsUser``
    (container or pod level), never ``runAsUser: 0``.
``read-only-root``
    Every container sets ``readOnlyRootFilesystem: true``.
``no-privilege``
    No ``privileged`` container, ``allowPrivilegeEscalation`` is ``false``,
    no added capability other than ``NET_BIND_SERVICE``, no host network,
    PID or IPC namespace and no ``hostPath`` volume.
``resources``
    Every container requests and limits both ``cpu`` and ``memory``.
``network``
    No NetworkPolicy of the app admits ingress from outside the namespace
    (a rule without ``from``, a ``namespaceSelector`` or an ``ipBlock``).
    The bundle adds its own default-deny policy; egress the app declares is
    allowed and listed in ``INSTALL.md``.
``rbac``
    No ``*`` in a role; no ``escalate``, ``bind`` or ``impersonate``; a
    ClusterRole only reads (``get``, ``list``, ``watch``) and never Secrets;
    bindings only name roles of the bundle (never ``cluster-admin``,
    ``admin`` or ``edit``) and only ServiceAccounts as subjects.
``node-port``
    No ``NodePort`` or ``LoadBalancer`` Service and no ``hostPort``.

An object opts out of one rule with the annotation
``safety.piceli.io/allow-<rule>: <reason>`` (``app.safety_exception``);
:func:`exceptions` lists them for the bundle's manifest and install guide.

Pure functions; importing this module is side-effect free.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from piceli.bundle.rules import CODES, RULES, SAFETY_ANNOTATION_PREFIX
from piceli.pipeline.compose import containers, pod_specs

_READ_VERBS = frozenset({"get", "list", "watch"})
_FORBIDDEN_VERBS = frozenset({"escalate", "bind", "impersonate", "*"})
_ALLOWED_CAPABILITIES = frozenset({"NET_BIND_SERVICE"})
_RBAC = "rbac.authorization.k8s.io"


@dataclass(frozen=True)
class Violation:
    """One object breaking one rule (no values, only names)."""

    kind: str
    name: str
    rule: str
    detail: str

    @property
    def object(self) -> str:
        return f"{self.kind}/{self.name}"

    @property
    def code(self) -> str:
        return CODES[self.rule]

    def public(self) -> dict[str, str]:
        return {
            "object": self.object,
            "rule": self.rule,
            "code": self.code,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class SafetyException:
    """A rule an object opts out of, with the app's reason."""

    kind: str
    name: str
    rule: str
    reason: str

    def public(self) -> dict[str, str]:
        return {
            "object": f"{self.kind}/{self.name}",
            "rule": self.rule,
            "reason": self.reason,
        }


def _metadata(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    value = manifest.get("metadata")
    return value if isinstance(value, Mapping) else {}


def _identity(manifest: Mapping[str, Any]) -> tuple[str, str]:
    return str(manifest.get("kind") or "?"), str(_metadata(manifest).get("name") or "?")


def _waived(manifest: Mapping[str, Any]) -> dict[str, str]:
    annotations = _metadata(manifest).get("annotations")
    if not isinstance(annotations, Mapping):
        return {}
    return {
        key.removeprefix(SAFETY_ANNOTATION_PREFIX): str(value)
        for key, value in annotations.items()
        if isinstance(key, str) and key.startswith(SAFETY_ANNOTATION_PREFIX)
    }


def exceptions(objects: Iterable[Mapping[str, Any]]) -> list[SafetyException]:
    """Every declared opt-out, sorted; unknown rule names are left out."""
    found = []
    for manifest in objects:
        kind, name = _identity(manifest)
        for rule, reason in _waived(manifest).items():
            if rule in RULES:
                found.append(SafetyException(kind, name, rule, reason))
    return sorted(found, key=lambda item: (item.kind, item.name, item.rule))


# ------------------------------------------------------------------ pods


def _context(item: Mapping[str, Any], key: str) -> Any:
    context = item.get("securityContext")
    return context.get(key) if isinstance(context, Mapping) else None


def _container_label(container: Mapping[str, Any]) -> str:
    return f"container {container.get('name')!r}"


def _pod_rules(pod: Mapping[str, Any]) -> Iterator[tuple[str, str]]:
    pod_non_root = _context(pod, "runAsNonRoot")
    pod_user = _context(pod, "runAsUser")
    if pod.get("hostNetwork") or pod.get("hostPID") or pod.get("hostIPC"):
        yield "no-privilege", "uses the node's network, PID or IPC namespace"
    for volume in pod.get("volumes") or ():
        if isinstance(volume, Mapping) and "hostPath" in volume:
            yield "no-privilege", f"mounts node directory volume {volume.get('name')!r}"
    for container in containers(pod):
        label = _container_label(container)
        user = _context(container, "runAsUser")
        non_root = _context(container, "runAsNonRoot")
        effective_user = user if user is not None else pod_user
        effective_non_root = non_root if non_root is not None else pod_non_root
        if effective_user == 0 or not (
            effective_non_root is True
            or (isinstance(effective_user, int) and effective_user > 0)
        ):
            yield (
                "non-root",
                f"{label} may run as root (set runAsNonRoot or a non-zero runAsUser)",
            )
        if _context(container, "readOnlyRootFilesystem") is not True:
            yield "read-only-root", f"{label} has a writable root filesystem"
        if _context(container, "privileged") is True:
            yield "no-privilege", f"{label} is privileged"
        if _context(container, "allowPrivilegeEscalation") is not False:
            yield (
                "no-privilege",
                f"{label} does not set allowPrivilegeEscalation: false",
            )
        capabilities = _context(container, "capabilities")
        added = capabilities.get("add") if isinstance(capabilities, Mapping) else None
        extra = sorted(set(added or ()) - _ALLOWED_CAPABILITIES)
        if extra:
            yield "no-privilege", f"{label} adds capabilities {extra}"
        resources = container.get("resources")
        resources = resources if isinstance(resources, Mapping) else {}
        missing = [
            f"{part}.{resource}"
            for part in ("requests", "limits")
            for resource in ("cpu", "memory")
            if not (resources.get(part) or {}).get(resource)
        ]
        if missing:
            yield "resources", f"{label} has no {', '.join(missing)}"
        for port in container.get("ports") or ():
            if isinstance(port, Mapping) and port.get("hostPort"):
                yield "node-port", f"{label} binds a host port"


# --------------------------------------------------------------- objects


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _service_rules(manifest: Mapping[str, Any]) -> Iterator[tuple[str, str]]:
    spec = _mapping(manifest.get("spec"))
    kind = spec.get("type") or "ClusterIP"
    if kind in {"NodePort", "LoadBalancer"} or any(
        isinstance(port, Mapping) and port.get("nodePort")
        for port in spec.get("ports") or ()
    ):
        yield "node-port", f"is a {kind} Service that opens a port on the nodes"


def _network_rules(manifest: Mapping[str, Any]) -> Iterator[tuple[str, str]]:
    spec = _mapping(manifest.get("spec"))
    types = spec.get("policyTypes") or ["Ingress"]
    if "Ingress" not in types:
        return
    for rule in spec.get("ingress") or ():
        peers = (rule or {}).get("from") if isinstance(rule, Mapping) else None
        if not peers:
            yield "network", "admits ingress from any source"
            continue
        for peer in peers:
            if not isinstance(peer, Mapping):
                continue
            if "ipBlock" in peer:
                yield "network", "admits ingress from an IP range"
            elif "namespaceSelector" in peer:
                yield "network", "admits ingress from other namespaces"


def _role_rules(manifest: Mapping[str, Any]) -> Iterator[tuple[str, str]]:
    cluster = manifest.get("kind") == "ClusterRole"
    if manifest.get("aggregationRule"):
        yield "rbac", "aggregates other roles"
    for rule in manifest.get("rules") or ():
        if not isinstance(rule, Mapping):
            continue
        verbs = {str(item) for item in rule.get("verbs") or ()}
        resources = {str(item) for item in rule.get("resources") or ()}
        groups = {str(item) for item in rule.get("apiGroups") or ()}
        if "*" in verbs | resources | groups or "*" in (
            rule.get("resourceNames") or ()
        ):
            yield "rbac", "grants a wildcard"
        forbidden = sorted(verbs & (_FORBIDDEN_VERBS - {"*"}))
        if forbidden:
            yield "rbac", f"grants {forbidden}"
        if rule.get("nonResourceURLs"):
            yield "rbac", "grants non-resource URLs"
        if cluster and verbs - _READ_VERBS:
            yield "rbac", f"grants cluster-wide writes {sorted(verbs - _READ_VERBS)}"
        if cluster and "" in groups and resources & {"secrets", "*"}:
            yield "rbac", "reads Secrets cluster-wide"


def _binding_rules(
    manifest: Mapping[str, Any], roles: set[tuple[str, str]]
) -> Iterator[tuple[str, str]]:
    ref = _mapping(manifest.get("roleRef"))
    target = (str(ref.get("kind")), str(ref.get("name")))
    if target not in roles:
        yield (
            "rbac",
            f"binds {target[0]} {target[1]!r}, which the bundle does not declare",
        )
    for subject in manifest.get("subjects") or ():
        if isinstance(subject, Mapping) and subject.get("kind") != "ServiceAccount":
            yield "rbac", f"binds a {subject.get('kind')} subject"


def check(objects: Sequence[Mapping[str, Any]]) -> list[Violation]:
    """Every break of a rule that the object does not waive, in object order."""
    roles = {
        _identity(item)
        for item in objects
        if item.get("kind") in {"Role", "ClusterRole"}
        and str(item.get("apiVersion", "")).startswith(_RBAC)
    }
    violations: list[Violation] = []
    for manifest in objects:
        kind, name = _identity(manifest)
        found: list[tuple[str, str]] = []
        for pod in pod_specs(manifest):
            found.extend(_pod_rules(pod))
        if kind == "Service":
            found.extend(_service_rules(manifest))
        elif kind == "NetworkPolicy":
            found.extend(_network_rules(manifest))
        elif kind in {"Role", "ClusterRole"}:
            found.extend(_role_rules(manifest))
        elif kind in {"RoleBinding", "ClusterRoleBinding"}:
            found.extend(_binding_rules(manifest, roles))
        waived = _waived(manifest)
        seen: set[tuple[str, str]] = set()
        for rule, detail in found:
            if rule in waived or (rule, detail) in seen:
                continue
            seen.add((rule, detail))
            violations.append(Violation(kind, name, rule, detail))
    return violations


def first_code(violations: Sequence[Violation]) -> str:
    """The code to refuse with: the first violated rule in :data:`RULES` order."""
    order = list(RULES)
    return min(violations, key=lambda item: order.index(item.rule)).code
