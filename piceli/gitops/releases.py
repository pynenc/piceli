"""The controller's deployment history for the command line (0.38.0).

``piceli gitops history``, ``gitops show`` and ``gitops compare`` read the
same document the UI's Deployment history reads: the controller's
``piceli.gitops-history.v1`` (:mod:`piceli.gitops.history`, the ConfigMap
``piceli-gitops-history`` or ``history.json`` of a local ``--state-dir``).
These functions only select and compare its runs; every run is the
history's own entry, unchanged, with two windows added:

- ``window``: the run, from its first step to its end;
- ``release_window``: from the start of its apply (the run's start when it
  applied nothing) to its end, its checks settled. The controller's release
  marker (``piceli.release``, see ``docs/deploy_events.md``) carries the
  same ``piceli.release.started_at`` and ``finished_at``.

Schemas (additive within v1): ``piceli.gitops-runs.v1`` (history),
``piceli.gitops-run.v1`` (one run) and ``piceli.gitops-compare.v1``.

Importing this module is side-effect free.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from piceli.gitops import GitOpsError
from piceli.gitops.history import SCHEMA as HISTORY_SCHEMA
from piceli.gitops.history import _span, _when

RUNS_SCHEMA = "piceli.gitops-runs.v1"
RUN_SCHEMA = "piceli.gitops-run.v1"
COMPARE_SCHEMA = "piceli.gitops-compare.v1"
#: Plan operations that add or remove an object; the others change it.
_ADDED, _REMOVED, _UNCHANGED = {"create"}, {"delete"}, {"no-op"}


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def check(document: Any) -> dict[str, Any]:
    """``document`` when it is a published history.
    :raises GitOpsError: ``gitops-history-unavailable``."""
    if not isinstance(document, Mapping) or document.get("schema") != HISTORY_SCHEMA:
        raise GitOpsError(
            "gitops-history-unavailable",
            "the controller has published no deployment history yet",
        )
    return dict(document)


def release_window(entry: Mapping[str, Any]) -> dict[str, Any]:
    """From the start of the run's apply (else the run's) to its end."""
    apply = _dict(_dict(entry.get("stages")).get("apply"))
    started = apply.get("started_at") or entry.get("started_at")
    return _span(started, entry.get("finished_at"))


def view(entry: Mapping[str, Any]) -> dict[str, Any]:
    """A history entry with its ``window`` and ``release_window``."""
    return {
        **entry,
        "window": _span(entry.get("started_at"), entry.get("finished_at")),
        "release_window": release_window(entry),
    }


def released(entry: Mapping[str, Any]) -> bool:
    """A run that applied a release (deployed or degraded, not a
    verification of an unchanged release, not a lifecycle entry)."""
    return (
        entry.get("kind", "run") == "run"
        and entry.get("state") in {"deployed", "degraded"}
        # A run recorded only by its journal (before 0.14.6) has no action.
        and entry.get("action") in {"deployed", None}
    )


def _runs(document: Mapping[str, Any], env: str) -> list[dict[str, Any]]:
    entry = _dict(_dict(document.get("envs")).get(env))
    found = [
        _dict(item) for item in entry.get("runs") or () if isinstance(item, Mapping)
    ]
    found.sort(
        key=lambda item: (
            _when(item.get("started_at")),
            _when(item.get("finished_at")),
        ),
        reverse=True,
    )
    return found


def _envs(document: Mapping[str, Any], env: str | None) -> list[str]:
    names = sorted(str(name) for name in _dict(document.get("envs")))
    if env is None:
        return names
    if env not in names:
        raise GitOpsError(
            "gitops-run-unknown",
            f"no environment {env!r} in the history"
            + (f" (it has {', '.join(names)})" if names else ""),
        )
    return [env]


def history(
    document: Any, env: str | None = None, limit: int | None = None
) -> dict[str, Any]:
    """``gitops history --json``: each environment's runs, newest first.
    :raises GitOpsError:"""
    document = check(document)
    return {
        "schema": RUNS_SCHEMA,
        "history_schema": HISTORY_SCHEMA,
        "generated_at": document.get("generated_at"),
        "truncated": document.get("truncated") is True,
        "environments": [
            {
                "name": name,
                "runs": [view(item) for item in _runs(document, name)[:limit]],
            }
            for name in _envs(document, env)
        ],
    }


def find(document: Any, run: str, env: str | None = None) -> tuple[str, dict[str, Any]]:
    """The environment and entry of ``run``: its ``id`` or ``run_id``, or a
    unique prefix of one. :raises GitOpsError:"""
    document = check(document)
    exact, prefixed = [], []
    for name in _envs(document, env):
        for item in _runs(document, name):
            ids = [str(item[k]) for k in ("id", "run_id") if item.get(k)]
            if run in ids:
                exact.append((name, item))
            elif any(value.startswith(run) for value in ids):
                prefixed.append((name, item))
    found = exact or prefixed
    if len(found) == 1:
        return found[0]
    raise GitOpsError(
        "gitops-run-unknown",
        f"run {run!r} is in the history {len(found)} times; pass more of its id or ENV"
        if found
        else f"no run {run!r} in the history",
    )


def logs(entry: Mapping[str, Any]) -> list[dict[str, Any]]:
    """What the history keeps of a run's output: the failed build's log tail
    and those of the revision's earlier failed attempts (bounded, redacted)."""
    found = []
    for item in entry.get("failed_attempts") or ():
        item = _dict(item)
        if item.get("log_tail"):
            found.append(
                {
                    "source": "failed-attempt",
                    "attempt": item.get("attempt"),
                    "at": item.get("at"),
                    "reason": item.get("reason"),
                    **({"stage": item["stage"]} if item.get("stage") else {}),
                    "text": item["log_tail"],
                }
            )
    failure = _dict(entry.get("failure"))
    if failure.get("log_tail"):
        found.append(
            {
                "source": "failure",
                "at": entry.get("finished_at"),
                "reason": failure.get("reason"),
                # 0.41.0: where it failed (a build host's stage) and on which host.
                **{k: failure[k] for k in ("stage", "host") if failure.get(k)},
                "text": failure["log_tail"],
            }
        )
    return found


