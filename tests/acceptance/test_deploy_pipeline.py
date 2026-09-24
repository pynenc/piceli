"""Acceptance: ``piceli deploy`` runs a Pipeline end to end against the fake API.

Builds and deliveries go through a fake :class:`~piceli.pipeline.backend.Backend`
(no docker, no registry); planning, apply, rollback and resume go through the
real release engine against the in-process fake API server.
"""

from __future__ import annotations

import hashlib
import json
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from typer.testing import CliRunner

from piceli.k8s.cli import app as cli
from piceli.pipeline.backend import Backend
from tests.acceptance.fake_api import TARGET, serve

BUILDER = "sha256:" + "b" * 64
BASE = "sha256:" + "c" * 64
CACHE = "docker.io/library/redis:7.2.3@sha256:" + "d" * 64

BUILD_TOML = f"""
revision = "piceli.build-spec.v1"
name = "shop"

[builder]
image = "docker.io/library/busybox"
digest = "{BUILDER}"

[build]
platforms = ["linux/amd64"]
command = ["true"]

[context.src]
include = ["src/*.txt"]

[[output.image]]
name = "web"
repository = "example/web"
tag = "{{image_id:12}}"
base = {{ image = "docker.io/library/busybox", digest = "{BASE}" }}
"""

PIPELINE = f"""
from piceli import App, Build, Pipeline, Random, Registry, Secrets, Target

target = Target.kubeconfig(
    "kubeconfig", context="fake", namespace="{TARGET.namespace}",
    transport="loopback-http",
)
app = App("shop")
secrets = Secrets(password=Random(24))
images = Build.spec("build.toml")
credentials = app.secret("credentials", {{"password": secrets.ref("password")}})
app.deployment("cache", image="{CACHE}", env={{"PASSWORD": credentials.key("password")}})
app.deployment("web", image=images["web"], ports=[8080])

CHECKS = []

pipeline = Pipeline(
    app, target, build=images, deliver=Registry("oci://registry.example:5000/shop"),
    secrets=secrets, checks=CHECKS, rollback_on_failed_checks=True,
    state_dir="state",
    execution={{"max_seconds": 30, "readiness_seconds": 1, "poll_seconds": 0.05}},
)
"""


class Check:
    """A fake check: passes while ``PASS[0]`` is true."""

    def describe(self) -> dict[str, Any]:
        return {"type": "fake", "path": "/"}


class Report:
    def __init__(self, passed: bool) -> None:
        self.passed = passed
        self.results = [{"check": "fake", "passed": passed}]


class FakeBackend(Backend):
    """Builds and deliveries in memory; counts every side effect."""

    image_id = "sha256:" + "1" * 64
    local: set[str] = set()
    registry: dict[str, set[str]] = {}
    calls: list[str] = []
    fail_delivery: list[str] = []
    passing = [True]
    contexts: list[Any] = []

    @classmethod
    def reset(cls) -> None:
        cls.image_id = "sha256:" + "1" * 64
        cls.local, cls.registry, cls.calls = set(), {}, []
        cls.fail_delivery, cls.passing, cls.contexts = [], [True], []

    def record_sources(self, inputs: Any, lock: Any) -> dict[str, Any]:
        return {"sources": []}

    def image_present(self, image_id: str) -> bool:
        return image_id in self.local

    def build(self, spec, grant, output_dir, *, inputs, lock, log, progress):  # type: ignore[no-untyped-def]
        assert grant.plan_hash == spec.plan(inputs).plan_hash
        assert grant.builder_digest == BUILDER
        self.calls.append("build")
        progress("step 1/1")
        self.local.add(self.image_id)
        return {
            "revision": "piceli.build-receipt.v1",
            "state": "succeeded",
            "plan_hash": grant.plan_hash,
            "outputs": {
                "files": {},
                "images": {
                    image.name: {
                        "image_id": self.image_id,
                        "digest": None,
                        "platform": spec.platforms[0],
                        "ref": f"{image.repository}:{self.image_id[7:19]}",
                    }
                    for image in spec.images
                },
            },
        }

    def registry_present(self, route, manifests):  # type: ignore[no-untyped-def]
        self.calls.append("head")
        return [digest in self.registry.get(repo, set()) for repo, digest in manifests]

    def registry_deliver(self, route, image_id, repository):  # type: ignore[no-untyped-def]
        self.calls.append("deliver")
        if self.fail_delivery:
            reason = self.fail_delivery.pop()
            return {"state": "failed", "result": "failed", "reason": reason}
        manifest = "sha256:" + hashlib.sha256(image_id.encode()).hexdigest()
        self.registry.setdefault(repository, set()).add(manifest)
        host = route.push
        return {
            "schema": "piceli.registry-delivery.v1",
            "result": "pushed",
            "state": "succeeded",
            "approved_digest": image_id,
            "image": {"config_digest": image_id, "manifest_digest": manifest},
            "target": {"registry": host, "repository": repository},
            "node_registry": host,
            "pull_ref": f"{host}/{repository}@{manifest}",
        }

    def check_runner(self):  # type: ignore[no-untyped-def]
        def run(checks, context):  # type: ignore[no-untyped-def]
            self.contexts.append(context)
            return Report(self.passing[0])

        return run


