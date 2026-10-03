"""Each environment's deploy history, as the composition controller publishes it.

The controller keeps two records of a deploy:

- its own **event** per deploy step (in its state, bounded per environment):
  the trigger, the revision and followed refs, the environment's plan hash,
  who approved it (``via``: ``cli``, ``ui`` or ``policy``), which components
  were built, rolled or unchanged with their digests, the verification and
  the failure (a registered code and the scrubbed, bounded build log tail);
- the deploy **run journal** of every run (``piceli.deploy-run.v1``, under
  ``<state_dir>/pipelines/…/runs/``), with its stages, plan, checks and
  failure; ``deleted`` (the objects its prune removed) and
  ``kept_orphaned`` (the claims, Secrets and retained objects it kept, each
  with the ``kubectl`` command deleting it) come from its plan.

:func:`history` joins both into ``piceli.gitops-history.v1``: per
environment, its runs newest first. A run journal without an event (a run
recorded before 0.14.6) is still listed; an event without a run (a build
that failed before the deploy) is listed too. The document holds commit ids,
hashes, digests, object kinds and names, registered codes, check names with
their short detail line and timings: never a field value, a Secret, a
kubeconfig or process output beyond the build log tail the status already
publishes. It is bounded (:data:`MAX_RUNS` per environment and
:data:`MAX_BYTES` in all) so it fits one ConfigMap, which the in-cluster UI
reads (``piceli-gitops-history``; see :mod:`piceli.gitops.state`).

Importing this module is side-effect free.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA = "piceli.gitops-history.v1"
#: Runs listed per environment (the newest).
MAX_RUNS = 20
#: Controller events kept per environment in its state.
MAX_EVENTS = 30
#: Upper bound of the serialized document (a ConfigMap holds at most 1 MiB).
MAX_BYTES = 600_000
#: Changed objects listed per run (``changes_total`` is always complete).
MAX_CHANGES = 50
#: Characters of a failure's log tail kept in the history.
MAX_TAIL = 2000
#: Characters of a check's detail line kept.
MAX_DETAIL = 300

_SUCCEEDED = {"ready", "verified"}


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any, limit: int = 256) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _when(value: Any) -> datetime:
    """An ISO timestamp (``Z`` or offset, any precision); the epoch when unreadable."""
    if not isinstance(value, str) or not value:
        return _EPOCH
    try:
        found = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return _EPOCH
    return found if found.tzinfo else found.replace(tzinfo=UTC)


# ------------------------------------------------------------------ events


def event(
    record: Mapping[str, Any],
    *,
    started_at: str,
    finished_at: str,
    trigger: str | None,
    approval: Mapping[str, Any] | None,
    outcome: Any,
    built: Iterable[str],
) -> dict[str, Any]:
    """The controller's event of one deploy step of ``record`` (its env record
    after the step). ``outcome`` is the step's
    :class:`~piceli.gitops.ports.EnvOutcome` (``None`` when the step raised).
    """
    components = _dict(record.get("components"))
    built_set = set(built)
    entries: dict[str, dict[str, Any]] = {}
    for name, value in sorted(components.items()):
        item = _dict(value)
        entries[str(name)] = {
            "source": _text(item.get("source")),
            "commit": _text(item.get("commit"), 64),
            "digest": _text(item.get("digest"), 80),
            "state": _text(item.get("state"), 32),
            "built": name in built_set,
        }
    state = str(record.get("state") or "unknown")
    if state == "deployed" and record.get("health") == "degraded":
        state = "degraded"
    via = _dict(approval).get("via") if approval else None
    found: dict[str, Any] = {
        "started_at": started_at,
        "finished_at": finished_at,
        "trigger": _text(trigger),
        "state": state,
        "action": _text(record.get("last_action"), 32)
        if state in {"deployed", "degraded"}
        else None,
        "reason": _text(record.get("reason"), 64),
        "revision": {str(k): str(v) for k, v in _dict(record.get("revision")).items()},
        "refs": {str(k): str(v) for k, v in _dict(record.get("refs")).items()},
        "namespace": _text(record.get("namespace"), 63),
        "plan_hash": _text(getattr(outcome, "plan_hash", None), 80),
        "combined_hash": _text(getattr(outcome, "combined_hash", None), 80),
        "run_id": _text(getattr(outcome, "run_id", None), 64),
        "approved_by": {
            "via": via if via in {"cli", "ui"} else "request",
            "at": _text(_dict(approval).get("at"), 32),
        }
        if approval
        else None,
        "components": entries,
        "built": sorted(built_set & set(entries)),
        "rolled": sorted(
            k for k, v in entries.items() if v["state"] in {"synced", "rolling"}
        )
        if state == "deployed"
        else [],
        "unchanged": sorted(k for k, v in entries.items() if v["state"] == "unchanged"),
        "failed": sorted(k for k, v in entries.items() if v["state"] == "failed"),
    }
    verification = _dict(record.get("verification"))
    if verification and str(verification.get("at") or "") >= started_at:
        found["verification"] = verification
    failure = _dict(record.get("failure"))
    if state in {"failed", "retrying"} and failure:
        found["failure"] = {
            key: failure[key]
            for key in ("kept_job", "denied", "refused")
            if key in failure
        }
        tail = failure.get("log_tail")
        if isinstance(tail, str) and tail:
            found["failure"]["log_tail"] = tail[-MAX_TAIL:]
    return found


def remember(state: dict[str, Any], env: str, entry: Mapping[str, Any]) -> None:
    """Append ``entry`` to ``env``'s events in the controller state (bounded)."""
    events = state.setdefault("history", {})
    kept = [item for item in events.get(env) or [] if isinstance(item, Mapping)]
    kept.append(dict(entry))
    events[env] = kept[-MAX_EVENTS:]


