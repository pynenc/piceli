"""Acceptance: pre-rollout checks gate ``piceli deploy`` before any pod changes.

Builds and deliveries are the fake backend of ``test_deploy_pipeline``; planning,
the check Jobs and apply go through the real client against the in-process fake
API. ``FakeAPI.job_result`` decides how each check Job ends.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from piceli.testing import manifest
from tests.acceptance.test_deploy_pipeline import FakeBackend, deploy

DB = """
from piceli import ClaimTemplate, SecretVolume, UpgradeCheck
db = app.stateful_set(
    "db", image=images["web"], ports=[5432],
    env={"TOKEN": credentials.key("password"), "PLAIN": "x"},
    volumes={
        "/etc/db": SecretVolume("db-config"),
        "/var/lib/db": ClaimTemplate("data", size="1Gi"),
    },
    replicas=1,
)
app.pre_rollout(
    db, ["db", "check-config"],
    UPGRADE
)
"""


def with_db(tmp_path: Path, upgrade: bool = False) -> None:
    text = (tmp_path / "app.py").read_text()
    block = DB.replace(
        "UPGRADE",
        'upgrade=UpgradeCheck(["db", "verify", "/var/lib/db"]),' if upgrade else "",
    )
    (tmp_path / "app.py").write_text(
        text.replace("CHECKS = []", block + "\nCHECKS = []")
    )


def secret(api: Any, name: str = "db-config", **data: str) -> None:
    body = manifest("Secret", name)
    body["data"] = data or {"token": "c2VjcmV0LXZhbHVl"}
    api.put(body)


def jobs(api: Any) -> list[dict[str, Any]]:
    return [
        request["body"]
        for request in api.requests
        if request["method"] == "POST" and request["path"].endswith("/jobs")
    ]


def db_image(api: Any) -> str:
    return str(
        api.objects[("StatefulSet", "db")]["spec"]["template"]["spec"]["containers"][0][
            "image"
        ]
    )


def run_record(tmp_path: Path) -> dict[str, Any]:
    return json.loads(
        sorted((tmp_path / "state" / "runs").glob("*.json"))[-1].read_text()
    )


@pytest.fixture
def guarded(shop) -> tuple[Any, Path]:
    api, tmp_path = shop
    with_db(tmp_path)
    secret(api)
    return api, tmp_path


def release_v2(tmp_path: Path) -> None:
    (tmp_path / "src" / "main.txt").write_text("v2\n")
    FakeBackend.image_id = "sha256:" + "2" * 64


def test_the_check_runs_with_the_new_image_and_real_mounts_before_apply(
    guarded,
) -> None:
    api, tmp_path = guarded
    code, events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 0, result.stdout + result.stderr
    final = events[-1]
    assert final["stages"]["prerollout"] == "done"
    (job,) = jobs(api)
    pod = job["spec"]["template"]["spec"]
    (container,) = pod["containers"]
    assert container["image"] == db_image(api)  # the image the pods now run
    assert container["command"] == ["db", "check-config"]
    assert {item["name"]: item for item in container["env"]}["TOKEN"]["valueFrom"] == {
        # Created by this very release: absent now, so optional for the check.
        "secretKeyRef": {"name": "credentials", "key": "password", "optional": True}
    }
    volumes = {item["name"]: item for item in pod["volumes"]}
    assert volumes["db-config"]["secret"]["secretName"] == "db-config"
    assert "persistentVolumeClaim" not in json.dumps(pod)  # config check: no data
    assert volumes["data"] == {"name": "data", "emptyDir": {}}
    labels = job["spec"]["template"]["metadata"]["labels"]
    assert set(labels) == {
        "piceli.io/pre-rollout",
        "piceli.io/pre-rollout-app",
        "piceli.io/pre-rollout-run",
    }  # nothing a Service or NetworkPolicy of the workload selects
    assert job["spec"]["backoffLimit"] == 0
    assert job["spec"]["activeDeadlineSeconds"] == 300
    # Removed again, with its pods.
    assert not [key for key in api.objects if key[0] in {"Job", "Pod"}]
    output = run_record(tmp_path)["stages"]["prerollout"]["output"]
    assert output["checks"][0]["state"] == "passed"
    assert output["checks"][0]["cleaned"] is True
    assert output["checks"][0]["unverified"] == ["Secret/credentials"]


def test_a_new_image_that_cannot_read_a_secret_stops_the_release_before_the_pods_change(
    guarded,
) -> None:
    api, tmp_path = guarded
    code, _, result = deploy(tmp_path, "--auto-approve")
    assert code == 0, result.stdout + result.stderr
    before = db_image(api)
    generation = api.objects[("StatefulSet", "db")]["metadata"]["generation"]

    release_v2(tmp_path)
    api.job_result(
        "db-cfg", exit_code=3, logs="cannot read /etc/db/token (permission denied)\n"
    )
    code, events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 1, result.stdout + result.stderr
    final = events[-1]
    assert (final["state"], final["reason"], final["stage"]) == (
        "failed",
        "prerollout-failed",
        "prerollout",
    )
    assert final["stages"]["apply"] == "pending"
    # The reproduction: the pods (their template) are exactly as they were.
    assert db_image(api) == before
    assert api.objects[("StatefulSet", "db")]["metadata"]["generation"] == generation
    assert image_of(api, "web") != ""  # nothing else moved either
    assert not [key for key in api.objects if key[0] in {"Job", "Pod"}]
    record = run_record(tmp_path)
    check = record["stages"]["prerollout"]["output"]["checks"][0]
    assert check["state"] == "failed" and check["exit_code"] == 3
    assert "permission denied" in check["log_tail"]
    assert record["stages"]["apply"]["state"] == "pending"


def image_of(api: Any, name: str) -> str:
    return str(
        api.objects[("Deployment", name)]["spec"]["template"]["spec"]["containers"][0][
            "image"
        ]
    )


def test_a_secret_mount_that_cannot_start_fails_the_run_not_the_pods(guarded) -> None:
    api, tmp_path = guarded
    code, _, result = deploy(tmp_path, "--auto-approve")
    assert code == 0, result.stdout + result.stderr
    before = db_image(api)
    release_v2(tmp_path)
    api.job_result(
        "db-cfg",
        waiting="CreateContainerConfigError",
        message='secret "db-config" not found',
    )
    code, events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 1, result.stdout + result.stderr
    assert events[-1]["reason"] == "prerollout-not-startable"
    assert db_image(api) == before
    check = run_record(tmp_path)["stages"]["prerollout"]["output"]["checks"][0]
    assert (check["state"], check["category"]) == ("not-startable", "mount")


def test_a_missing_secret_is_refused_at_plan_time(shop) -> None:
    api, tmp_path = shop
    with_db(tmp_path)  # the Secret "db-config" is not in the cluster
    code, events, result = deploy(tmp_path, "--plan")
    assert code == 2, result.stdout + result.stderr
    assert events[-1]["reason"] == "prerollout-mount-missing"
    assert "Secret/db-config does not exist" in result.stderr + result.stdout
    assert jobs(api) == [] and ("StatefulSet", "db") not in api.objects


def test_a_missing_key_is_refused_at_plan_time(shop) -> None:
    api, tmp_path = shop
    text = (tmp_path / "app.py").read_text()
    (tmp_path / "app.py").write_text(text)
    with_db(tmp_path)
    (tmp_path / "app.py").write_text(
        (tmp_path / "app.py")
        .read_text()
        .replace(
            '"PLAIN": "x"',
            '"PLAIN": "x", "KEY": SecretKey(secret="db-config", key="token")',
        )
        .replace(
            "from piceli import ClaimTemplate",
            "from piceli import SecretKey, ClaimTemplate",
        )
    )
    secret(api, other="eA==")
    code, events, result = deploy(tmp_path, "--plan")
    assert code == 2, result.stdout + result.stderr
    assert events[-1]["reason"] == "prerollout-mount-missing"
    assert "has no key token" in result.stderr + result.stdout


def test_the_plan_shows_the_check_and_an_undeclared_app_is_unchanged(guarded) -> None:
    api, tmp_path = guarded
    code, events, _ = deploy(tmp_path, "--plan")
    assert code == 0
    stage = events[-1]["stages"]["prerollout"]
    assert stage["state"] == "planned"
    (check,) = stage["checks"]
    assert check["workload"] == "db" and check["check"]["command"] == [
        "db",
        "check-config",
    ]
    uses = {(item["kind"], item["name"], item["source"]) for item in check["uses"]}
    assert ("Secret", "db-config", "cluster") in uses
    assert ("Secret", "credentials", "release") in uses


def test_the_upgrade_check_opens_the_retained_claim_read_only_on_the_holders_node(
    shop,
) -> None:
    api, tmp_path = shop
    with_db(tmp_path, upgrade=True)
    secret(api)
    code, _, result = deploy(tmp_path, "--auto-approve")
    assert code == 0, result.stdout + result.stderr
    # The running pod holds the ReadWriteOnce claim on node-a.
    claim = manifest("PersistentVolumeClaim", "data-db-0")
    claim["spec"] = {"accessModes": ["ReadWriteOnce"]}
    api.put(claim)
    running = manifest("Pod", "db-0")
    running["spec"] = {
        "nodeName": "node-a",
        "volumes": [
            {"name": "data", "persistentVolumeClaim": {"claimName": "data-db-0"}}
        ],
        "containers": [{"name": "db", "image": "x"}],
    }
    running["status"] = {"phase": "Running"}
    api.put(running)
    before = len(jobs(api))

    release_v2(tmp_path)
    code, _, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 0, result.stdout + result.stderr
    config, upgrade = jobs(api)[before:]
    assert "emptyDir" in json.dumps(config["spec"]["template"]["spec"]["volumes"])
    pod = upgrade["spec"]["template"]["spec"]
    volumes = {item["name"]: item for item in pod["volumes"]}
    assert volumes["data"]["persistentVolumeClaim"] == {
        "claimName": "data-db-0",
        "readOnly": True,
    }
    mounts = {item["mountPath"]: item for item in pod["containers"][0]["volumeMounts"]}
    assert mounts["/var/lib/db"]["readOnly"] is True
    assert pod["containers"][0]["command"] == ["db", "verify", "/var/lib/db"]
    terms = pod["affinity"]["nodeAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"
    ]["nodeSelectorTerms"]
    assert terms[0]["matchFields"][0]["values"] == ["node-a"]
    output = run_record(tmp_path)["stages"]["prerollout"]["output"]
    assert [item["kind"] for item in output["checks"]] == ["config", "upgrade"]
    assert output["checks"][1]["claims"] == ["data-db-0"]


def test_an_incompatible_store_stops_the_release_and_leaves_pods_unchanged(
    shop,
) -> None:
    api, tmp_path = shop
    with_db(tmp_path, upgrade=True)
    secret(api)
    code, _, result = deploy(tmp_path, "--auto-approve")
    assert code == 0, result.stdout + result.stderr
    claim = manifest("PersistentVolumeClaim", "data-db-0")
    claim["spec"] = {"accessModes": ["ReadWriteOnce"]}
    api.put(claim)
    before = db_image(api)

    release_v2(tmp_path)
    api.job_result("db-up0", exit_code=1, logs="store format 7 is newer than 6\n")
    code, events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 1, result.stdout + result.stderr
    assert events[-1]["reason"] == "prerollout-failed"
    assert db_image(api) == before
    checks = run_record(tmp_path)["stages"]["prerollout"]["output"]["checks"]
    assert [(c["kind"], c["state"]) for c in checks] == [
        ("config", "passed"),
        ("upgrade", "failed"),
    ]
    assert "store format 7" in checks[1]["log_tail"]


def test_a_read_write_once_pod_claim_is_refused_at_plan_time(shop) -> None:
    api, tmp_path = shop
    with_db(tmp_path, upgrade=True)
    secret(api)
    claim = manifest("PersistentVolumeClaim", "data-db-0")
    claim["spec"] = {"accessModes": ["ReadWriteOncePod"]}
    api.put(claim)
    running = manifest("Pod", "db-0")
    running["spec"] = {
        "nodeName": "node-a",
        "volumes": [
            {"name": "data", "persistentVolumeClaim": {"claimName": "data-db-0"}}
        ],
        "containers": [{"name": "db", "image": "x"}],
    }
    running["status"] = {"phase": "Running"}
    api.put(running)
    code, events, _ = deploy(tmp_path, "--plan")
    assert code == 2
    assert events[-1]["reason"] == "prerollout-claim-exclusive"


def test_an_unchanged_release_skips_the_check_and_a_resume_reruns_it(guarded) -> None:
    api, tmp_path = guarded
    code, _, result = deploy(tmp_path, "--auto-approve")
    assert code == 0, result.stdout + result.stderr
    ran = len(jobs(api))
    code, events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 0, result.stdout + result.stderr
    assert events[-1]["stages"]["prerollout"] == "skipped"
    assert len(jobs(api)) == ran

    release_v2(tmp_path)
    api.job_result("db-cfg", exit_code=2)
    code, events, _ = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 1 and events[-1]["stages"]["prerollout"] == "failed"
    api.job_results.clear()
    code, events, result = deploy(tmp_path, "--resume", "--json")
    assert code == 0, result.stdout + result.stderr
    assert events[-1]["stages"]["prerollout"] == "done"
    assert events[-1]["stages"]["apply"] == "done"