@pytest.fixture
def shop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Any, Path]]:
    FakeBackend.reset()
    monkeypatch.setattr("piceli.pipeline.runner.Backend", FakeBackend)
    with serve() as (api, url):
        (tmp_path / "kubeconfig").write_text(
            textwrap.dedent(
                f"""
                apiVersion: v1
                kind: Config
                current-context: must-not-be-used
                clusters: [{{name: fake, cluster: {{server: "{url}"}}}}]
                users: [{{name: nobody, user: {{}}}}]
                contexts:
                - {{name: fake, context: {{cluster: fake, user: nobody}}}}
                - {{name: must-not-be-used, context: {{cluster: fake, user: nobody}}}}
                """
            )
        )
        (tmp_path / "build.toml").write_text(BUILD_TOML)
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.txt").write_text("v1\n")
        (tmp_path / "app.py").write_text(PIPELINE)
        yield api, tmp_path


SCHEMA = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "docs"
        / "schemas"
        / "piceli-deploy-event-v1.schema.json"
    ).read_text()
)


def deploy(tmp_path: Path, *args: str) -> tuple[int, list[dict[str, Any]], Any]:
    result = CliRunner().invoke(
        cli, ["deploy", str(tmp_path / "app.py:pipeline"), *args]
    )
    lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    for line in lines:
        jsonschema.validate(line, SCHEMA)  # every stdout line follows the schema
    return result.exit_code, lines, result


def image(api: Any, name: str = "web") -> str:
    return str(
        api.objects[("Deployment", name)]["spec"]["template"]["spec"]["containers"][0][
            "image"
        ]
    )


def with_checks(tmp_path: Path) -> None:
    text = (tmp_path / "app.py").read_text()
    (tmp_path / "app.py").write_text(
        text.replace(
            "CHECKS = []",
            "from tests.acceptance.test_deploy_pipeline import Check\nCHECKS = [Check()]",
        )
    )


