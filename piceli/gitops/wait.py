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


# ---------------------------------------------------------------- rollout

ROLLOUT_SCHEMA = "piceli.gitops-rollout-wait.v1"
#: Lines of a failed step's log tail a rollout wait prints.
TAIL_LINES = 20


def _epoch(value: Any) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def duration(seconds: float | None) -> str:
    """``45s``, ``3m``, ``1h05m``."""
    if seconds is None or seconds < 0:
        return ""
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h{minutes % 60:02d}m"


def _runs_revision(record: Mapping[str, Any]) -> bool:
    revision, deployed = record.get("revision"), record.get("deployed_revision")
    if isinstance(revision, Mapping) and revision:
        return dict(revision) == _dict(deployed)
    commit = record.get("commit")
    return bool(commit) and commit == record.get("deployed_commit")


def _tail(record: Mapping[str, Any]) -> str | None:
    tail = _dict(record.get("failure")).get("log_tail")
    if not isinstance(tail, str) or not tail.strip():
        attempts = record.get("failed_attempts") or ()
        tail = next(
            (
                item.get("log_tail")
                for item in reversed(list(attempts))
                if isinstance(item, Mapping) and item.get("log_tail")
            ),
            None,
        )
    if not isinstance(tail, str) or not tail.strip():
        return None
    return "\n".join(tail.rstrip("\n").splitlines()[-TAIL_LINES:])


def progress(name: str, record: Mapping[str, Any], now: float | None) -> dict[str, Any]:
    """One environment's view in a rollout wait: ``state`` (``done``,
    ``failed`` or ``waiting``), a short ``text`` (``building (waiting for a
    build host) 3m``), the same without its duration (``base``) and, when
    failed, its ``failure`` (reason, stage, detail, log tail)."""
    view = _progress(name, record, now)
    base, _, took = view["text"].partition("\t")
    return {**view, "text": f"{base} {took}".rstrip(), "base": base}


def _progress(
    name: str, record: Mapping[str, Any], now: float | None
) -> dict[str, Any]:
    state, reason = record.get("state"), record.get("reason")
    commit = str(record.get("commit") or "")[:12]

    def since(value: Any) -> str:
        began = _epoch(value)
        took = duration(None if now is None or began is None else now - began)
        return f"\t{took}" if took else ""

    def failed(why: str, stage: Any, detail: str | None) -> dict[str, Any]:
        return {
            "state": "failed",
            "text": f"failed in stage {stage or '?'} ({why})",
            "failure": {
                "env": name,
                "reason": why,
                "stage": stage,
                "commit": record.get("commit"),
                "detail": detail,
                "log_tail": _tail(record),
            },
        }

    checks = failed_checks(record)
    detail = (
        "failing checks: "
        + ", ".join(
            f"{item['name']} ({item['detail']})"
            if item.get("detail")
            else str(item["name"])
            for item in checks
        )
        if checks
        else None
    )
    if record.get("health") == "degraded":
        return failed(str(reason or "pipeline-checks-failed"), "checks", detail)
    if state == "failed":
        return failed(
            str(reason or "gitops-env-failed"), record.get("failed_stage"), detail
        )
    if state == "stopped":
        return {"state": "done", "text": "stopped"}
    progress_ = _dict(record.get("in_progress"))
    if progress_:
        action = str(progress_.get("action") or "deploy")
        stage = progress_.get("stage")
        verb = {"deploy": "deploying", "teardown": "tearing down", "stop": "stopping"}
        text = verb.get(action, action) + (f", stage {stage}" if stage else "")
        return {
            "state": "waiting",
            "text": text
            + since(progress_.get("stage_since") or progress_.get("since")),
        }
    if state == "retrying" and reason == "build-waiting":
        return {
            "state": "waiting",
            "text": "building (waiting for a build host)"
            + since(record.get("waiting_since")),
        }
    if state == "approval-required":
        return {
            "state": "waiting",
            "text": f"waiting for approval: piceli gitops approve {name} "
            f"{record.get('plan_hash')}" + since(record.get("approval_required_since")),
        }
    if state == "retrying":
        return {
            "state": "waiting",
            "text": f"retrying ({reason}), attempt {record.get('attempts')}",
        }
    if state == "deployed" and _runs_revision(record):
        total = _dict(record.get("checks")).get("total")
        return {
            "state": "done",
            "text": f"running {commit}"
            + (", checks passed" if total else ", no checks"),
        }
    return {"state": "waiting", "text": str(state or "pending")}


