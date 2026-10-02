"""A fake API server that enforces RBAC rules, for the controller's grants.

:class:`RbacFakeAPI` answers ``403`` to every request the given rules do not
allow, and records it, so a test shows which request the rules lack. The
rules are evaluated like the API server's RBAC authorizer for the subset
Piceli uses: API group, resource (``resource/subresource``), verb and
``resourceNames``. Cluster rules apply everywhere; namespace rules only in
their namespace.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from piceli.testing import FakeAPI

Rules = Sequence[Mapping[str, Any]]


def attributes(request: Mapping[str, Any]) -> dict[str, Any] | None:
    """``{verb, group, resource, namespace, name}`` of a resource request."""
    parts = [part for part in request["path"].split("/") if part]
    if not parts or parts[0] not in {"api", "apis"}:
        return None
    rest = parts[2:] if parts[0] == "api" else parts[3:]
    group = "" if parts[0] == "api" else parts[1]
    if not rest:
        return None  # discovery
    namespace = None
    if rest[0] == "namespaces" and len(rest) >= 3:
        namespace, rest = rest[1], rest[2:]
    resource = rest[0]
    name = rest[1] if len(rest) > 1 else None
    if len(rest) > 2:
        resource = f"{resource}/{rest[2]}"
    method = request["method"]
    if method == "GET":
        watch = (request.get("query") or {}).get("watch") in (["true"], ["1"])
        verb = "get" if name else ("watch" if watch else "list")
    else:
        verb = {
            "POST": "create",
            "PATCH": "patch",
            "PUT": "update",
            "DELETE": "delete" if name else "deletecollection",
        }[method]
    return {
        "verb": verb,
        "group": group,
        "resource": resource,
        "namespace": namespace,
        "name": name,
    }


def allows(rules: Iterable[Mapping[str, Any]], wanted: Mapping[str, Any]) -> bool:
    for rule in rules:
        names = rule.get("resourceNames")
        if (
            wanted["group"] in rule.get("apiGroups", ())
            and wanted["resource"] in rule.get("resources", ())
            and wanted["verb"] in rule.get("verbs", ())
            and (not names or wanted["name"] in names)
        ):
            return True
    return False


class RbacFakeAPI(FakeAPI):
    """:class:`FakeAPI` answering ``403`` to what ``cluster_rules`` and
    ``namespace_rules`` (``{namespace: rules}``) do not allow."""

    def __init__(
        self,
        cluster_rules: Rules,
        namespace_rules: Mapping[str, Rules],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.cluster_rules = list(cluster_rules)
        self.namespace_rules = {k: list(v) for k, v in namespace_rules.items()}
        self.denied: list[dict[str, Any]] = []

    def route(self, request: dict[str, Any]) -> tuple[int, Any]:
        wanted = attributes(request)
        if wanted is not None:
            local = self.namespace_rules.get(wanted["namespace"] or "", [])
            if not allows(self.cluster_rules, wanted) and not (
                wanted["namespace"] and allows(local, wanted)
            ):
                self.denied.append(wanted)
                return 403, {"kind": "Status", "reason": "Forbidden"}
        return super().route(request)
