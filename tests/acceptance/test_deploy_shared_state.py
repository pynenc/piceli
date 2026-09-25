"""Acceptance: ``piceli deploy`` with shared state (``state="cluster"``) across runners.

A "runner" is a state directory plus a local image store: each test switches
``DEPLOY_STATE_DIR`` and empties the fake backend's local images, so nothing
is shared through disk or build cache, only through the fake API server
(the Lease lock and the state Secrets in the namespace) and the plan file.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from typer.testing import CliRunner

from piceli.k8s.cli import app as cli
from tests.acceptance.test_deploy_pipeline import (
    PIPELINE,
    SCHEMA,
    FakeBackend,
    image,
    make_shop,
)

SHARED = PIPELINE.replace(
    "from piceli import",
    "import os\nfrom piceli import",
).replace(
    'state_dir="state",',
    'state_dir=os.environ["DEPLOY_STATE_DIR"], '
    'state=os.environ.get("DEPLOY_STATE", "cluster"), state_lease_seconds=5,',
)


@pytest.fixture
def shared(tmp_path, monkeypatch):
    for api, root in make_shop(tmp_path, monkeypatch):
        (root / "app.py").write_text(SHARED)
        yield api, root


class Runner:
    """One CI runner: its own state directory and its own local images."""

    def __init__(self, root: Path, name: str, *, state: str = "cluster") -> None:
        self.root = root
        self.state_dir = root / f"runner-{name}"
        self.state = state

    def invoke(self, *args: str) -> tuple[int, list[dict[str, Any]], Any]:
        FakeBackend.local = set()  # another machine: no local images
        for name in [n for n in sys.modules if n.startswith("_piceli_render_")]:
            del sys.modules[name]  # another process: the module is imported anew
        result = CliRunner().invoke(
            cli,
            list(args),
            env={"DEPLOY_STATE_DIR": str(self.state_dir), "DEPLOY_STATE": self.state},
        )
        try:
            lines = [
                json.loads(line) for line in result.stdout.splitlines() if line.strip()
            ]
        except ValueError:  # one indented object (piceli release …)
            lines = [json.loads(result.stdout)]
        return result.exit_code, lines, result

    def deploy(self, *args: str) -> tuple[int, list[dict[str, Any]], Any]:
        code, lines, result = self.invoke(
            "deploy", str(self.root / "app.py:pipeline"), *args
        )
        for line in lines:
            jsonschema.validate(line, SCHEMA)
        return code, lines, result


def _lease(api: Any) -> dict[str, Any]:
    return dict(api.objects[("Lease", "piceli-lock-shop")]["spec"])


def test_plan_on_one_runner_and_apply_the_plan_file_on_another(shared) -> None:
    api, root = shared
    planner, applier = Runner(root, "plan"), Runner(root, "apply")
    code, lines, result = planner.deploy("--plan", "--out", str(root / "plan.json"))
    assert code == 0, result.stdout + result.stderr
    planned = lines[-1]
    assert planned["state"] == "planned"
    assert planned["plan_file"] == str(root / "plan.json")
    assert "piceli deploy --apply" in result.stderr
    assert "holderIdentity" not in _lease(api)  # freed after the plan
    document = json.loads((root / "plan.json").read_text())
    assert document["schema"] == "piceli.deploy-plan-file.v1"
    assert document["combined_hash"] == planned["combined_hash"]
    assert document["observed_target"] == {
        "cluster_uid": "cluster-uid",
        "namespace_uid": "namespace-uid",
    }
    assert document["stages"]["plan"]["preview"]["approvable"] is False
    assert str(planner.state_dir) not in (root / "plan.json").read_text()

    # The apply job: another runner, no shared disk, the plan file only.
    code, lines, result = applier.invoke(
        "deploy",
        "--apply",
        str(root / "plan.json"),
        "--approve",
        planned["combined_hash"],
        "--json",
    )
    assert code == 0, result.stdout + result.stderr
    assert lines[-1]["state"] == "ready"
    assert FakeBackend.calls == ["build", "deliver"]
    assert image(api).startswith("registry.example:5000/shop/web@sha256:")
    assert "holderIdentity" not in _lease(api)

    release = lines[-1]["release"]

    # A third runner sees the release history (the shared catalog).
    code, lines, result = Runner(root, "third").invoke(
        "release", "status", "--spec", str(root / "app.py:pipeline")
    )
    assert code == 0, result.stdout + result.stderr
    assert lines[-1]["deployed"] == release


def test_apply_needs_no_build_cache_for_delivered_images(shared) -> None:
    _, root = shared
    builder, planner, applier = (Runner(root, n) for n in ("build", "plan", "apply"))
    code, lines, result = builder.deploy("--until", "deliver", "--auto-approve")
    assert code == 0, result.stdout + result.stderr
    assert FakeBackend.calls == ["build", "deliver"]
    code, lines, result = planner.deploy("--plan", "--out", str(root / "plan.json"))
    assert code == 0, result.stdout + result.stderr
    stages = lines[-1]["stages"]
    assert stages["build"]["builds"]["shop"]["action"] == "cached"
    assert stages["deliver"]["images"]["web"]["action"] == "present"
    assert stages["plan"]["state"] == "planned"
    document = json.loads((root / "plan.json").read_text())
    assert set(document["receipts"]["builds"]) == {"shop"}
    assert [key.partition("@")[0] for key in document["receipts"]["deliveries"]] == [
        "web"
    ]
    FakeBackend.calls.clear()
    code, lines, result = applier.invoke(
        "deploy",
        "--apply",
        str(root / "plan.json"),
        "--approve",
        lines[-1]["combined_hash"],
    )
    assert code == 0, result.stdout + result.stderr
    assert lines[-1]["state"] == "ready"
    assert "build" not in FakeBackend.calls and "deliver" not in FakeBackend.calls


def test_the_plan_file_cannot_widen_the_approval(shared) -> None:
    api, root = shared
    planner, applier = Runner(root, "plan"), Runner(root, "apply")
    code, lines, _ = planner.deploy("--plan", "--out", str(root / "plan.json"))
    good = lines[-1]["combined_hash"]
    path = str(root / "plan.json")

    def apply(*extra: str, file: str = path) -> tuple[int, dict[str, Any]]:
        code, lines, _ = applier.invoke("deploy", "--apply", file, *extra)
        return code, lines[-1]

    code, body = apply("--approve", "f" * 64)
    assert (code, body["reason"]) == (2, "deploy-plan-file-mismatch")
    assert apply()[1]["reason"] == "deploy-flags-conflict"
    assert apply("--approve", good, "--plan")[1]["reason"] == "deploy-flags-conflict"
    document = json.loads((root / "plan.json").read_text())
    tampered = root / "tampered.json"
    tampered.write_text(
        json.dumps({**document, "pipeline": {**document["pipeline"], "owner": "x"}})
    )
    assert apply("--approve", good, file=str(tampered))[1]["reason"] == (
        "deploy-plan-file-mismatch"
    )
    tampered.write_text(
        json.dumps(
            {
                **document,
                "observed_target": {"cluster_uid": "another", "namespace_uid": "x"},
            }
        )
    )
    assert apply("--approve", good, file=str(tampered))[1]["reason"] == (
        "deploy-plan-target-mismatch"
    )
    tampered.write_text("{}")
    assert apply("--approve", good, file=str(tampered))[1]["reason"] == (
        "deploy-plan-file-invalid"
    )
    # The live state changed after the review: re-planned, refused.
    (root / "src" / "main.txt").write_text("v2\n")
    code, body = apply("--approve", good)
    assert (code, body["reason"]) == (2, "pipeline-plan-changed")
    assert ("Deployment", "web") not in api.objects
    assert FakeBackend.calls == []


def test_resume_on_another_runner_after_the_runner_state_is_lost(shared) -> None:
    api, root = shared
    first, second = Runner(root, "first"), Runner(root, "second")
    FakeBackend.fail_delivery.append("registry-unreachable")
    code, lines, result = first.deploy("--auto-approve", "--json")
    assert code == 1 and lines[-1]["stage"] == "deliver", result.stdout
    run_id = lines[-1]["run_id"]
    shutil.rmtree(first.state_dir)  # the runner (and its disk) is gone
    code, lines, result = second.deploy("--resume", "--json")
    assert code == 0, result.stdout + result.stderr
    assert lines[-1]["state"] == "ready" and lines[-1]["run_id"] == run_id
    assert lines[-1]["stages"]["build"] == "done"  # kept from the first runner
    assert FakeBackend.calls == ["build", "deliver", "build", "deliver"]
    assert ("Deployment", "web") in api.objects


def test_a_second_runner_is_refused_while_the_lock_is_held(shared) -> None:
    from piceli.k8s.ops.provider_factory import KubeconfigTarget
    from piceli.state.cluster import open_store
    from piceli.testing import TARGET

    api, root = shared
    runner = Runner(root, "one")
    code, _, _ = runner.deploy("--plan")
    assert code == 0
    target = KubeconfigTarget(
        kubeconfig=root / "kubeconfig",
        context="fake",
        namespace=TARGET.namespace,
        transport="loopback-http",
    )
    other = open_store(target, name="shop", layout="pipeline", lease_seconds=60)
    other.holder = "runner-x/9/zz"
    other.acquire()
    try:
        code, lines, result = Runner(root, "two").deploy("--auto-approve")
        assert code == 2, result.stdout
        assert lines[-1]["reason"] == "pipeline-locked"
        assert lines[-1]["lock"]["holder"] == "runner-x/9/zz"
        assert 0 < lines[-1]["lock"]["expires_in"] <= 60
        assert "runner-x/9/zz" in result.stderr
        # The release commands of the pipeline are refused the same way.
        code, lines, _ = Runner(root, "three").invoke(
            "release", "plan", "--spec", str(root / "app.py:pipeline")
        )
        assert (code, lines[-1]["reason"]) == (2, "pipeline-locked")
    finally:
        other.close()
    code, lines, result = Runner(root, "two").deploy("--auto-approve")
    assert code == 0, result.stdout + result.stderr


def test_an_expired_lock_is_taken_over(shared) -> None:
    api, root = shared
    api.put(
        {
            "apiVersion": "coordination.k8s.io/v1",
            "kind": "Lease",
            "metadata": {
                "name": "piceli-lock-shop",
                "namespace": "piceli-test",
                "labels": {"piceli.io/state": "shop"},
            },
            "spec": {
                "holderIdentity": "dead-runner/1/aa",
                "leaseDurationSeconds": 5,
                "renewTime": "2020-01-01T00:00:00.000000Z",
                "leaseTransitions": 4,
            },
        }
    )
    code, lines, result = Runner(root, "new").deploy("--auto-approve")
    assert code == 0, result.stdout + result.stderr
    assert "took over the expired lock of 'shop' from dead-runner/1/aa" in result.stderr
    assert _lease(api)["leaseTransitions"] == 5


def test_local_state_moves_to_the_cluster_on_first_use(shared) -> None:
    api, root = shared
    local = Runner(root, "one", state="local")
    code, _, result = local.deploy("--auto-approve")
    assert code == 0, result.stdout + result.stderr
    assert ("Lease", "piceli-lock-shop") not in api.objects
    moved = Runner(root, "one")  # the same directory, now state="cluster"
    code, _, result = moved.deploy("--auto-approve")
    assert code == 0, result.stdout + result.stderr
    assert "moved the local state of 'shop'" in result.stderr
    code, lines, result = Runner(root, "other").deploy("--auto-approve", "--json")
    assert code == 0, result.stdout + result.stderr
    # The other runner knows the deployed release: nothing to apply.
    assert lines[-1]["stages"]["apply"] == "skipped"


def test_a_failed_write_back_is_kept_by_the_same_runner(shared) -> None:
    api, root = shared
    runner = Runner(root, "one")
    code, _, result = runner.deploy("--plan")
    assert code == 0, result.stdout + result.stderr
    # Every write of the state head fails for a while (API server trouble).
    for _ in range(50):
        api.inject("PATCH", "/secrets/piceli-state-shop", status=503)
    code, lines, result = runner.deploy("--auto-approve", "--json")
    assert code != 0 and lines[-1]["reason"] == "state-unavailable", result.stdout
    assert "the final state write failed (state-unavailable)" in result.stderr
    api.faults.clear()
    # The working copy is ahead of the shared state: the runner keeps it and
    # writes it back instead of replacing it with the older generation.
    code, lines, result = runner.deploy("--auto-approve", "--json")
    assert code == 0, result.stdout + result.stderr
    assert "unsynced state of 'shop'" in result.stderr
    assert lines[-1]["state"] == "ready"