def test_deploy_rerun_is_noop_and_one_change_moves_one_deployment(shop) -> None:
    api, tmp_path = shop
    code, events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 0, result.stdout + result.stderr
    final = events[-1]
    assert final["event"] == "result" and final["state"] == "ready"
    assert final["stages"] == {
        "inputs": "done",
        "build": "done",
        "deliver": "done",
        "plan": "done",
        "apply": "done",
        "checks": "skipped",
    }
    stage_events = [(e["stage"], e["state"]) for e in events if e["event"] == "stage"]
    assert ("build", "running") in stage_events and ("apply", "done") in stage_events
    assert all(e["schema"] == "piceli.deploy-event.v1" for e in events)
    first = image(api)
    manifest = "sha256:" + hashlib.sha256(FakeBackend.image_id.encode()).hexdigest()
    assert first == f"registry.example:5000/shop/web@{manifest}"
    assert image(api, "cache") == CACHE
    assert FakeBackend.calls == ["build", "deliver"]
    # Feedback 4: the release source is the whole image set.
    catalog = json.loads((tmp_path / "state" / "release" / "catalog.json").read_text())
    source = catalog["releases"][0]["source"]
    assert source["kind"] == "oci-set"
    assert source["images"] == {"cache": "sha256:" + "d" * 64, "web": manifest}

    # Rerun without changes: every stage is skipped by content identity.
    started = time.monotonic()
    code, events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 0, result.stdout + result.stderr
    assert time.monotonic() - started < 10
    assert events[-1]["stages"] == {
        "inputs": "done",
        "build": "skipped",
        "deliver": "skipped",
        "plan": "done",
        "apply": "skipped",
        "checks": "skipped",
    }
    assert FakeBackend.calls == ["build", "deliver", "head"]
    assert image(api) == first

    # One source change: one build, one delivery, one Deployment changes.
    (tmp_path / "src" / "main.txt").write_text("v2\n")
    FakeBackend.image_id = "sha256:" + "2" * 64
    code, events, result = deploy(tmp_path, "--plan")
    assert code == 0
    planned = events[-1]
    assert planned["state"] == "planned"
    assert planned["stages"]["build"]["builds"]["shop"]["action"] == "build"
    assert planned["stages"]["plan"]["state"] == "pending"
    assert "combined hash:" in result.stderr
    code, events, result = deploy(tmp_path, "--approve", planned["combined_hash"])
    assert code == 0, result.stdout + result.stderr
    assert FakeBackend.calls[-2:] == ["build", "deliver"]
    assert image(api) != first
    plan = json.loads(
        next(
            path.read_text()
            for path in sorted((tmp_path / "state" / "runs").glob("*.json"))[-1:]
        )
    )["stages"]["plan"]["output"]
    changed = {(c["kind"], c["name"]) for c in plan["changes"]}
    # The carried-over Secret matches privately, so it is a no-op.
    assert changed == {("Deployment", "web")}


def test_plan_changed_and_approval_required(shop) -> None:
    api, tmp_path = shop
    code, events, _ = deploy(tmp_path)
    assert code == 3
    assert events[-1]["state"] == "approval-required"
    assert ("Deployment", "web") not in api.objects
    code, events, _ = deploy(tmp_path, "--approve", "0" * 64)
    assert code == 2
    assert (events[-1]["state"], events[-1]["reason"]) == (
        "rejected",
        "pipeline-plan-changed",
    )
    code, events, _ = deploy(tmp_path, "--plan", "--auto-approve")
    assert code == 2 and events[-1]["reason"] == "deploy-flags-conflict"
    code, events, _ = deploy(tmp_path, "--until", "nowhere")
    assert code == 2 and events[-1]["reason"] == "deploy-stage-unknown"
    code, events, _ = deploy(tmp_path, "--resume")
    assert code == 2 and events[-1]["reason"] == "pipeline-nothing-to-resume"
    assert FakeBackend.calls == []


def test_until_stops_early(shop) -> None:
    api, tmp_path = shop
    code, events, result = deploy(tmp_path, "--auto-approve", "--until", "deliver")
    assert code == 0, result.stdout + result.stderr
    assert events[-1]["state"] == "stopped"
    assert events[-1]["stages"]["plan"] == "pending"
    assert ("Deployment", "web") not in api.objects
    # The next full run reuses the build and the delivery.
    code, events, result = deploy(tmp_path, "--auto-approve")
    assert code == 0, result.stdout + result.stderr
    assert events[-1]["stages"]["build"] == "skipped"
    assert events[-1]["stages"]["deliver"] == "skipped"
    assert FakeBackend.calls == ["build", "deliver", "head"]


