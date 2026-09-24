"""Acceptance: ``piceli release`` drives ReleaseWorkflow against the fake API.

The kubeconfig points at the loopback fake API (``transport = "loopback-http"``);
no ambient kubeconfig is ever read.
"""

from __future__ import annotations

import base64
import json
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli.release import app
from tests.acceptance.fake_api import TARGET, serve

DIGEST_1 = "sha256:" + "1" * 64
DIGEST_2 = "sha256:" + "2" * 64

COMPOSITION = """
from piceli.k8s.ops.plan import DeploymentComponent, DeploymentComposition, ResourceIntent


def build(ctx):
    meta = lambda name: {"name": name, "namespace": ctx.namespace}
    config = ResourceIntent.from_manifest(
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta("settings"),
         "data": {"mode": str(ctx.values["mode"])}}
    )
    token = ResourceIntent.from_manifest(
        {"apiVersion": "v1", "kind": "Secret", "metadata": meta("credential"),
         "data": {"password": "<private>"}}
    ).with_secret("/data/password", ctx.secret("password"))
    worker = ResourceIntent.from_manifest(
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta("worker"),
         "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "worker"}},
                  "template": {"metadata": {"labels": {"app": "worker"}},
                               "spec": {"containers": [
                                   {"name": "worker", "image": ctx.image("api")}]}}}}
    )
    return DeploymentComposition((
        DeploymentComponent("config", (config, token)),
        DeploymentComponent("worker", (worker,), dependencies=("config",)),
    ))
"""


def _receipt(path: Path, digest: str) -> None:
    path.write_text(
        json.dumps(
            {
                "revision": "piceli.build-receipt.v1",
                "status": "passed",
                "outputs": {
                    "images": {
                        "api": {
                            "image_id": "sha256:" + "a" * 64,
                            "digest": digest,
                            "platform": "linux/amd64",
                            "ref": "registry.example/app/api:build-7",
                        }
                    }
                },
            }
        )
    )


@pytest.fixture
def release_env(tmp_path):
    with serve() as (api, url):
        (tmp_path / "kubeconfig").write_text(
            textwrap.dedent(
                f"""
                apiVersion: v1
                kind: Config
                current-context: must-not-be-used
                clusters:
                - name: fake
                  cluster: {{server: "{url}"}}
                users:
                - name: nobody
                  user: {{}}
                contexts:
                - name: fake
                  context: {{cluster: fake, user: nobody}}
                - name: must-not-be-used
                  context: {{cluster: fake, user: nobody}}
                """
            )
        )
        (tmp_path / "compose.py").write_text(COMPOSITION)
        _receipt(tmp_path / "build.receipt.json", DIGEST_1)
        (tmp_path / "release.toml").write_text(
            textwrap.dedent(
                f"""
                images_from = "build.receipt.json"

                [target]
                kubeconfig = "kubeconfig"
                context = "fake"
                namespace = "{TARGET.namespace}"
                cluster_uid = "cluster-uid"
                transport = "loopback-http"

                [release]
                name = "app"
                owner = "acceptance-owner"
                field_manager = "piceli-acceptance"
                composition = "compose.py:build"
                state_dir = "state"

                [execution]
                max_seconds = 30
                readiness_seconds = 1
                poll_seconds = 0.05

                [secrets.password]
                type = "random"
                bytes = 24

                [values]
                mode = "blue"
                """
            )
        )
        yield api, tmp_path


def _run(tmp_path: Path, *args: str):
    result = CliRunner().invoke(app, [*args, "--spec", str(tmp_path / "release.toml")])
    payload = json.loads(result.stdout) if result.stdout.strip() else {}
    return result.exit_code, payload, result


def _image(api) -> str:
    return api.objects[("Deployment", "worker")]["spec"]["template"]["spec"][
        "containers"
    ][0]["image"]


