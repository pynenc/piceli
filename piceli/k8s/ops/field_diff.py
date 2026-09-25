"""Field-level diffs of a plan: what each change does to the live object.

A diff is evidence for people and agents, not part of the plan: it is derived
from the plan and the snapshot it was built from, and it is never included in
the plan hash. Each changed object gets

* ``changes``: JSON-patch style operations, ``{path, op, before, after}``
  with ``path`` a JSON pointer into the object and ``op`` one of ``add``,
  ``remove``, ``replace`` (``before``/``after`` are ``null`` when absent);
* ``unified``: a unified text diff of the object as YAML;
* ``basis``: ``server-dry-run`` when the "after" side is what the API server
  answered to a dry run of the write (defaults included), or ``client`` when
  it is the desired manifest merged onto the live object locally;
* ``not_compared``: JSON pointers bound to secret versions. Their values are
  never shown (whether they match the live object is decided privately, see
  :class:`~piceli.k8s.ops.plan.PrivateEvidence`).

Keys an earlier release declared and the composition dropped (three-way
removal, :attr:`~piceli.k8s.ops.plan.PlanAction.removals`) appear as
``remove`` changes.

Values are redacted with the same rules as public plans and discovery
(:func:`~piceli.k8s.ops.discovery.public_manifest`): a secret value is shown as
``"<redacted>"``, never printed.

This module is pure: no Kubernetes client, no I/O.
"""

from __future__ import annotations

import copy
import difflib
import json
import re
from collections.abc import Mapping
from typing import Any

from piceli.k8s.ops.discovery import public_manifest
from piceli.k8s.ops.plan import (
    DeploymentPlan,
    ObservedSnapshot,
    PlanAction,
    PlanOperation,
    ResourceIntent,
    normalized_manifest,
    removal_patch,
    usable_dry_run,
    without_removed_fields,
)
from piceli.k8s.ops.secret_versions import replace_pointer

REDACTED = "<redacted>"
BASIS_SERVER = "server-dry-run"
BASIS_CLIENT = "client"

#: Operations that change an existing object and so get a field diff.
DIFFED_OPERATIONS = frozenset(
    {PlanOperation.APPLY, PlanOperation.ADOPT, PlanOperation.REPLACE}
)


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _walk(before: Any, after: Any, pointer: str, out: list[dict[str, Any]]) -> None:
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after)):
            path = f"{pointer}/{_escape(str(key))}"
            if key not in after:
                out.append({"path": path, "op": "remove", "before": before[key]})
            elif key not in before:
                out.append({"path": path, "op": "add", "after": after[key]})
            else:
                _walk(before[key], after[key], path, out)
    elif isinstance(before, list) and isinstance(after, list):
        common = min(len(before), len(after))
        for index in range(common):
            _walk(before[index], after[index], f"{pointer}/{index}", out)
        for index in range(common, len(after)):
            out.append(
                {"path": f"{pointer}/{index}", "op": "add", "after": after[index]}
            )
        for index in range(common, len(before)):
            out.append(
                {"path": f"{pointer}/{index}", "op": "remove", "before": before[index]}
            )
    elif before != after or type(before) is not type(after):
        out.append({"path": pointer, "op": "replace", "before": before, "after": after})


def _lookup(value: Any, pointer: str) -> Any:
    for token in pointer.split("/")[1:]:
        token = token.replace("~1", "/").replace("~0", "~")
        value = value[int(token)] if isinstance(value, list) else value[token]
    return value


