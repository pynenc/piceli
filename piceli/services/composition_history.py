"""Browser projection of the controller's run history (``piceli.gitops-history.v1``).

The composition controller publishes each environment's recent deploy runs
in the ConfigMap ``piceli-gitops-history`` (:mod:`piceli.gitops.history`);
the in-cluster UI reads it with its ``get`` grant on that one ConfigMap (it
mounts no controller volume). This module keeps only documented fields, each
validated: commit ids, hashes, digests, object kinds and names, registered
codes, check names with their short detail line, timings and the bounded
build log tail the status already shows. Anything else is dropped.

Importing this module is side-effect free.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_NAME = re.compile(r"[A-Za-z0-9](?:[-A-Za-z0-9_.]{0,126}[A-Za-z0-9])?")
_SHA = re.compile(r"[0-9a-f]{7,64}")
_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_RUN = re.compile(r"[0-9A-Za-z][0-9A-Za-z_.@:-]{0,127}")
_STATES = {
    "deployed",
    "degraded",
    "approval-required",
    "failed",
    "retrying",
    "pending",
    "running",
    "deleting",
    "stopped",
}
_VIA = {"cli", "ui", "policy", "request"}
_TEXT = 256
_TAIL = 2000
_DETAIL = 300
_CHANGES = 50
_RUNS = 50


def _text(value: Any, limit: int = _TEXT) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return value


def _hash(value: Any) -> str | None:
    return value if isinstance(value, str) and _HASH.fullmatch(value) else None


def _names(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted(
        {item for item in value if isinstance(item, str) and _NAME.fullmatch(item)}
    )


def _dict(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sources(value: Any) -> list[dict[str, Any]]:
    found = []
    for name, item in sorted(_dict(value).items()):
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            continue
        item = _dict(item)
        commit = item.get("commit")
        found.append(
            {
                "name": name,
                "commit": commit
                if isinstance(commit, str) and _SHA.fullmatch(commit)
                else None,
                "ref": _text(item.get("ref")),
            }
        )
    return found


def _components(value: Any) -> list[dict[str, Any]]:
    found = []
    for item in value if isinstance(value, list) else []:
        item = _dict(item)
        name = item.get("name")
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            continue
        commit = item.get("commit")
        found.append(
            {
                "name": name,
                "source": _text(item.get("source")),
                "commit": commit
                if isinstance(commit, str) and _SHA.fullmatch(commit)
                else None,
                "digest": _hash(item.get("digest")),
                "state": _text(item.get("state"), 32) or "unknown",
                "built": item.get("built") is True,
            }
        )
    return found


def _plan(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    changes = [
        {
            "operation": _text(item.get("operation"), 32) or "unknown",
            "kind": _text(item.get("kind"), 64) or "unknown",
            "name": _text(item.get("name"), 253) or "unknown",
        }
        for item in (value.get("changes") or [])[:_CHANGES]
        if isinstance(item, Mapping)
    ]
    total = value.get("changes_total")
    return {
        "counts": {
            str(key): count
            for key, count in _dict(value.get("counts")).items()
            if isinstance(key, str) and type(count) is int
        },
        "changes": changes,
        "changes_total": total if type(total) is int else len(changes),
    }


def _checks(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    results = []
    for item in value.get("results") or []:
        if not isinstance(item, Mapping):
            continue
        results.append(
            {
                "name": _text(item.get("name"), 128) or "check",
                "type": _text(item.get("type"), 32),
                "passed": item.get("passed")
                if isinstance(item.get("passed"), bool)
                else None,
                "code": _text(item.get("code"), 64),
                "detail": _text(item.get("detail"), _DETAIL),
                "duration": _number(item.get("duration")),
            }
        )
    passed = value.get("passed")
    return {
        "passed": passed if isinstance(passed, bool) else None,
        "verification": value.get("verification") is True,
        "results": results,
    }


def _failure(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    tail = value.get("log_tail")
    failed = [
        {"name": _text(item.get("name"), 128), "code": _text(item.get("code"), 64)}
        for item in value.get("failed_checks") or []
        if isinstance(item, Mapping)
    ]
    found = {
        "stage": _text(value.get("stage"), 32),
        "reason": _text(value.get("reason"), 64),
        "message": _text(value.get("message"), 500),
        "failed_checks": failed,
        "log_tail": tail[-_TAIL:] if isinstance(tail, str) and tail else None,
        "kept_job": _text(value.get("kept_job"), 253),
    }
    return found if any(v for v in found.values()) else None


def _approval(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping) or value.get("via") not in _VIA:
        return None
    return {"via": value["via"], "at": _text(value.get("at"), 32)}


def _stages(value: Any) -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "state": _text(_dict(item).get("state"), 32) or "unknown",
            "seconds": _number(_dict(item).get("seconds")),
        }
        for name, item in _dict(value).items()
        if isinstance(name, str) and _NAME.fullmatch(name)
    ]


def run(value: Mapping[str, Any]) -> dict[str, Any] | None:
    """One run of the history document, validated field by field."""
    identity = value.get("id")
    if not isinstance(identity, str) or not _RUN.fullmatch(identity):
        return None
    run_id = value.get("run_id")
    state = value.get("state")
    verification = _dict(value.get("verification"))
    return {
        "id": identity,
        "run_id": run_id
        if isinstance(run_id, str) and _RUN.fullmatch(run_id)
        else None,
        "started_at": _text(value.get("started_at"), 40),
        "finished_at": _text(value.get("finished_at"), 40),
        "state": state if state in _STATES else "unknown",
        "action": _text(value.get("action"), 32),
        "reason": _text(value.get("reason"), 64),
        "trigger": _text(value.get("trigger")),
        "sources": _sources(value.get("sources")),
        "plan_hash": _hash(value.get("plan_hash")),
        "combined_hash": _hash(value.get("combined_hash")),
        "approved_by": _approval(value.get("approved_by")),
        "release": _text(value.get("release"), 128),
        "components": _components(value.get("components")),
        "built": _names(value.get("built")),
        "rolled": _names(value.get("rolled")),
        "unchanged": _names(value.get("unchanged")),
        "plan": _plan(value.get("plan")),
        "checks": _checks(value.get("checks")),
        "verification": {
            "state": _text(verification.get("state"), 32) or "unknown",
            "trigger": _text(verification.get("trigger"), 64),
            "checks_hash": _text(verification.get("checks_hash"), 80),
        }
        if verification
        else None,
        "failure": _failure(value.get("failure")),
        "stages": _stages(value.get("stages")),
        "seconds": _number(value.get("seconds")),
        "recorded_by": value.get("recorded_by")
        if value.get("recorded_by") in {"controller", "run", "controller+run"}
        else None,
    }


def environment_runs(
    document: Mapping[str, Any] | None, env: str
) -> list[dict[str, Any]]:
    """``env``'s validated runs of ``document``, newest first (bounded)."""
    entry = _dict(_dict(_dict(document).get("envs")).get(env))
    runs = [
        item
        for item in (run(_dict(value)) for value in (entry.get("runs") or [])[:_RUNS])
        if item is not None
    ]
    runs.sort(key=lambda item: item["started_at"] or "", reverse=True)
    return runs


def published(document: Mapping[str, Any] | None) -> dict[str, Any]:
    """Whether the controller published a history and its freshness."""
    ok = (
        isinstance(document, Mapping)
        and document.get("schema") == "piceli.gitops-history.v1"
    )
    return {
        "available": ok,
        "generated_at": _text(_dict(document).get("generated_at"), 40) if ok else None,
        "truncated": _dict(document).get("truncated") is True if ok else False,
    }