def rollout(
    document: Mapping[str, Any] | None, *, since_poll: str | None = None
) -> tuple[str | None, dict[str, Any]]:
    """Whether every environment runs its revision with its checks passed
    (``deployed``), one failed (``failed``, the first one under
    ``failure``), or ``None`` to keep waiting.

    ``since_poll``: the controller's ``last_poll`` before an install; the
    wait is over only after a later poll (the new controller's). A
    composition that does not import, or Git unreadable for
    :data:`GIT_FAILED_POLLS` polls, fails at once: nothing can deploy.
    """
    doc = _dict(document)
    controller = _dict(doc.get("controller"))
    body: dict[str, Any] = {"envs": {}, "failure": None}
    fresh = controller.get("last_poll") is not None and (
        since_poll is None or controller.get("last_poll") != since_poll
    )
    body["fresh"] = fresh
    repo = _dict(controller.get("composition_repo"))
    if fresh and repo.get("failed"):
        body["failure"] = {
            "env": None,
            "reason": repo.get("error") or "composition-invalid",
            "stage": "import",
            "commit": repo.get("failed"),
            "detail": repo.get("error_detail"),
            "log_tail": None,
        }
        return "failed", body
    if (
        fresh
        and controller.get("last_error") == "gitops-git-failed"
        and int(controller.get("poll_failures") or 0) >= GIT_FAILED_POLLS
    ):
        body["failure"] = {
            "env": None,
            "reason": "gitops-git-failed",
            "stage": "fetch",
            "commit": None,
            "detail": controller.get("last_error_detail"),
            "log_tail": None,
        }
        return "failed", body
    now = _epoch(controller.get("heartbeat_at") or controller.get("last_poll"))
    done = True
    for name, value in sorted(_dict(doc.get("envs")).items()):
        view = progress(name, _dict(value), now)
        body["envs"][name] = view
        if fresh and view["state"] == "failed" and body["failure"] is None:
            body["failure"] = view["failure"]
        done = done and view["state"] == "done"
    if body["failure"] is not None:
        return "failed", body
    if not fresh or not body["envs"]:
        return None, body
    return ("deployed" if done else None), body


PREVIEW_SCHEMA = "piceli.env-preview.v1"


def preview(
    document: Mapping[str, Any] | None, branch: str, request: str, action: str
) -> tuple[str | None, dict[str, Any]]:
    """Where a ``preview`` request stands (``piceli env up|down BRANCH
    --wait``, 0.28.0): ``ready`` (the branch runs the head the request saw,
    its checks passed; ``url`` then), ``removed`` (``down``), ``failed``
    (rejected, or the deploy failed: ``reason``), or ``None`` to keep waiting.

    The request counts only once the controller lists it as served
    (``controller.previews.served``), so a status published before it is
    never mistaken for the answer.
    """
    doc = _dict(document)
    controller = _dict(doc.get("controller"))
    body: dict[str, Any] = {"branch": branch, "request": request, "action": action}
    for item in doc.get("rejected_requests") or ():
        if isinstance(item, Mapping) and item.get("request") == request:
            body.update(
                reason=item.get("reason"), detail=item.get("message"), text="rejected"
            )
            return "failed", body
    served = request in (_dict(controller.get("previews")).get("served") or ())
    record = _dict(_dict(doc.get("envs")).get(branch))
    if not served:
        body["text"] = "requested; waiting for the controller"
        return None, body
    if action == "down":
        if not record:
            body["text"] = "removed"
            return "removed", body
        body["text"] = "tearing down"
        return None, body
    if not record:
        body["text"] = "pending"
        return None, body
    now = _epoch(controller.get("heartbeat_at") or controller.get("last_poll"))
    view = progress(branch, record, now)
    body.update(text=view["text"], base=view["base"], commit=record.get("commit"))
    if view["state"] == "failed":
        failure = view["failure"]
        body.update(reason=failure.get("reason"), failure=failure)
        return "failed", body
    if view["state"] != "done":
        return None, body
    url = _dict(record.get("preview")).get("url")
    if url:
        body["url"] = url
    return "ready", body