def test_failed_delivery_resumes_at_the_failed_stage(shop) -> None:
    api, tmp_path = shop
    FakeBackend.fail_delivery.append("registry-unreachable")
    code, events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 1, result.stdout + result.stderr
    final = events[-1]
    assert final["reason"] == "registry-unreachable"
    assert final["stage"] == "deliver"
    assert final["stages"]["build"] == "done"
    assert "--resume" in result.stderr
    run_id = final["run_id"]

    code, events, result = deploy(tmp_path, "--resume", "--json")
    assert code == 0, result.stdout + result.stderr
    assert events[-1]["run_id"] == run_id
    assert events[-1]["state"] == "ready"
    running = [e["stage"] for e in events if e.get("state") == "running"]
    assert running == ["deliver", "plan", "apply", "checks"]
    assert FakeBackend.calls == ["build", "deliver", "deliver"]
    assert image(api).startswith("registry.example:5000/shop/web@sha256:")


def test_not_ready_apply_resumes_the_same_execution(shop) -> None:
    api, tmp_path = shop
    api.ready = False
    code, events, result = deploy(tmp_path, "--auto-approve")
    assert code == 1, result.stdout + result.stderr
    assert events[-1]["reason"] == "pipeline-apply-not-ready"
    assert events[-1]["stage"] == "apply"
    api.ready = True
    code, events, result = deploy(tmp_path, "--resume")
    assert code == 0, result.stdout + result.stderr
    assert events[-1]["state"] == "ready"
    history = json.loads((tmp_path / "state" / "release" / "history.json").read_text())[
        "entries"
    ]
    assert [entry["intent"] for entry in history] == ["apply", "resume"]
    assert history[0]["execution_id"] == history[1]["execution_id"]


def test_failed_checks_roll_back_to_the_previous_release(shop) -> None:
    api, tmp_path = shop
    with_checks(tmp_path)
    code, events, result = deploy(tmp_path, "--auto-approve")
    assert code == 0, result.stdout + result.stderr
    assert events[-1]["stages"]["checks"] == "done"
    good = image(api)
    context = FakeBackend.contexts[-1]
    assert context.namespace == TARGET.namespace and context.context == "fake"
    assert context.images["web"] == good

    # Unchanged rerun: the verified release is not checked again.
    code, events, _ = deploy(tmp_path, "--auto-approve")
    assert code == 0 and events[-1]["stages"]["checks"] == "skipped"
    assert len(FakeBackend.contexts) == 1

    (tmp_path / "src" / "main.txt").write_text("broken\n")
    FakeBackend.image_id = "sha256:" + "3" * 64
    FakeBackend.passing[0] = False
    code, events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 1, result.stdout + result.stderr
    final = events[-1]
    assert final["reason"] == "pipeline-checks-failed"
    assert final["state"] == "rolled-back"
    checks = [e for e in events if e["event"] == "stage" and e["stage"] == "checks"][-1]
    assert checks["state"] == "failed"
    run = json.loads(
        sorted((tmp_path / "state" / "runs").glob("*.json"))[-1].read_text()
    )
    rollback = run["stages"]["checks"]["output"]["rollback"]
    assert rollback["state"] == "ready"
    assert image(api) == good
    # A rolled-back run is finished: there is nothing to resume.
    code, events, _ = deploy(tmp_path, "--resume")
    assert code == 2 and events[-1]["reason"] == "pipeline-nothing-to-resume"


def test_checks_without_a_runner_are_refused(shop, monkeypatch) -> None:
    api, tmp_path = shop
    with_checks(tmp_path)
    monkeypatch.setattr(FakeBackend, "check_runner", Backend.check_runner)
    monkeypatch.setattr(
        "importlib.import_module",
        lambda name, *a: (_ for _ in ()).throw(ImportError(name)),
    )
    code, events, result = deploy(tmp_path, "--auto-approve")
    assert code == 2, result.stdout + result.stderr
    assert events[-1]["reason"] == "pipeline-checks-unavailable"
    assert events[-1]["stage"] == "checks"