def test_plan_apply_release_per_digest_and_rollback(release_env):
    """plan -> apply -> new digest -> apply -> rollback: one release per digest."""
    api, tmp_path = release_env

    code, planned, _ = _run(tmp_path, "plan")
    assert code == 0, planned
    assert planned["state"] == "planned"
    assert planned["mode"] == "create"
    assert planned["source"]["identity"] == DIGEST_1
    assert planned["summary"] == {"create": 3}
    assert planned["secrets"] == {"password": "generated"}
    first = planned["release"]
    assert first.startswith("app-")
    assert ("Deployment", "worker") not in api.objects  # plan never writes

    code, applied, _ = _run(tmp_path, "apply", "--approve", planned["plan_hash"])
    assert code == 0, applied
    assert applied["execution"]["state"] == "ready"
    assert applied["selected"] == first
    assert _image(api) == f"registry.example/app/api@{DIGEST_1}"
    password = api.objects[("Secret", "credential")]["data"]["password"]
    assert base64.b64decode(password)
    # The approval is one-shot.
    code, refused, _ = _run(tmp_path, "apply", "--approve", planned["plan_hash"])
    assert code == 2 and refused["state"] == "refused"

    _receipt(tmp_path / "build.receipt.json", DIGEST_2)
    code, second_apply, result = _run(tmp_path, "apply", "--auto-approve")
    assert code == 0, result.output
    second = second_apply["release"]
    assert second != first
    assert second_apply["mode"] == "create"
    assert second_apply["source"]["identity"] == DIGEST_2
    assert _image(api) == f"registry.example/app/api@{DIGEST_2}"
    # Generated secrets are carried over, not regenerated per release.
    assert api.objects[("Secret", "credential")]["data"]["password"] == password
    assert "carried:" + first in result.stderr

    code, pending, _ = _run(tmp_path, "rollback", "previous")
    assert code == 3
    assert pending["state"] == "approval-required"
    assert pending["release"] == first
    assert pending["intent"] == "rollback"
    assert pending["mode"] == "reapply"
    operations = {item["name"]: item["operation"] for item in pending["actions"]}
    # Private Secret content is never compared, so the Secret is re-applied.
    assert operations == {"settings": "no-op", "credential": "apply", "worker": "apply"}

    code, rolled, _ = _run(
        tmp_path, "rollback", "previous", "--approve", pending["plan_hash"]
    )
    assert code == 0, rolled
    assert rolled["intent"] == "rollback"
    assert rolled["execution"]["state"] == "ready"
    assert rolled["selected"] == first
    assert _image(api) == f"registry.example/app/api@{DIGEST_1}"

    code, status, _ = _run(tmp_path, "status")
    assert code == 0
    assert status["deployed"] == first
    assert status["previous"] == second
    identities = {
        item["name"]: item["source"]["identity"] for item in status["releases"]
    }
    assert identities == {first: DIGEST_1, second: DIGEST_2}
    assert [entry["intent"] for entry in status["history"]] == [
        "apply",
        "apply",
        "rollback",
    ]
    assert all(entry["state"] == "ready" for entry in status["history"])
    assert "password" not in json.dumps(status).replace('"password"', "")
    serialized = (tmp_path / "state" / "catalog.json").read_text()
    assert base64.b64decode(password).decode() not in serialized


def test_unchanged_spec_replans_existing_release_as_noop(release_env):
    api, tmp_path = release_env
    code, applied, _ = _run(tmp_path, "apply", "--auto-approve")
    assert code == 0, applied
    code, planned, _ = _run(tmp_path, "plan")
    assert code == 0
    assert planned["release"] == applied["release"]
    assert planned["mode"] == "reapply"
    operations = {item["name"]: item["operation"] for item in planned["actions"]}
    assert operations == {"settings": "no-op", "credential": "apply", "worker": "no-op"}


def test_approval_required_without_tty_and_bad_hash(release_env):
    api, tmp_path = release_env
    code, pending, _ = _run(tmp_path, "apply")
    assert code == 3
    assert pending["state"] == "approval-required"
    assert ("Deployment", "worker") not in api.objects
    code, refused, _ = _run(tmp_path, "apply", "--approve", "f" * 64)
    assert code == 2 and "no pending plan" in refused["reason"]
    code, refused, _ = _run(tmp_path, "apply", "--approve", "not-a-hash")
    assert code == 2


def test_resume_and_stop_use_the_session_execution(release_env):
    api, tmp_path = release_env
    api.ready = False
    code, applied, _ = _run(tmp_path, "apply", "--auto-approve")
    assert code == 1
    assert applied["execution"]["state"] == "failed"
    assert "failure_category" in applied["execution"]
    execution_id = applied["execution"]["execution_id"]

    api.ready = True
    code, resumed, _ = _run(tmp_path, "resume")
    assert code == 0, resumed
    assert resumed["execution"]["execution_id"] == execution_id
    assert resumed["execution"]["state"] == "ready"
    code, refused, _ = _run(tmp_path, "stop")
    assert code == 2 and "nothing to stop" in refused["reason"]

    _receipt(tmp_path / "build.receipt.json", DIGEST_2)
    api.ready = False
    code, applied, _ = _run(tmp_path, "apply", "--auto-approve")
    assert code == 1
    code, stopped, _ = _run(tmp_path, "stop")
    assert code == 0, stopped
    assert stopped["execution"]["state"] == "cancelled"
    assert stopped["execution"]["execution_id"] == applied["execution"]["execution_id"]
    code, refused, _ = _run(tmp_path, "resume")
    assert code == 2  # a stopped execution is not resumed; plan/apply again
    code, status, _ = _run(tmp_path, "status")
    assert status["deployed"] == resumed["release"]
    # After an unfinished change, "previous" is the last known good release.
    assert status["previous"] == resumed["release"]
    assert status["latest"]["state"] == "cancelled"
    assert [entry["state"] for entry in status["history"]] == [
        "failed",
        "ready",
        "cancelled",
    ]


def test_cluster_identity_pin_is_enforced(release_env):
    api, tmp_path = release_env
    spec = tmp_path / "release.toml"
    spec.write_text(
        spec.read_text().replace('cluster_uid = "cluster-uid"', 'cluster_uid = "other"')
    )
    code, refused, _ = _run(tmp_path, "plan")
    assert code == 2
    assert "cluster identity mismatch" in refused["reason"]
    assert not (tmp_path / "state").exists()


def test_unknown_spec_keys_are_rejected(release_env):
    api, tmp_path = release_env
    spec = tmp_path / "release.toml"
    spec.write_text(spec.read_text().replace("[values]", "surprise = 1\n[values]"))
    code, refused, _ = _run(tmp_path, "plan")
    assert code == 2
    assert "surprise" in refused["reason"]
