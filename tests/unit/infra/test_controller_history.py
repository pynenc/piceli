"""The composition controller's published run history (``piceli-gitops-history``).

Reproduces the 0.14.5 gap: the controller kept run journals on its volume
but published nothing the in-cluster UI could read. The controller here runs
against local Git sources (as in ``test_controller.py``); its deploys write
real run journals with :class:`~piceli.pipeline.journal.Journal`.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from piceli.gitops import history
from piceli.gitops.state import DirectoryChannel, approve_request, sync_request
from piceli.pipeline.errors import PipelineError
from tests.gitops_history_fixture import write_run
from tests.unit.infra.test_controller import FakePorts, world

__all__ = ["world"]

PLAN = "sha256:" + "d" * 64
COMBINED = "sha256:" + "9" * 64


class JournalPorts(FakePorts):
    """Fake deploys that journal real runs where the controller's pipelines do."""

    def __init__(self, state_dir: Path, clock: dict[str, float]) -> None:
        super().__init__()
        self.state_dir = state_dir
        self.clock = clock
        self.fail_check: str | None = None
        self.measured: list[Any] = []

    def _runs(self, name: str) -> Path:
        return self.state_dir / "pipelines" / name / "environments" / name

    def env_up(self, pipeline: Any, name: str, **kwargs: Any) -> dict[str, Any]:
        approve = kwargs["approve"]
        if approve is None:
            return {
                "state": "approval-required",
                "plan_hash": PLAN,
                "create_namespace": False,
                "stop": [],
                "images": {"web": "registry.example:5000/shop/web@sha256:" + "a" * 64},
                "deploy": {
                    "combined_hash": COMBINED,
                    "stages": {
                        "plan": {
                            "state": "planned",
                            "release": "shop-0123456789ab",
                            "summary": {"update": 1, "no-op": 4},
                            "changes": [
                                {
                                    "operation": "update",
                                    "kind": "Deployment",
                                    "name": "web",
                                },
                                {
                                    "operation": "no-op",
                                    "kind": "Service",
                                    "name": "web",
                                },
                            ],
                        }
                    },
                },
            }
        self.clock["now"] = time.time()
        run_id = write_run(
            self._runs(name),
            combined_hash=COMBINED,
            release="shop-0123456789ab",
            sources={"shop": {"ref": "refs/heads/main", "commit": "a" * 40}},
            changes=[{"operation": "update", "kind": "Deployment", "name": "web"}],
            policy=approve == "policy",
            applied=self.fail_check is None,
            failed_check=self.fail_check,
        )
        if self.fail_check is not None:
            raise PipelineError(
                "pipeline-checks-failed",
                "release shop-0123456789ab failed its checks",
                failed=True,
                details={
                    "run_state": "failed",
                    "output": {
                        "passed": False,
                        "checks_hash": "sha256:" + "f" * 64,
                        "verification": {"trigger": "checks-changed", "applied": False},
                        "results": [
                            {
                                "name": self.fail_check,
                                "passed": False,
                                "code": "check-failed",
                                "detail": "sh exited 1, expected 0",
                            }
                        ],
                    },
                },
            )
        self.deployed.append((name, pipeline["images"]))
        return {
            "state": "ready",
            "namespace": f"ns-{name}",
            "plan_hash": PLAN,
            "result": {
                "state": "ready",
                "run_id": run_id,
                "combined_hash": COMBINED,
                "stages": {"plan": "done", "apply": "done", "checks": "done"},
            },
        }

    def registry_usage(self, registry: Any) -> dict[str, Any] | None:
        self.measured.append(registry.name)
        return {
            "used_bytes": 553_889_792,
            "used_source": "du",
            "claim": "piceli-registry-storage",
        }


@pytest.fixture
def journaled(world: dict[str, Any]) -> dict[str, Any]:
    controller = world["controller"]
    world["clock"]["now"] = time.time()
    ports = JournalPorts(controller.state_dir, world["clock"])
    controller.ports = ports
    world["ports"] = ports
    return world


def _published(channel: DirectoryChannel) -> dict[str, Any]:
    document = channel.read_history()
    assert document is not None, "the controller published no history"
    assert document["schema"] == history.SCHEMA
    return document


def _manual_main(controller: Any) -> None:
    from piceli.envs import Environment

    composition = controller.composition
    main = composition.environment("main")
    manual = Environment(
        "main",
        namespace=main.namespace,
        stack=main.stack,
        follow={source: list(items) for source, items in main.sources},
    )
    object.__setattr__(
        composition, "environments", (manual, *composition.environments[1:])
    )