def forget_gone(state: dict[str, Any], envs: Iterable[str]) -> None:
    """Drop the events of environments that are gone from the state."""
    events = state.get("history")
    if not isinstance(events, dict):
        return
    keep = set(envs)
    for name in [name for name in events if name not in keep]:
        del events[name]


# -------------------------------------------------------------------- runs


def run_dirs(state_dir: Path, env: str, namespace: str | None) -> list[Path]:
    """The run journal directories of ``env`` under the controller's state.

    A named environment's pipeline state is ``pipelines/<env>/…``; a branch
    environment's is ``pipelines/<rule>/branches/<namespace>/``.
    """
    root = state_dir / "pipelines"
    found: list[Path] = []
    named = root / env
    if named.is_dir():
        found += [
            path
            for path in sorted(named.rglob("runs"))
            if path.is_dir() and "branches" not in path.relative_to(named).parts
        ]
    if namespace and root.is_dir():
        for rule in sorted(root.iterdir()):
            candidate = rule / "branches" / namespace / "runs"
            if candidate.is_dir():
                found.append(candidate)
    return found


class RunReader:
    """Reads run journals into history entries, cached by file identity."""

    def __init__(self) -> None:
        self._cache: dict[Path, tuple[tuple[int, int], dict[str, Any] | None]] = {}

    def runs(self, directories: Iterable[Path]) -> list[dict[str, Any]]:
        """Every readable run of ``directories``, newest first."""
        found: list[dict[str, Any]] = []
        seen: set[Path] = set()
        for directory in directories:
            for path in sorted(directory.glob("*.json")):
                seen.add(path)
                entry = self._read(path)
                if entry is not None:
                    found.append(entry)
        for stale in [path for path in self._cache if path not in seen]:
            del self._cache[stale]
        found.sort(key=lambda item: _when(item.get("started_at")), reverse=True)
        return found

    def _read(self, path: Path) -> dict[str, Any] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        key = (stat.st_mtime_ns, stat.st_size)
        cached = self._cache.get(path)
        if cached is not None and cached[0] == key:
            return cached[1]
        entry = run_entry(path)
        self._cache[path] = (key, entry)
        return entry


def run_entry(path: Path) -> dict[str, Any] | None:
    """One run journal as a history entry, or ``None`` when unreadable."""
    from piceli.pipeline.journal import RUN_SCHEMA, Run
    from piceli.pipeline.summary import build

    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("schema") != RUN_SCHEMA:
        return None
    try:
        summary = build(Run(path, data), path.parent.parent)
    except Exception:  # a malformed journal never breaks the history
        return None
    return _from_summary(summary)


