"""Run journals and controller events shaped like a real composition controller's.

:func:`write_run` writes a ``piceli.deploy-run.v1`` journal with the real
:class:`~piceli.pipeline.journal.Journal` (stages, plan, checks and failure
as the runner records them); :func:`sample_history` builds a whole
``piceli.gitops-history.v1`` document with the real
:mod:`piceli.gitops.history` builder, for the UI service tests and browser
journeys. Generic names only.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from piceli.gitops import history
from piceli.gitops.ports import EnvOutcome
from piceli.pipeline.journal import Journal, write_private

PRODUCT = "3f9c2d1e8a7b4c5d6e0f1a2b3c4d5e6f7a8b9c0d"
PRODUCT_OLD = "9e8d7c6b5a4f3e2d1c0b9a8f7e6d5c4b3a2f1e0d"
ASSETS = "b7e4a1c9d2f3081726354a5b6c7d8e9f0a1b2c3d"
BRANCH = "c1d2e3f4a5b60718293a4b5c6d7e8f9001122334"
WEB = "sha256:" + "a" * 64
WEB_OLD = "sha256:" + "1" * 64
WORKER = "sha256:" + "b" * 64
MEDIA = "sha256:" + "c" * 64
PLAN_HASH = "sha256:" + "d" * 64
CHECKS = [
    {"name": "web-home", "type": "http", "passed": True, "code": None,
     "detail": "GET / returned 200", "duration": 0.084},
    {"name": "worker-ready", "type": "exec", "passed": True, "code": None,
     "detail": "sh exited 0", "duration": 1.25},
]  # fmt: skip


def combined(n: int) -> str:
    return "sha256:" + f"{n:x}".rjust(64, "e")


def write_run(
    state_dir: Path,
    *,
    combined_hash: str,
    release: str,
    sources: Mapping[str, Mapping[str, str]],
    changes: Sequence[Mapping[str, str]] = (),
    checks: Sequence[Mapping[str, Any]] = CHECKS,
    applied: bool = True,
    failed_check: str | None = None,
    policy: bool = False,
    at: tuple[str, str] | None = None,
) -> str:
    """A finished run journal under ``state_dir/runs``; its run id.

    ``at``: its ``(created_at, updated_at)`` instead of now.
    """
    journal = Journal(state_dir)
    run = journal.create(
        pipeline={
            "app": "shop",
            "owner": "piceli",
            "field_manager": "piceli",
            "target": {"context": "piceli-incluster", "namespace": "piceli-test"},
        },
        combined_hash=combined_hash,
        until="checks",
        approval=combined_hash,
        plan={
            "hashed": {},
            "stages": {
                "inputs": {
                    "sources": {
                        name: {"commit": value["commit"], "dirty": False}
                        for name, value in sources.items()
                    }
                }
            },
        },
        refs={name: dict(value) for name, value in sources.items()},
        approved_by="policy" if policy else None,
    )
    run.set_stage("inputs", state="done", seconds=0.4)
    run.set_stage("build", state="skipped", seconds=0.0)
    run.set_stage("deliver", state="skipped", seconds=0.0)
    counts = {"no-op": 7}
    for item in changes:
        counts[item["operation"]] = counts.get(item["operation"], 0) + 1
    run.set_stage(
        "plan",
        state="done",
        seconds=1.2,
        output={
            "state": "planned",
            "release": release,
            "mode": "apply",
            "summary": counts,
            "changes": [dict(item) for item in changes],
            "drift": [],
        },
    )
    run.set_stage(
        "apply",
        state="done" if applied else "skipped",
        seconds=4.5 if applied else 0.0,
        output={"release": release} if applied else {},
    )
    results = [dict(item) for item in checks]
    if failed_check is not None:
        results.append(
            {
                "name": failed_check,
                "type": "exec",
                "passed": False,
                "code": "check-failed",
                "detail": "sh exited 1, expected 0",
                "duration": 0.31,
            }
        )
    output = {
        "passed": failed_check is None,
        "results": results,
        "checks_hash": "sha256:" + "f" * 64,
    }
    if failed_check is None:
        run.set_stage("checks", state="done", seconds=1.6, output=output)
        run.set_state("ready")
    else:
        run.set_stage(
            "checks",
            state="failed",
            seconds=1.6,
            output=output,
            reason="pipeline-checks-failed",
            message=f"release {release} failed its checks",
        )
        run.set_state("stopped", reason="pipeline-checks-failed")
    if at is not None:
        run.data["created_at"], run.data["updated_at"] = at
        write_private(run.path, json.dumps(run.data, sort_keys=True, indent=2) + "\n")
    return run.run_id


def _record(
    *,
    state: str,
    revision: Mapping[str, str],
    components: Mapping[str, Mapping[str, Any]],
    action: str | None = None,
    health: str = "healthy",
    reason: str | None = None,
    failure: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "branch": "main",
        "namespace": "piceli-test",
        "state": state,
        "health": health,
        "last_action": action,
        "reason": reason,
        "failure": dict(failure) if failure else None,
        "revision": dict(revision),
        "refs": dict.fromkeys(revision, "refs/heads/main"),
        "components": {name: dict(value) for name, value in components.items()},
    }


def _component(source: str, commit: str, digest: str, state: str) -> dict[str, Any]:
    return {"source": source, "commit": commit, "digest": digest, "state": state}


def sample_history(state_dir: Path) -> dict[str, Any]:
    """Three runs of ``main`` (newest: a failing check; then a deploy that
    rolled web after a CLI approval; the first deploy, recorded only as a
    run journal) and a failed build of ``wp-login`` (no run)."""
    main_dir = state_dir / "pipelines" / "main" / "environments" / "main"
    first = write_run(
        main_dir,
        combined_hash=combined(1),
        release="shop-1a2b3c4d5e6f",
        sources={
            "product": {"ref": "refs/heads/main", "commit": PRODUCT_OLD},
            "assets": {"ref": "refs/heads/main", "commit": ASSETS},
        },
        changes=[
            {"operation": "create", "kind": "Deployment", "name": "web"},
            {"operation": "create", "kind": "Deployment", "name": "worker"},
            {"operation": "create", "kind": "Service", "name": "web"},
        ],
        policy=True,
        at=("2026-10-01T08:10:04.120Z", "2026-10-01T08:10:12.004Z"),
    )
    second = write_run(
        main_dir,
        combined_hash=combined(2),
        release="shop-2b3c4d5e6f70",
        sources={
            "product": {"ref": "refs/heads/main", "commit": PRODUCT},
            "assets": {"ref": "refs/heads/main", "commit": ASSETS},
        },
        changes=[{"operation": "update", "kind": "Deployment", "name": "web"}],
        at=("2026-10-01T09:21:10.500Z", "2026-10-01T09:21:58.250Z"),
    )
    third = write_run(
        main_dir,
        combined_hash=combined(3),
        release="shop-2b3c4d5e6f70",
        sources={
            "product": {"ref": "refs/heads/main", "commit": PRODUCT},
            "assets": {"ref": "refs/heads/main", "commit": ASSETS},
        },
        applied=False,
        failed_check="deliberate-failure",
        at=("2026-10-01T09:26:20.010Z", "2026-10-01T09:26:59.900Z"),
    )
    revision = {"product": PRODUCT, "assets": ASSETS}
    events: dict[str, list[dict[str, Any]]] = {"main": [], "wp-login": []}
    deployed = _record(
        state="deployed",
        action="deployed",
        revision=revision,
        components={
            "web": _component("product", PRODUCT, WEB, "synced"),
            "worker": _component("product", PRODUCT, WORKER, "unchanged"),
            "media": _component("assets", ASSETS, MEDIA, "unchanged"),
        },
    )
    events["main"].append(
        history.event(
            deployed,
            started_at="2026-10-01T09:20:00Z",
            finished_at="2026-10-01T09:22:00Z",
            trigger="push product/main",
            approval={"via": "cli", "at": "2026-10-01T09:19:30Z"},
            outcome=EnvOutcome(
                state="deployed",
                plan_hash=PLAN_HASH,
                run_id=second,
                combined_hash=combined(2),
            ),
            built=["web"],
        )
    )
    degraded = _record(
        state="deployed",
        action="verified",
        health="degraded",
        reason="pipeline-checks-failed",
        revision=revision,
        components={
            "web": _component("product", PRODUCT, WEB, "unchanged"),
            "worker": _component("product", PRODUCT, WORKER, "unchanged"),
            "media": _component("assets", ASSETS, MEDIA, "unchanged"),
        },
    )
    degraded["verification"] = {
        "state": "failed",
        "trigger": "checks-changed",
        "checks_hash": "sha256:" + "f" * 64,
        "at": "2026-10-01T09:27:00Z",
        "failed": [{"check": "deliberate-failure", "code": "check-failed"}],
    }
    events["main"].append(
        history.event(
            degraded,
            started_at="2026-10-01T09:26:00Z",
            finished_at="2026-10-01T09:27:00Z",
            trigger="checks-changed",
            approval=None,
            outcome=None,
            built=[],
        )
    )
    failed = _record(
        state="failed",
        revision={"product": BRANCH, "assets": ASSETS},
        reason="component-build-failed",
        failure={"log_tail": "step 3/7: RUN npm ci\nnpm ERR! missing script: build"},
        components={
            "web": {
                "source": "product",
                "commit": BRANCH,
                "digest": None,
                "state": "failed",
            }
        },
    )
    failed["branch"] = "wp-login"
    failed["namespace"] = None
    failed["refs"] = {"product": "refs/heads/wp-login", "assets": "refs/heads/main"}
    events["wp-login"].append(
        history.event(
            failed,
            started_at="2026-10-01T09:29:00Z",
            finished_at="2026-10-01T09:29:40Z",
            trigger="push product/wp-login",
            approval=None,
            outcome=None,
            built=[],
        )
    )
    del third  # joined to the failing verification by the step's time window
    envs = {
        "main": {"namespace": "piceli-test"},
        "wp-login": {"namespace": None},
    }
    del first  # listed from its journal alone (recorded before 0.14.6)
    document = history.history(envs, events, state_dir, at="2026-10-01T09:30:00Z")
    return document
