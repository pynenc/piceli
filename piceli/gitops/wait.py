"""What coding agents read from a GitOps status (0.17.0).

:func:`summary` is the compact view ``piceli gitops status --json`` adds
under ``summary``: per environment its state, commit, running stage,
attempt, next retry and last error. :func:`outcome` decides, from one status
document, whether ``piceli gitops wait ENV COMMIT`` is over and how.

Both are pure: they read a status document (``piceli.gitops-status.v1``),
from any controller version (fields a version lacks are ``None``).

Importing this module is side-effect free.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

WAIT_SCHEMA = "piceli.gitops-wait.v1"
#: A commit the wait accepts: 7 to 40 (or 64) lowercase hex characters.
COMMIT = re.compile(r"[0-9a-f]{7,64}")
#: Outcomes that end a wait, besides ``timed-out``.
FINAL = ("deployed", "failed", "approval-required", "superseded", "stopped")
#: Polls in a row that failed to read Git before a wait for a commit the
#: controller has not seen fails (one failed poll may be a network blip).
GIT_FAILED_POLLS = 2


def _iso(value: Any) -> str | None:
    if isinstance(value, int | float):
        return datetime.fromtimestamp(float(value), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return value if isinstance(value, str) else None


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def last_error(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """The reason, failed stage and failure detail of the last failed step."""
    reason = record.get("reason")
    if not reason or record.get("state") in {"deployed", "approval-required"}:
        return None
    return {
        "reason": reason,
        "stage": record.get("failed_stage"),
        "failure": record.get("failure"),
        "at": record.get("updated_at"),
    }


def summary(document: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Per environment: ``state``, ``commit``, ``revision``, ``stage``,
    ``attempt``, ``next_retry_at``, ``last_error``, ``deployed_commit``,
    ``failed_checks`` (0.19.0)."""
    found: dict[str, dict[str, Any]] = {}
    for name, value in sorted(_dict(_dict(document).get("envs")).items()):
        record = _dict(value)
        progress = _dict(record.get("in_progress"))
        found[name] = {
            "state": record.get("state"),
            "commit": record.get("commit"),
            "revision": record.get("revision"),
            "in_progress": progress.get("action"),
            "stage": progress.get("stage"),
            "since": progress.get("stage_since") or progress.get("since"),
            "attempt": record.get("attempts"),
            "next_retry_at": _iso(record.get("next_attempt_at")),
            "last_error": last_error(record),
            "deployed_commit": record.get("deployed_commit"),
            "deployed_revision": record.get("deployed_revision"),
            "health": record.get("health"),
            "updated_at": record.get("updated_at"),
            "failed_checks": failed_checks(record),
        }
    return found


def failed_checks(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """``[{name, code, detail}]`` of the environment's failing checks (0.19.0):
    a degraded verification's, else the last run's."""
    verification = _dict(record.get("verification"))
    if record.get("health") == "degraded" and verification.get("failed"):
        return [
            {
                "name": item.get("check"),
                "code": item.get("code"),
                "detail": item.get("detail") or None,
            }
            for item in verification["failed"]
            if isinstance(item, Mapping)
        ]
    checks = _dict(record.get("checks"))
    if checks.get("state") != "failed":
        return []
    return [
        {
            "name": item.get("name"),
            "code": item.get("code"),
            "detail": item.get("detail") or None,
        }
        for item in checks.get("results") or ()
        if isinstance(item, Mapping) and not item.get("passed")
    ]


def _names(commit: str, *values: Any) -> bool:
    """Whether ``commit`` (a prefix) is one of the commits in ``values``."""
    for value in values:
        items = value.values() if isinstance(value, Mapping) else [value]
        if any(isinstance(item, str) and item.startswith(commit) for item in items):
            return True
    return False


def outcome(
    document: Mapping[str, Any] | None, env: str, commit: str, *, seen: bool
) -> tuple[str | None, dict[str, Any]]:
    """``(state, body)``: a :data:`FINAL` state, or ``None`` to keep waiting.

    ``seen``: the environment named ``commit`` in an earlier read of this
    wait (``body["seen"]`` says whether it does now); a revision that moved
    on before deploying it is then ``superseded``, while one that never
    named it yet (the controller has not polled since the push) waits,
    unless the controller could not read Git in its last
    :data:`GIT_FAILED_POLLS` polls (0.19.0): the commit cannot arrive, so
    the wait fails at once, ``stage: fetch``, with Git's masked
    message under ``controller_error`` (also attached to any other outcome).
    """
    record = _dict(_dict(_dict(document).get("envs")).get(env))
    view = summary({"envs": {env: record}}).get(env, {})
    body: dict[str, Any] = {"env": env, "commit": commit, **view}
    controller = _dict(_dict(document).get("controller"))
    git_failing = controller.get("last_error") == "gitops-git-failed"
    if git_failing:
        body["controller_error"] = {
            "reason": "gitops-git-failed",
            "detail": controller.get("last_error_detail"),
        }
    wanted = _names(commit, record.get("revision"), record.get("commit"))
    running = _names(
        commit, record.get("deployed_revision"), record.get("deployed_commit")
    )
    body["seen"] = seen or wanted
    state = record.get("state")
    if running and (state == "deployed" or not wanted):
        return "deployed", body
    if wanted:
        if state == "failed":
            error = last_error(record) or {}
            body["stage"] = error.get("stage")
            body["cause"] = error.get("reason")
            return "failed", body
        if state == "approval-required":
            body["plan_hash"] = record.get("plan_hash")
            return "approval-required", body
        if state == "stopped":
            return "stopped", body
        return None, body
    if seen:
        return "superseded", body
    if git_failing and int(controller.get("poll_failures") or 0) >= GIT_FAILED_POLLS:
        body["stage"], body["cause"] = "fetch", "gitops-git-failed"
        return "failed", body
    return None, body