def _from_summary(summary: Mapping[str, Any]) -> dict[str, Any]:
    plan = _dict(summary.get("plan"))
    changes = [_dict(item) for item in plan.get("changes") or ()]
    checks = _dict(summary.get("checks"))
    failure = _dict(summary.get("failure"))
    stages = {
        str(name): {
            key: value
            for key, value in _dict(stage).items()
            if key in {"state", "seconds", "reason"}
        }
        for name, stage in _dict(summary.get("stages")).items()
    }
    applied = _dict(stages.get("apply")).get("state") == "done"
    return {
        "run_id": summary.get("run_id"),
        "run_state": summary.get("state"),
        # What the run's prune deleted (once applied) and kept, with the
        # command deleting each kept object.
        "deleted": [
            {"kind": item.get("kind"), "name": item.get("name")}
            for item in changes
            if item.get("operation") == "delete"
        ][:MAX_CHANGES]
        if applied
        else [],
        "kept_orphaned": [
            {
                key: _dict(item).get(key)
                for key in ("kind", "name", "namespace", "why", "command")
            }
            for item in plan.get("kept_orphaned") or ()
        ][:MAX_CHANGES],
        "started_at": summary.get("created_at"),
        "finished_at": summary.get("updated_at"),
        "combined_hash": summary.get("combined_hash"),
        "release": summary.get("release"),
        "sources": {
            str(name): {
                "commit": _dict(value).get("commit"),
                **({"ref": _dict(value)["ref"]} if _dict(value).get("ref") else {}),
            }
            for name, value in _dict(summary.get("sources")).items()
        },
        "images": {
            str(name): {
                key: value
                for key, value in _dict(item).items()
                if key in {"reference", "config_digest", "size_bytes"}
            }
            for name, item in _dict(summary.get("images")).items()
        },
        "plan": {
            "counts": _dict(plan.get("counts")),
            "changes": [
                {
                    "operation": item.get("operation"),
                    "kind": item.get("kind"),
                    "name": item.get("name"),
                }
                for item in changes[:MAX_CHANGES]
            ],
            "changes_total": len(changes),
        }
        if plan
        else None,
        "checks": {
            "passed": checks.get("passed"),
            "results": [
                {
                    key: (
                        value[:MAX_DETAIL]
                        if key == "detail" and isinstance(value, str)
                        else value
                    )
                    for key, value in _dict(item).items()
                    if key in {"name", "type", "passed", "code", "detail", "duration"}
                }
                for item in checks.get("results") or ()
            ],
            **(
                {"rollback": _dict(checks.get("rollback"))}
                if checks.get("rollback")
                else {}
            ),
        }
        if checks
        else None,
        "failure": {
            key: failure[key]
            for key in (
                "stage",
                "state",
                "reason",
                "message",
                "category",
                "failed_checks",
            )
            if key in failure
        }
        if failure
        else None,
        "stages": stages,
        "seconds": summary.get("seconds"),
        "policy": summary.get("approved_by") == "policy",
    }


# ------------------------------------------------------------------ joined


def _entry(
    env: str, event_: Mapping[str, Any] | None, run: Mapping[str, Any] | None
) -> dict[str, Any]:
    event_ = _dict(event_)
    run = _dict(run)
    components = _dict(event_.get("components"))
    sources: dict[str, Any] = {}
    revision = _dict(event_.get("revision"))
    refs = _dict(event_.get("refs"))
    for name in sorted(set(revision) | set(refs)):
        sources[name] = {
            "commit": revision.get(name),
            **({"ref": refs[name]} if name in refs else {}),
        }
    for name, value in _dict(run.get("sources")).items():
        sources.setdefault(name, value)
    run_state = run.get("run_state")
    state = event_.get("state")
    if state is None:
        state = (
            "deployed"
            if run_state in _SUCCEEDED
            else "running"
            if run_state == "running"
            else "failed"
        )
    approved = event_.get("approved_by")
    if not approved and run.get("policy"):
        approved = {"via": "policy", "at": None}
    failure = dict(_dict(run.get("failure")))
    failure.update(_dict(event_.get("failure")))
    if state in {"failed", "retrying"} and event_.get("reason"):
        failure.setdefault("reason", event_.get("reason"))
    verification = _dict(event_.get("verification"))
    checks = run.get("checks")
    if isinstance(checks, Mapping):
        checks = {**checks, "verification": bool(verification)}
    started = event_.get("started_at") or run.get("started_at")
    return {
        "id": run.get("run_id") or f"{env}@{started}",
        "env": env,
        "run_id": run.get("run_id"),
        "started_at": started,
        "finished_at": event_.get("finished_at") or run.get("finished_at"),
        "state": state,
        "run_state": run_state,
        "action": event_.get("action"),
        "reason": event_.get("reason"),
        "trigger": event_.get("trigger"),
        "sources": sources,
        "namespace": event_.get("namespace"),
        "plan_hash": event_.get("plan_hash"),
        "combined_hash": run.get("combined_hash") or event_.get("combined_hash"),
        "approved_by": approved,
        "release": run.get("release"),
        "components": [
            {"name": name, **_dict(value)} for name, value in sorted(components.items())
        ],
        "built": list(event_.get("built") or []),
        "rolled": list(event_.get("rolled") or []),
        "unchanged": list(event_.get("unchanged") or []),
        "deleted": list(run.get("deleted") or []),
        "kept_orphaned": list(run.get("kept_orphaned") or []),
        "images": _dict(run.get("images")),
        "plan": run.get("plan"),
        "checks": checks,
        "verification": verification or None,
        "failure": failure or None,
        "stages": _dict(run.get("stages")),
        "seconds": run.get("seconds"),
        "recorded_by": "controller+run"
        if event_ and run
        else "controller"
        if event_
        else "run",
    }