def field_changes(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    private_pointers: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """JSON-patch style changes from ``before`` to ``after``, redacted.

    Values at ``private_pointers`` (secret bindings) are masked on both sides
    first, so they are never compared. Every other value is shown as the
    public redaction shows it. Lists are compared by position.
    """
    masked_before = _masked(before, private_pointers)
    masked_after = _masked(after, private_pointers)
    public_before = public_manifest(masked_before)[0]
    public_after = public_manifest(masked_after)[0]
    raw: list[dict[str, Any]] = []
    _walk(masked_before, masked_after, "", raw)
    changes = []
    for change in raw:
        path = change["path"]
        changes.append(
            {
                "path": path,
                "op": change["op"],
                "before": _lookup(public_before, path)
                if change["op"] != "add"
                else None,
                "after": _lookup(public_after, path)
                if change["op"] != "remove"
                else None,
            }
        )
    return changes


def _masked(manifest: Mapping[str, Any], pointers: tuple[str, ...]) -> dict[str, Any]:
    value = copy.deepcopy(dict(manifest))
    for pointer in pointers:
        try:
            replace_pointer(value, pointer, REDACTED)
        except ValueError:
            continue
    return value


def unified_diff(
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    *,
    label: str,
    private_pointers: tuple[str, ...] = (),
) -> str:
    """A unified diff of the redacted objects rendered as YAML."""
    import yaml

    def lines(value: Mapping[str, Any] | None) -> list[str]:
        if value is None:
            return []
        public = public_manifest(_masked(value, private_pointers))[0]
        return yaml.safe_dump(
            public, sort_keys=True, default_flow_style=False, width=1000
        ).splitlines(keepends=True)

    return "".join(
        difflib.unified_diff(
            lines(before),
            lines(after),
            fromfile=f"live/{label}",
            tofile=f"release/{label}",
        )
    )


def merge_patch(target: Any, patch: Any) -> Any:
    """RFC 7386 JSON merge patch (what a same-owner ``apply`` sends)."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    result = copy.deepcopy(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = merge_patch(result.get(key), value)
    return result


def _label(intent: ResourceIntent) -> str:
    return f"{intent.ref.kind}/{intent.ref.name}"


def action_diff(
    action: PlanAction, snapshot: ObservedSnapshot
) -> dict[str, Any] | None:
    """The field diff of one action, or ``None`` when it changes nothing."""
    if action.operation not in DIFFED_OPERATIONS:
        return None
    current = next(
        (item for item in snapshot.resources if item.intent.ref == action.resource.ref),
        None,
    )
    if current is None:
        return None
    before = current.intent.manifest
    evidence = (
        usable_dry_run(action.resource, snapshot.server_dry_run(action.resource.ref))
        if action.operation is PlanOperation.APPLY
        else None
    )
    if evidence is not None:
        # The dry run carries no removals; the write's nulls delete exactly
        # these keys from what the server would store.
        after = without_removed_fields(
            normalized_manifest(evidence.manifest), action.removals
        )
        basis = BASIS_SERVER
    else:
        after = normalized_manifest(
            merge_patch(
                before, removal_patch(action.resource.manifest, action.removals)
            )
        )
        basis = BASIS_CLIENT
    private = tuple(
        sorted(binding.json_pointer for binding in action.resource.secret_bindings)
    )
    return {
        "resource": action.resource.ref.__dict__,
        "operation": action.operation.value,
        "basis": basis,
        "changes": field_changes(before, after, private_pointers=private),
        "not_compared": list(private),
        "unified": unified_diff(
            before, after, label=_label(action.resource), private_pointers=private
        ),
    }


def plan_diffs(
    plan: DeploymentPlan, snapshot: ObservedSnapshot
) -> list[dict[str, Any]]:
    """Field diffs of every action that changes an existing object, in plan order."""
    if plan.snapshot_hash != snapshot.snapshot_hash:
        raise ValueError("plan was not built from this snapshot")
    diffs = []
    for action in plan.actions:
        diff = action_diff(action, snapshot)
        if diff is not None:
            diffs.append(diff)
    return diffs


#: A human change line longer than this puts its values on their own lines.
CHANGE_LINE_WIDTH = 100
_DIGEST_HEX = re.compile(r"(sha256:)([0-9a-f]{12})[0-9a-f]{46}([0-9a-f]{6})")


def short_value(value: Any, width: int = 60) -> str:
    """``value`` as JSON, shortened in the middle to at most ``width`` characters.

    A ``sha256:`` digest first becomes ``sha256:<12 hex>…<6 hex>``; anything
    still too long keeps its head and a longer tail (the tag or digest end
    and the closing quote or bracket), so a value stays recognisable and is
    never mistaken for a complete one.
    """
    encoded = json.dumps(value, sort_keys=True)
    if len(encoded) <= width:
        return encoded
    encoded = _DIGEST_HEX.sub(r"\1\2…\3", encoded)
    if len(encoded) <= width:
        return encoded
    keep = max(width - 1, 2)
    head = keep * 2 // 5
    return encoded[:head] + "…" + encoded[len(encoded) - (keep - head) :]


def describe_change(
    change: Mapping[str, Any], width: int = 60, indent: str = ""
) -> str:
    """A human line for a change: ``~ /spec/replicas: 1 -> 2``.

    When the line would exceed :data:`CHANGE_LINE_WIDTH`, the values move to
    their own lines, each prefixed with ``indent`` (the caller's indentation).
    """
    symbol = {"add": "+", "remove": "-", "replace": "~"}[change["op"]]
    head = f"{symbol} {change['path']}:"
    if change["op"] in {"add", "remove"}:
        value = short_value(
            change["after"] if change["op"] == "add" else change["before"], width
        )
        line = f"{head} {value}"
        return (
            line if len(line) <= CHANGE_LINE_WIDTH else f"{head}\n{indent}    {value}"
        )
    before, after = (
        short_value(change["before"], width),
        short_value(change["after"], width),
    )
    line = f"{head} {before} -> {after}"
    if len(line) <= CHANGE_LINE_WIDTH:
        return line
    return f"{head}\n{indent}    {before}\n{indent} -> {after}"