def test_every_deploy_is_published_with_its_run(journaled: dict[str, Any]) -> None:
    controller, channel = journaled["controller"], journaled["channel"]
    controller.poll_once()
    runs = _published(channel)["envs"]["main"]["runs"]
    assert len(runs) == 1
    run = runs[0]
    assert run["state"] == "deployed" and run["action"] == "deployed"
    assert run["recorded_by"] == "controller+run"
    assert run["run_id"] and run["combined_hash"] == COMBINED
    assert run["plan_hash"] == PLAN
    assert run["trigger"]  # what made the controller deploy
    assert set(run["sources"]) == {"shop", "catalog"}
    assert run["sources"]["shop"]["ref"] == "refs/heads/main"
    assert run["built"] == ["api", "cache", "catalog", "web"]  # built or mirrored
    assert run["rolled"] == ["api", "cache", "catalog", "web"]
    assert {item["name"]: item["built"] for item in run["components"]} == {
        "api": True,
        "cache": True,  # mirrored once
        "catalog": True,
        "web": True,
    }
    assert all(item["digest"].startswith("sha256:") for item in run["components"])
    # 0.16.0: a policy approval has its time too (it was null before).
    assert run["approved_by"]["via"] == "policy" and run["approved_by"]["at"]
    assert run["checks"]["passed"] is True
    assert [item["name"] for item in run["checks"]["results"]] == [
        "web-home",
        "worker-ready",
    ]
    assert run["checks"]["results"][0]["duration"] == 0.084
    assert run["plan"]["changes"] == [
        {"operation": "update", "kind": "Deployment", "name": "web"}
    ]

    # A change of web's files: only web is built and rolled.
    journaled["shop"].commit({"web/index.html": "<h1>shop v2</h1>\n"})
    controller.poll_once()
    runs = _published(channel)["envs"]["main"]["runs"]
    assert len(runs) == 2
    assert runs[0]["built"] == ["web"] and runs[0]["rolled"] == ["web"]
    assert runs[0]["unchanged"] == ["api", "cache", "catalog"]


def test_the_owner_reviews_the_pending_plan_and_is_recorded_as_approver(
    journaled: dict[str, Any],
) -> None:
    controller, channel = journaled["controller"], journaled["channel"]
    _manual_main(controller)
    status = controller.poll_once()
    main = status["envs"]["main"]
    assert main["state"] == "approval-required"
    assert main["pending_plan"]["plan_hash"] == PLAN == main["plan_hash"]
    assert main["pending_plan"]["combined_hash"] == COMBINED
    assert main["pending_plan"]["changes"] == [
        {"operation": "update", "kind": "Deployment", "name": "web"}
    ]
    assert main["pending_plan"]["changes_total"] == 1
    assert main["pending_plan"]["counts"] == {"update": 1, "no-op": 4}
    waiting = _published(channel)["envs"]["main"]["runs"]
    assert waiting[0]["state"] == "approval-required" and waiting[0]["run_id"] is None

    channel.add_request(*approve_request("main", PLAN, via="cli"))
    status = controller.poll_once()
    main = status["envs"]["main"]
    assert main["state"] == "deployed"
    assert "pending_plan" not in main and "approval" not in main
    runs = _published(channel)["envs"]["main"]["runs"]
    assert runs[0]["state"] == "deployed"
    assert runs[0]["approved_by"]["via"] == "cli" and runs[0]["approved_by"]["at"]
    assert runs[0]["run_id"]


def test_a_failing_check_and_a_failed_build_are_in_the_history(
    journaled: dict[str, Any],
) -> None:
    from piceli.infra import CompositionError

    controller, channel, ports = (
        journaled["controller"],
        journaled["channel"],
        journaled["ports"],
    )
    controller.poll_once()
    ports.fail_check = "deliberate-failure"
    channel.add_request(*sync_request("main"))
    status = controller.poll_once()
    assert status["envs"]["main"]["health"] == "degraded"
    runs = _published(channel)["envs"]["main"]["runs"]
    assert len(runs) == 2  # the failing run is joined to its step, not listed twice
    degraded = runs[0]
    assert degraded["state"] == "degraded" and degraded["trigger"] == "sync"
    assert degraded["recorded_by"] == "controller+run"
    assert degraded["rolled"] == []
    assert degraded["verification"]["state"] == "failed"
    assert degraded["checks"]["passed"] is False
    failing = [item for item in degraded["checks"]["results"] if not item["passed"]]
    assert failing == [
        {
            "name": "deliberate-failure",
            "type": "exec",
            "passed": False,
            "code": "check-failed",
            "detail": "sh exited 1, expected 0",
            "duration": 0.31,
        }
    ]
    assert degraded["failure"]["failed_checks"] == [
        {"name": "deliberate-failure", "code": "check-failed"}
    ]

    def build(items: Any, checkout: Any) -> Any:
        raise CompositionError(
            "component-build-failed",
            "the component build Job ended failed",
            details={"outcome": {"state": "failed", "log_tail": "npm ERR! build"}},
        )

    ports.fail_check = None
    ports.builder.build = build
    channel.add_request(*sync_request("main", "web"))
    controller.poll_once()
    failed = _published(channel)["envs"]["main"]["runs"][0]
    assert failed["state"] in {"retrying", "failed"}
    assert failed["run_id"] is None and failed["recorded_by"] == "controller"
    assert failed["failure"]["log_tail"] == "npm ERR! build"
    assert failed["failure"]["reason"] == "component-build-failed"