LOGS_NOTE = (
    "The controller keeps only the redacted log tail of a failed build "
    "(and of the revision's earlier failed attempts). For the output of the "
    "running workloads use `piceli logs`; the run's stages, checks and "
    "failure are in `run`."
)


def run(
    document: Any, run_id: str, env: str | None = None, with_logs: bool = False
) -> dict[str, Any]:
    """``gitops show --json``: one run, with its captured log on ``--logs``.
    :raises GitOpsError:"""
    name, entry = find(document, run_id, env)
    body: dict[str, Any] = {"schema": RUN_SCHEMA, "env": name, "run": view(entry)}
    if with_logs:
        body["logs"] = logs(entry)
        body["logs_note"] = LOGS_NOTE
    return body


# ----------------------------------------------------------------- compare


def _side(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: entry.get(key)
        for key in (
            "id",
            "run_id",
            "state",
            "action",
            "trigger",
            "release",
            "plan_hash",
            "combined_hash",
            "started_at",
            "finished_at",
        )
    } | {
        "window": _span(entry.get("started_at"), entry.get("finished_at")),
        "release_window": release_window(entry),
        "checks_passed": _dict(entry.get("checks")).get("passed"),
    }


def _defaults(runs: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The running release and the one before it (the newest two runs that
    applied a release; else the newest two runs)."""
    releases = [item for item in runs if released(item)]
    if len(releases) < 2:
        releases = [item for item in runs if item.get("kind", "run") == "run"]
    return list(reversed(releases[:2]))


def _sources(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> list[dict[str, Any]]:
    old, new = _dict(before.get("sources")), _dict(after.get("sources"))
    found = []
    for name in sorted(set(old) | set(new)):
        a, b = _dict(old.get(name)), _dict(new.get(name))
        found.append(
            {
                "name": name,
                "before": {"commit": a.get("commit"), "ref": a.get("ref")}
                if a
                else None,
                "after": {"commit": b.get("commit"), "ref": b.get("ref")}
                if b
                else None,
                "changed": a.get("commit") != b.get("commit"),
            }
        )
    return found


def _components(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> list[dict[str, Any]]:
    def index(entry: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        return {
            str(item.get("name")): item
            for item in (_dict(i) for i in entry.get("components") or ())
            if item.get("name")
        }

    old, new = index(before), index(after)
    found = []
    for name in sorted(set(old) | set(new)):
        a, b = old.get(name) or {}, new.get(name) or {}
        found.append(
            {
                "name": name,
                "before": {"digest": a.get("digest"), "commit": a.get("commit")}
                if a
                else None,
                "after": {"digest": b.get("digest"), "commit": b.get("commit")}
                if b
                else None,
                "image_changed": a.get("digest") != b.get("digest"),
            }
        )
    return found


def _resources(
    runs: Sequence[dict[str, Any]], before: Mapping[str, Any], after: Mapping[str, Any]
) -> dict[str, Any]:
    """The objects the runs after ``before`` up to ``after`` added, removed
    and changed (their plans, oldest first; an object added then removed is
    in neither list)."""
    oldest_first = list(reversed(runs))
    ids = [item.get("id") for item in oldest_first]
    start = ids.index(before.get("id")) + 1 if before.get("id") in ids else 0
    end = ids.index(after.get("id")) + 1 if after.get("id") in ids else len(ids)
    net: dict[tuple[str, str], str] = {}
    partial = False
    for item in oldest_first[start:end]:
        if item.get("kind", "run") != "run" or item.get("state") not in {
            "deployed",
            "degraded",
        }:
            continue
        plan = _dict(item.get("plan"))
        changes = [_dict(c) for c in plan.get("changes") or ()]
        if plan.get("changes_total", len(changes)) > len(changes):
            partial = True
        for change in changes:
            key = (str(change.get("kind")), str(change.get("name")))
            operation = str(change.get("operation"))
            if operation in _UNCHANGED:
                continue
            earlier = net.get(key)
            if operation in _REMOVED and earlier == "added":
                del net[key]
            elif operation in _ADDED or earlier == "added":
                net[key] = "added"
            elif operation in _REMOVED:
                net[key] = "removed"
            else:
                net[key] = "changed" if earlier != "removed" else "added"

    def listed(state: str) -> list[dict[str, str]]:
        return [
            {"kind": kind, "name": name}
            for (kind, name), value in sorted(net.items())
            if value == state
        ]

    return {
        "added": listed("added"),
        "removed": listed("removed"),
        "changed": listed("changed"),
        "partial": partial,
    }


FIELDS_NOTE = (
    "The controller's history holds object kinds and names, never field "
    "values. For before/after field values, use the UI's Compare revisions "
    "(plans saved by the UI) or `piceli diff` against the cluster."
)


def compare(
    document: Any, env: str, a: str | None = None, b: str | None = None
) -> dict[str, Any]:
    """``gitops compare --json``: run ``a`` (before) against ``b`` (after);
    by default the release before the running one, and the running one.
    :raises GitOpsError:"""
    document = check(document)
    _envs(document, env)
    runs = _runs(document, env)
    if a is None and b is None:
        chosen = _defaults(runs)
        if len(chosen) < 2:
            raise GitOpsError(
                "gitops-run-unknown", f"{env} has fewer than two runs to compare"
            )
        before, after = chosen
    else:
        before = find(document, str(a), env)[1]
        if b is None:
            running = [item for item in runs if released(item)]
            if not running:
                raise GitOpsError(
                    "gitops-run-unknown",
                    f"{env} has no running release to compare with",
                )
            after = running[0]
        else:
            after = find(document, b, env)[1]
    if _when(before.get("started_at")) > _when(after.get("started_at")):
        before, after = after, before
    components = _components(before, after)
    return {
        "schema": COMPARE_SCHEMA,
        "env": env,
        "before": _side(before),
        "after": _side(after),
        "sources": _sources(before, after),
        "components": components,
        "images_changed": [c["name"] for c in components if c["image_changed"]],
        "resources": _resources(runs, before, after),
        "fields": {"available": False, "note": FIELDS_NOTE},
    }