def environment_runs(
    env: str,
    events: Iterable[Mapping[str, Any]],
    runs: Iterable[Mapping[str, Any]],
    limit: int = MAX_RUNS,
) -> list[dict[str, Any]]:
    """``env``'s runs newest first: each event joined to its run, plus the
    runs no event claims (recorded before 0.14.6).

    An event names its run when the deploy result did; a step that raised
    (a failed or degraded run) does not, and is joined to the run its
    environment's journal created during the step (the controller deploys
    one environment at a time).
    """
    by_id = {str(run["run_id"]): run for run in runs if run.get("run_id")}
    used: set[str] = set()
    entries: list[dict[str, Any]] = []
    slack = timedelta(seconds=1)
    events = list(events)
    named = {str(item.get("run_id")) for item in events if item.get("run_id")}
    for item in events:
        run = None
        run_id = item.get("run_id")
        if isinstance(run_id, str) and run_id in by_id:
            run = by_id[run_id]
        else:
            start = _when(item.get("started_at")) - slack
            end = _when(item.get("finished_at")) + slack
            run = next(
                (
                    candidate
                    for candidate in by_id.values()  # newest first
                    if str(candidate["run_id"]) not in used | named
                    and start <= _when(candidate.get("started_at")) <= end
                ),
                None,
            )
        if run is not None:
            used.add(str(run["run_id"]))
        entries.append(_entry(env, item, run))
    for run_id, run in by_id.items():
        if run_id not in used:
            entries.append(_entry(env, None, run))
    # Newest first; the later of two steps that started in the same second
    # (events are in step order) comes first.
    order = {id(item): index for index, item in enumerate(entries)}
    entries.sort(
        key=lambda item: (
            _when(item.get("started_at")),
            _when(item.get("finished_at")),
            order[id(item)],
        ),
        reverse=True,
    )
    return entries[:limit]


def history(
    envs: Mapping[str, Mapping[str, Any]],
    events: Mapping[str, Any],
    state_dir: Path,
    *,
    reader: RunReader | None = None,
    at: str | None = None,
) -> dict[str, Any]:
    """The ``piceli.gitops-history.v1`` document of every environment in ``envs``.

    ``envs`` is the controller's environment records (by name), ``events``
    its remembered events (by name).
    """
    reader = reader or RunReader()
    result: dict[str, Any] = {}
    for name, record in sorted(envs.items()):
        namespace = record.get("namespace") if isinstance(record, Mapping) else None
        runs = reader.runs(
            run_dirs(state_dir, name, namespace if isinstance(namespace, str) else None)
        )
        own = [item for item in events.get(name) or () if isinstance(item, Mapping)]
        result[name] = {"runs": environment_runs(name, own, runs)}
    document = {
        "schema": SCHEMA,
        "generated_at": at,
        "limits": {"runs": MAX_RUNS, "bytes": MAX_BYTES},
        "envs": result,
        "truncated": False,
    }
    return bounded(document)


def bounded(document: dict[str, Any], limit: int = MAX_BYTES) -> dict[str, Any]:
    """``document`` within ``limit`` serialized bytes: the oldest runs go first."""

    def size() -> int:
        return len(json.dumps(document, sort_keys=True).encode())

    envs = document["envs"]
    while size() > limit:
        longest = max(envs.values(), key=lambda item: len(item["runs"]), default=None)
        if longest is None or not longest["runs"]:
            break
        longest["runs"].pop()
        document["truncated"] = True
    return document