def test_runs_recorded_before_the_controller_kept_events_are_listed(
    journaled: dict[str, Any],
) -> None:
    controller, channel = journaled["controller"], journaled["channel"]
    old = write_run(
        controller.state_dir / "pipelines" / "main" / "environments" / "main",
        combined_hash="sha256:" + "7" * 64,
        release="shop-old",
        sources={"shop": {"ref": "refs/heads/main", "commit": "b" * 40}},
        at=("2026-09-30T10:00:00.000Z", "2026-09-30T10:01:00.000Z"),
    )
    controller.poll_once()
    runs = _published(channel)["envs"]["main"]["runs"]
    assert runs[-1]["run_id"] == old and runs[-1]["recorded_by"] == "run"
    assert runs[-1]["state"] == "deployed" and runs[-1]["trigger"] is None


def test_registry_use_is_measured_at_most_every_ten_minutes(
    journaled: dict[str, Any],
) -> None:
    from piceli.infra.controller import REGISTRY_USAGE_SECONDS
    from piceli.pipeline.model import Registry

    controller, ports = journaled["controller"], journaled["ports"]
    registry = Registry.in_cluster(on="node-a")
    object.__setattr__(controller.composition.cluster, "registry", registry)
    status = controller.poll_once()
    usage = status["controller"]["registry_usage"]
    assert usage["used_bytes"] == 553_889_792 and usage["used_source"] == "du"
    assert usage["claim"] == "piceli-registry-storage" and usage["measured_at"]
    controller.poll_once()
    assert len(ports.measured) == 1
    journaled["clock"]["now"] += REGISTRY_USAGE_SECONDS
    controller.poll_once()
    assert len(ports.measured) == 2


def test_the_history_is_bounded() -> None:
    entry = {"id": "x", "started_at": "2026-10-01T00:00:00Z", "pad": "p" * 1000}
    document = {
        "schema": history.SCHEMA,
        "envs": {"main": {"runs": [dict(entry) for _ in range(50)]}},
        "truncated": False,
    }
    bounded = history.bounded(document, limit=10_000)
    assert len(bounded["envs"]["main"]["runs"]) < 10 and bounded["truncated"]


def test_default_ports_measure_du_in_the_running_registry_pod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import contextmanager

    from piceli.infra.controller import DefaultCompositionPorts
    from piceli.pipeline.model import Registry

    pods = {
        "items": [
            {
                "metadata": {
                    "name": "piceli-registry-agent-x",
                    "labels": {
                        "app.kubernetes.io/instance": "piceli-registry",
                        "app.kubernetes.io/component": "mirror",
                    },
                },
                "status": {"phase": "Running"},
            },
            {
                "metadata": {
                    "name": "piceli-registry-5d9f",
                    "labels": {
                        "app.kubernetes.io/instance": "piceli-registry",
                        "app.kubernetes.io/component": "registry",
                    },
                },
                "status": {"phase": "Running"},
            },
        ]
    }
    calls: list[tuple[str, str]] = []

    class Api:
        client = object()

        def call(self, path: str, method: str) -> Any:
            calls.append((path, method))
            return pods

    @contextmanager
    def connect(*_args: Any, **_kwargs: Any) -> Any:
        yield Api()

    measured: list[str] = []

    def du(client: Any, namespace: str, pod: str) -> int:
        measured.append(f"{namespace}/{pod}")
        return 4096

    monkeypatch.setattr("piceli.gitops.install.connect", connect)
    monkeypatch.setattr("piceli.artifacts.registry_storage.read_disk_usage", du)
    ports = DefaultCompositionPorts(
        object(),
        object(),
        kubeconfig=tmp_path / "config",
        context="c",
        state_dir=tmp_path,
    )
    usage = ports.registry_usage(Registry.in_cluster(on="node-a"))
    assert usage == {
        "used_bytes": 4096,
        "used_source": "du",
        "claim": "piceli-registry-storage",
    }
    assert measured == ["piceli-system/piceli-registry-5d9f"]
    assert calls == [("/api/v1/namespaces/piceli-system/pods", "GET")]


KEPT = {
    "kind": "PersistentVolumeClaim",
    "name": "data-db-0",
    "namespace": "ns-main",
    "why": "claim",
    "command": "kubectl --namespace ns-main delete persistentvolumeclaim data-db-0",
}
PRUNE_CHANGES = [
    {"operation": "create", "kind": "StatefulSet", "name": "web"},
    {"operation": "delete", "kind": "Deployment", "name": "web", "prune": True},
    {"operation": "delete", "kind": "StatefulSet", "name": "db", "prune": True},
]


