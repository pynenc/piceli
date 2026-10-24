"""Cycle records: one commit's way through one target, with its phases.

Every builder here is a pure function of what its owner already recorded
(the controller's step, a train run's result, a host's deploy attempt, a
Nix release's stages), so the same record comes out wherever it is built.
A record is bounded (:data:`MAX_PHASES` phases, short names) so it fits in
the controller's history ConfigMap and in a train's ``run.json``.

Importing this module is side-effect free.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

#: Phases kept per cycle (the longest, when there are more).
MAX_PHASES = 24
#: Characters of a phase or target name.
MAX_NAME = 48

#: A deploy run's stages (``piceli.deploy-run.v1``) as cycle phases.
STAGE_PHASES = {
    "inputs": "preparing",
    "build": "building",
    "deliver": "delivering",
    "plan": "applying",
    "apply": "applying",
    "prune": "applying",
    "checks": "checking",
    "rollback": "rolling-back",
}
#: A Nix release's stages (controller and build host) as cycle phases.
RELEASE_PHASES = {
    "approval": "approval",
    "queue": "waiting-for-host",
    "fetch": "fetching",
    "inputs": "fetching",
    "build": "building",
    "publish": "publishing",
}
#: A Nix host's deploy attempt stages (``piceli.hosts.agent``) as phases.
HOST_PHASES = {
    "resolve": "downloading",
    "verify": "downloading",
    "realise": "downloading",
    "activate": "activating",
    "checks": "checking",
    "boot": "boot",
}


def epoch(value: Any) -> float | None:
    """An ISO timestamp (``Z`` or an offset) as seconds; ``None`` when unreadable."""
    if isinstance(value, int | float):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        found = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (found if found.tzinfo else found.replace(tzinfo=UTC)).timestamp()


def iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def between(start: Any, end: Any) -> float | None:
    """Seconds from ``start`` to ``end`` (ISO or epoch); ``None`` when unknown."""
    begin, finish = epoch(start), epoch(end)
    if begin is None or finish is None:
        return None
    return round(max(0.0, finish - begin), 3)


def _add(phases: dict[str, float], name: str, seconds: Any) -> None:
    if isinstance(seconds, int | float) and not isinstance(seconds, bool):
        if seconds >= 0:
            phases[name] = round(phases.get(name, 0.0) + float(seconds), 3)


def bounded_phases(phases: Mapping[str, Any]) -> dict[str, float]:
    """``phases`` with numeric, non-negative seconds, short names, at most
    :data:`MAX_PHASES` (the longest)."""
    found: dict[str, float] = {}
    for name, seconds in phases.items():
        _add(found, str(name)[:MAX_NAME], seconds)
    kept = sorted(found.items(), key=lambda item: -item[1])[:MAX_PHASES]
    return dict(sorted(kept))


def cycle(
    *,
    target: str,
    kind: str,
    commit: Any,
    seen_at: Any,
    checked_at: Any,
    ok: bool,
    phases: Mapping[str, Any],
    pushed_at: Any = None,
    state: Any = None,
) -> dict[str, Any]:
    """One cycle record. ``seconds`` runs from the push (when known) or from
    when Piceli first saw the commit (``from``) to running and checked."""
    start = pushed_at or seen_at
    seconds = between(start, checked_at) if ok else None
    return {
        "target": str(target)[:MAX_NAME],
        "kind": kind,
        "commit": str(commit)[:64] if commit else None,
        "pushed_at": pushed_at if isinstance(pushed_at, str) else None,
        "seen_at": seen_at if isinstance(seen_at, str) else None,
        "checked_at": checked_at if ok and isinstance(checked_at, str) else None,
        "from": "pushed" if pushed_at else "seen",
        "ok": bool(ok),
        "state": str(state)[:32] if state else None,
        "seconds": seconds,
        "phases": bounded_phases(phases),
    }


# ------------------------------------------------------------- controller


def _builds_phases(
    phases: dict[str, float], builds: Mapping[str, Mapping[str, Any]]
) -> None:
    """In-cluster builds of the step: their joint span is ``building``, the
    longest push of them ``delivering`` (taken out of ``building``)."""
    spans = [
        (epoch(item.get("started_at")), epoch(item.get("finished_at")))
        for item in builds.values()
        if isinstance(item, Mapping) and item.get("state") != "cached"
    ]
    spans = [(a, b) for a, b in spans if a is not None and b is not None]
    if not spans:
        return
    total = max(b for _, b in spans) - min(a for a, _ in spans)
    push = max(
        (
            float((item.get("timings") or {}).get("push_seconds") or 0)
            for item in builds.values()
            if isinstance(item, Mapping)
        ),
        default=0.0,
    )
    push = min(push, total)
    _add(phases, "building", round(total - push, 3))
    if push:
        _add(phases, "delivering", round(push, 3))


def deploy_cycle(
    *,
    target: str,
    commit: Any,
    seen_at: Any,
    first_step_at: Any,
    step_started_at: Any,
    finished_at: Any,
    state: Any,
    checked: bool,
    approval: Mapping[str, Any] | None = None,
    waited_since: Any = None,
    host_build: Mapping[str, Any] | None = None,
    builds: Mapping[str, Mapping[str, Any]] | None = None,
    stages: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """The cycle of a controller deploy step (an environment).

    - ``queued``: from first seen to the first step of this commit;
    - ``approval``: waiting for an owner's approval;
    - ``waiting-for-host``, ``building``, ``delivering``: a build host's
      build (asked, claimed, built and uploaded, delivered), or the wait for
      a build host that never came, then in-cluster builds;
    - ``applying``, ``checking`` (and ``delivering``, ``preparing``): the
      deploy run's stages.
    """
    phases: dict[str, float] = {}
    _add(phases, "queued", between(seen_at, first_step_at))
    if approval and approval.get("requested_at") and approval.get("at"):
        _add(phases, "approval", between(approval["requested_at"], approval["at"]))
    if host_build:
        _add(
            phases,
            "waiting-for-host",
            between(host_build.get("requested_at"), host_build.get("claimed_at")),
        )
        built = host_build.get("built_at") or host_build.get("delivered_at")
        _add(phases, "building", between(host_build.get("claimed_at"), built))
        _add(phases, "delivering", between(built, host_build.get("delivered_at")))
    elif waited_since:
        _add(phases, "waiting-for-host", between(waited_since, step_started_at))
    _builds_phases(phases, builds or {})
    for name, stage in (stages or {}).items():
        if isinstance(stage, Mapping) and str(name) in STAGE_PHASES:
            if str(name) == "build" and builds:
                continue  # the controller's own build, counted above
            _add(phases, STAGE_PHASES[str(name)], stage.get("seconds"))
    return cycle(
        target=target,
        kind="environment",
        commit=commit,
        seen_at=seen_at or step_started_at,
        checked_at=finished_at,
        ok=checked,
        phases=phases,
        state=state,
    )


def environment_cycle(env: str, entry: Mapping[str, Any]) -> dict[str, Any] | None:
    """The cycle of one history run (``piceli.gitops-history.v1``): its own
    ``cycle`` when the controller recorded one (0.28.0), else what the run
    shows (its stages and builds; first seen is unknown: its start)."""
    if entry.get("kind", "run") != "run":
        return None
    own = entry.get("cycle")
    if isinstance(own, Mapping) and own.get("kind") == "environment":
        return dict(own)
    state = entry.get("state")
    builds = {
        str(name): item
        for name, item in (entry.get("builds") or {}).items()
        if isinstance(item, Mapping)
    }
    phases: dict[str, float] = {}
    wait = entry.get("approval_wait")
    if isinstance(wait, Mapping):
        _add(phases, "approval", wait.get("seconds"))
    _builds_phases(phases, builds)
    for name, stage in (entry.get("stages") or {}).items():
        if isinstance(stage, Mapping) and str(name) in STAGE_PHASES:
            if str(name) == "build" and builds:
                continue
            _add(phases, STAGE_PHASES[str(name)], stage.get("seconds"))
    commit = next(
        (
            item.get("commit")
            for item in (entry.get("sources") or {}).values()
            if isinstance(item, Mapping) and item.get("commit")
        ),
        None,
    )
    return cycle(
        target=env,
        kind="environment",
        commit=commit,
        seen_at=entry.get("started_at"),
        checked_at=entry.get("finished_at"),
        ok=state == "deployed",
        phases=phases,
        state=state,
    )


def release_cycle(name: str, run: Mapping[str, Any]) -> dict[str, Any]:
    """The cycle of one Nix release publish (``nix_releases`` of the history)."""
    phases: dict[str, float] = {}
    for stage, item in (run.get("stages") or {}).items():
        if isinstance(item, Mapping) and str(stage) in RELEASE_PHASES:
            _add(phases, RELEASE_PHASES[str(stage)], item.get("seconds"))
    return cycle(
        target=name,
        kind="nix-release",
        commit=run.get("commit"),
        seen_at=run.get("requested_at"),
        checked_at=run.get("finished_at"),
        ok=run.get("state") == "published",
        phases=phases,
        state=run.get("state"),
    )


# ------------------------------------------------------------------ train


def train_cycle(
    result: Mapping[str, Any],
    *,
    queued_at: Any = None,
    timings: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """The cycle of a train run (its ``run.json`` result).

    ``queued`` (from ``train enqueue`` to the run's start), ``merging``,
    ``testing <step>`` per test step (summed over the trials that counted),
    ``promoting`` (tag and fast-forwards), ``images`` and ``publishing``
    (the publish steps), ``pushing``.
    """
    timings = timings or {}
    phases: dict[str, float] = {}
    started = result.get("started")
    _add(phases, "queued", between(queued_at, started))
    for trial in result.get("trials") or ():
        if not isinstance(trial, Mapping) or trial.get("discarded"):
            continue
        for step in trial.get("steps") or ():
            if not isinstance(step, Mapping):
                continue
            name = (
                "merging"
                if step.get("kind") == "merge"
                else f"testing {step.get('name')}"
            )
            _add(phases, name, step.get("duration"))
    promote = (
        result.get("promote") if isinstance(result.get("promote"), Mapping) else {}
    )
    published = 0.0
    for step in (promote or {}).get("publish") or ():
        if isinstance(step, Mapping):
            seconds = float(step.get("duration") or 0)
            published += seconds
            _add(
                phases,
                "images" if step.get("kind") == "images" else "publishing",
                seconds,
            )
    if "promoting" in timings:
        _add(phases, "promoting", round(max(0.0, timings["promoting"] - published), 3))
    _add(phases, "pushing", timings.get("pushing"))
    begin = epoch(started)
    end = iso(begin + float(result.get("duration") or 0)) if begin is not None else None
    return cycle(
        target=str(result.get("train") or "train"),
        kind="train",
        commit=result.get("head"),
        seen_at=queued_at or started,
        checked_at=end,
        ok=result.get("ok") is True,
        phases=phases,
        state=result.get("state"),
    )


# ------------------------------------------------------------------- host


def host_cycle(
    host: str,
    attempt: Mapping[str, Any],
    releases: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """The cycle of a Nix host's deploy attempt (``last_attempt`` of its
    status). ``polling``: from the build's publication to the attempt's
    start; then its stages. When the controller's release of the same
    commit is known (``releases``), the cycle starts there and its stages
    come first."""
    phases: dict[str, float] = {}
    seen_at = attempt.get("published_at") or attempt.get("started_at")
    commit = attempt.get("commit")
    release = next(
        (
            run
            for run in releases
            if commit
            and run.get("commit") == commit
            and run.get("state") == "published"
        ),
        None,
    )
    if release is not None:
        for stage, item in (release.get("stages") or {}).items():
            if isinstance(item, Mapping) and str(stage) in RELEASE_PHASES:
                _add(phases, RELEASE_PHASES[str(stage)], item.get("seconds"))
        seen_at = release.get("requested_at") or seen_at
        _add(
            phases,
            "polling",
            between(release.get("finished_at"), attempt.get("started_at")),
        )
    else:
        _add(
            phases,
            "polling",
            between(attempt.get("published_at"), attempt.get("started_at")),
        )
    for stage in attempt.get("stages") or ():
        if isinstance(stage, Mapping) and str(stage.get("name")) in HOST_PHASES:
            _add(phases, HOST_PHASES[str(stage.get("name"))], stage.get("seconds"))
    return cycle(
        target=host,
        kind="nix-host",
        commit=commit,
        seen_at=seen_at,
        checked_at=attempt.get("ended_at"),
        ok=attempt.get("state") == "deployed",
        phases=phases,
        state=attempt.get("state"),
    )