class PrunePorts(JournalPorts):
    """A deploy whose plan deletes two objects and keeps a claim."""

    def env_up(self, pipeline: Any, name: str, **kwargs: Any) -> dict[str, Any]:
        if kwargs["approve"] is None:
            planned = super().env_up(pipeline, name, **kwargs)
            stage = planned["deploy"]["stages"]["plan"]
            stage["changes"] = [dict(item) for item in PRUNE_CHANGES]
            stage["kept_orphaned"] = [dict(KEPT)]
            return planned
        self.clock["now"] = time.time()
        run_id = write_run(
            self._runs(name),
            combined_hash=COMBINED,
            release="shop-0123456789ab",
            sources={"shop": {"ref": "refs/heads/main", "commit": "a" * 40}},
            changes=PRUNE_CHANGES,
            kept_orphaned=[KEPT],
        )
        return {
            "state": "ready",
            "namespace": f"ns-{name}",
            "plan_hash": PLAN,
            "result": {
                "state": "ready",
                "run_id": run_id,
                "combined_hash": COMBINED,
                "stages": {"plan": "done", "apply": "done", "checks": "done"},
                "pruned": {
                    "deleted": [
                        {"kind": "Deployment", "name": "web"},
                        {"kind": "StatefulSet", "name": "db"},
                    ],
                    "kept_orphaned": [dict(KEPT)],
                },
            },
        }


def test_a_prune_is_reviewed_recorded_in_status_and_in_the_history(
    journaled: dict[str, Any],
) -> None:
    controller, channel = journaled["controller"], journaled["channel"]
    controller.ports = PrunePorts(controller.state_dir, journaled["clock"])
    _manual_main(controller)
    main = controller.poll_once()["envs"]["main"]
    assert main["state"] == "approval-required"
    pending = main["pending_plan"]
    assert {(c["operation"], c["kind"], c["name"]) for c in pending["changes"]} >= {
        ("delete", "Deployment", "web"),
        ("delete", "StatefulSet", "db"),
    }
    assert pending["kept_orphaned"] == [KEPT]

    channel.add_request(*approve_request("main", PLAN, via="cli"))
    main = controller.poll_once()["envs"]["main"]
    assert main["state"] == "deployed"
    assert main["deleted"] == [
        {"kind": "Deployment", "name": "web"},
        {"kind": "StatefulSet", "name": "db"},
    ]
    assert main["kept_orphaned"] == [KEPT]
    run = _published(channel)["envs"]["main"]["runs"][0]
    assert run["deleted"] == main["deleted"]
    assert run["kept_orphaned"] == [KEPT]
    assert {"operation": "delete", "kind": "StatefulSet", "name": "db"} in run["plan"][
        "changes"
    ]


class RolledBackPorts(JournalPorts):
    """A deploy whose checks fail and whose release is rolled back."""

    def __init__(self, state_dir: Path, clock: dict[str, float]) -> None:
        super().__init__(state_dir, clock)
        self.applies = 0
        self.rolled_back = True

    def env_up(self, pipeline: Any, name: str, **kwargs: Any) -> dict[str, Any]:
        if kwargs["approve"] is None or not self.rolled_back:
            return super().env_up(pipeline, name, **kwargs)
        self.applies += 1
        raise PipelineError(
            "pipeline-checks-failed",
            "release shop-0123456789ab failed its checks",
            failed=True,
            details={
                "run_state": "rolled-back",
                "output": {
                    "passed": False,
                    "results": [],
                    "rollback": {"state": "ready", "release": "shop-0011223344aa"},
                },
            },
        )


def test_a_rolled_back_release_is_not_retried_until_a_sync(
    journaled: dict[str, Any],
) -> None:
    """A failed check whose release was rolled back stops the environment:
    no retry every poll (apply, fail, roll back), until a new revision or a
    ``gitops sync``."""
    controller, channel = journaled["controller"], journaled["channel"]
    ports = RolledBackPorts(controller.state_dir, journaled["clock"])
    controller.ports = ports
    main = controller.poll_once()["envs"]["main"]
    assert main["state"] == "failed", main
    assert main["reason"] == "checks-failed-rolled-back"
    assert main["next_attempt_at"] is None and ports.applies == 1
    for _ in range(3):
        journaled["clock"]["now"] += 3600
        assert controller.poll_once()["envs"]["main"]["state"] == "failed"
    assert ports.applies == 1
    ports.rolled_back = False
    channel.add_request(*sync_request("main"))
    assert controller.poll_once()["envs"]["main"]["state"] == "deployed"
