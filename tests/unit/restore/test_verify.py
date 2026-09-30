"""``piceli restore --to-new-claim``: restore into scratch claims and verify.

The cluster is the in-memory :class:`FakeCluster` (claims are directories,
helper commands run in a local shell) and verify Jobs run their command
locally against the scratch claim's directory.
"""

from __future__ import annotations

import json
import shutil
import textwrap
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli import App, ClaimTemplate, RestoreVerify
from piceli.k8s.cli import app as cli
from piceli.pipeline.backend import Backend
from piceli.restore import RestorePointError, RestorePoints
from piceli.restore.cluster import SCRATCH_LABEL, VERIFY_LABEL
from piceli.restore.plan import plan
from piceli.restore.runner import take
from piceli.restore.verify import plan_verify, verify
from tests.unit.restore.conftest import NEW, OLD, claims, stateful_app, workloads
from tests.unit.restore.fake_cluster import FakeCluster, FakeJobs

pytestmark = pytest.mark.skipif(
    shutil.which("sha256sum") is None, reason="needs sha256sum"
)

GOOD = ["sh", "-c", "grep -q '^v1-' /var/lib/db/marker"]
DECLARED = {"StatefulSet/db": RestoreVerify(GOOD).describe()}
AFFINITY = {
    "nodeSelectorTerms": [
        {
            "matchExpressions": [
                {"key": "kubernetes.io/hostname", "operator": "In", "values": ["n1"]}
            ]
        }
    ]
}


@pytest.fixture
def taken(tmp_path: Path) -> tuple[FakeCluster, str, Path]:
    live = workloads(stateful_app(OLD))
    cluster = FakeCluster(tmp_path, live)
    cluster.claim_names = ["data-db-0", "data-db-1"]
    for index in (0, 1):
        (cluster.claim_dir(f"data-db-{index}") / "marker").write_text(f"v1-{index}\n")
    root = tmp_path / "state" / "restore-points"
    record = take(
        cluster,  # type: ignore[arg-type]
        plan(workloads(stateful_app(NEW)), live, claims()),
        RestorePoints(),
        root,
        context={"namespace": "shop", "app": "shop"},
    )
    cluster.calls.clear()
    cluster.jobs.clear()
    return cluster, record["id"], root


def _plan(cluster: FakeCluster, point: str, root: Path, **kwargs: Any) -> Any:
    return plan_verify(
        cluster,  # type: ignore[arg-type]
        root,
        point,
        declared=kwargs.pop("declared", DECLARED),
        target={"namespace": "shop"},
        **kwargs,
    )


def _run(cluster: FakeCluster, jobs: FakeJobs, root: Path, planned: Any) -> Any:
    return verify(
        cluster,  # type: ignore[arg-type]
        jobs,
        root,
        planned,
        app="shop",
        timeout_seconds=30,
    )


def _live_state(cluster: FakeCluster) -> dict[str, Any]:
    return {
        "claims": {
            name: (cluster.claim_dir(name) / "marker").read_text()
            for name in ("data-db-0", "data-db-1")
        },
        "workloads": json.dumps(cluster.live, sort_keys=True),
    }


def test_the_plan_reads_only_and_names_scratch_claims_and_checks(taken) -> None:
    cluster, point, root = taken
    cluster.affinity = AFFINITY
    planned = _plan(cluster, point, root)
    assert all(call.startswith("read pv-") for call in cluster.calls)
    assert cluster.scratch == {}
    first, second = planned["claims"]
    assert first["claim"] == "data-db-0" and second["claim"] == "data-db-1"
    assert first["scratch_claim"] == f"piceli-verify-{point[-6:]}-00"
    assert (first["storage_class"], first["size"], first["access_modes"]) == (
        "standard",
        "1Gi",
        ["ReadWriteOnce"],
    )
    assert first["node_affinity"] == AFFINITY
    assert first["path"] == "/var/lib/db"
    assert [check["ordinal"] for check in planned["checks"]] == [0, 1]
    assert planned["checks"][0]["command"] == GOOD
    assert planned["checks"][0]["mounts"] == {"/var/lib/db": first["scratch_claim"]}
    assert planned["verify_hash"].startswith("sha256:")
    assert _plan(cluster, point, root)["verify_hash"] == planned["verify_hash"]
    assert (
        _plan(cluster, point, root, keep=True)["verify_hash"]
        != (planned["verify_hash"])
    )


def test_a_good_copy_passes_and_live_data_and_writers_stay_untouched(taken) -> None:
    cluster, point, root = taken
    cluster.affinity = AFFINITY
    (cluster.claim_dir("data-db-0") / "marker").write_text("v2 written later\n")
    before = _live_state(cluster)
    jobs = FakeJobs(cluster)
    planned = _plan(cluster, point, root)
    receipt = _run(cluster, jobs, root, planned)

    assert receipt["state"] == "passed"
    assert [item["result"] for item in receipt["claims"]] == ["PASS", "PASS"]
    assert {item["verify"] for item in receipt["claims"]} == {"passed"}
    assert _live_state(cluster) == before  # live claims and workloads unchanged
    assert not any(call.startswith("scale") for call in cluster.calls)
    assert not any(call.startswith("hook") for call in cluster.calls)
    # Scratch claims: created, labelled Piceli-owned scratch, deleted after.
    created = [c for c in cluster.calls if c.startswith("create claim")]
    deleted = [c for c in cluster.calls if c.startswith("delete claim")]
    assert len(created) == len(deleted) == 2
    assert cluster.scratch == {} and receipt["kept"] == [] and receipt["left"] == []
    # Helpers mount only scratch claims, read-write, pinned to the volume's node.
    for name, manifest in cluster.jobs.items():
        spec = manifest["spec"]["template"]["spec"]
        (volume,) = spec["volumes"]
        assert volume["persistentVolumeClaim"]["claimName"].startswith("piceli-verify-")
        assert manifest["metadata"]["labels"][VERIFY_LABEL] == point
        assert spec["affinity"]["nodeAffinity"][
            "requiredDuringSchedulingIgnoredDuringExecution"
        ] == (AFFINITY)
        assert name.startswith("piceli-rv-")
    # Verify Jobs: the workload's current image, the scratch copy read-only.
    for job in jobs.jobs:
        (container,) = job["spec"]["template"]["spec"]["containers"]
        assert container["image"] == OLD
        assert container["command"] == GOOD
        claims_used = [
            v["persistentVolumeClaim"]
            for v in job["spec"]["template"]["spec"]["volumes"]
            if "persistentVolumeClaim" in v
        ]
        assert [c["readOnly"] for c in claims_used] == [True]
        assert claims_used[0]["claimName"].startswith("piceli-verify-")
        assert job["metadata"]["labels"][VERIFY_LABEL] == point
    written = json.loads(Path(receipt["receipt"]).read_text())
    assert written["state"] == "passed"
    assert "v1-0" not in Path(receipt["receipt"]).read_text()


def test_the_scratch_claim_is_shaped_like_its_source(taken) -> None:
    cluster, point, root = taken
    planned = _plan(cluster, point, root, keep=True)
    _run(cluster, FakeJobs(cluster), root, planned)
    manifest = cluster.scratch[f"piceli-verify-{point[-6:]}-00"]
    assert manifest["metadata"]["labels"] == {
        "app.kubernetes.io/managed-by": "piceli",
        SCRATCH_LABEL: "true",
        VERIFY_LABEL: point,
    }
    assert manifest["metadata"]["annotations"] == {
        "piceli.io/source-claim": "data-db-0"
    }
    assert manifest["spec"] == {
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": "1Gi"}},
        "storageClassName": "standard",
    }


def test_a_failing_verify_reports_fail_and_still_cleans_up(taken) -> None:
    cluster, point, root = taken
    bad = {"StatefulSet/db": RestoreVerify(["sh", "-c", "exit 3"]).describe()}
    jobs = FakeJobs(cluster)
    receipt = _run(cluster, jobs, root, _plan(cluster, point, root, declared=bad))
    assert receipt["state"] == "failed"
    assert {item["result"] for item in receipt["claims"]} == {"FAIL"}
    assert {item["reason"] for item in receipt["claims"]} == {"restore-verify-failed"}
    assert receipt["checks"][0]["exit_code"] == 3
    assert "log_tail" not in json.dumps(receipt)
    assert cluster.scratch == {}


def test_a_copy_the_app_rejects_fails_only_its_claims(taken) -> None:
    cluster, point, root = taken
    # The archive restores what was taken; an app check that expects other
    # content (as it would for corrupted data) fails and names the claim.
    strict = {
        "StatefulSet/db": RestoreVerify(
            ["sh", "-c", "grep -q '^v1-0' /var/lib/db/marker"]
        ).describe()
    }
    receipt = _run(
        cluster, FakeJobs(cluster), root, _plan(cluster, point, root, declared=strict)
    )
    assert [item["result"] for item in receipt["claims"]] == ["PASS", "FAIL"]
    assert receipt["state"] == "failed"


def test_a_failed_extraction_is_fail_skips_the_check_and_cleans_up(taken) -> None:
    cluster, point, root = taken
    cluster.fail["tar -xzf"] = 2
    jobs = FakeJobs(cluster)
    receipt = _run(cluster, jobs, root, _plan(cluster, point, root))
    assert {item["reason"] for item in receipt["claims"]} == {
        "restore-verify-scratch-failed"
    }
    assert {item["verify"] for item in receipt["claims"]} == {"skipped"}
    assert jobs.jobs == []
    assert cluster.scratch == {}


def test_keep_leaves_the_scratch_claims(taken) -> None:
    cluster, point, root = taken
    receipt = _run(
        cluster, FakeJobs(cluster), root, _plan(cluster, point, root, keep=True)
    )
    assert receipt["state"] == "passed"
    assert sorted(receipt["kept"]) == sorted(cluster.scratch)
    assert not any(call.startswith("delete claim") for call in cluster.calls)
    with pytest.raises(RestorePointError) as error:
        _plan(cluster, point, root)
    assert error.value.code == "restore-verify-scratch-exists"


def test_an_interrupt_still_deletes_the_scratch_claims(taken) -> None:
    cluster, point, root = taken
    jobs = FakeJobs(cluster)
    jobs.raise_on_run = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        _run(cluster, jobs, root, _plan(cluster, point, root))
    assert cluster.scratch == {}


def test_a_scratch_claim_that_stays_is_an_error_naming_it(taken) -> None:
    cluster, point, root = taken
    planned = _plan(cluster, point, root)
    cluster.undeletable = {planned["claims"][0]["scratch_claim"]}
    with pytest.raises(RestorePointError) as error:
        _run(cluster, FakeJobs(cluster), root, planned)
    assert error.value.code == "restore-verify-cleanup-failed"
    assert error.value.details["receipt"]["left"] == [
        planned["claims"][0]["scratch_claim"]
    ]


def test_without_a_declared_check_the_digest_decides(taken) -> None:
    cluster, point, root = taken
    jobs = FakeJobs(cluster)
    receipt = _run(cluster, jobs, root, _plan(cluster, point, root, declared={}))
    assert receipt["state"] == "passed"
    assert {item["verify"] for item in receipt["claims"]} == {"not-declared"}
    assert jobs.jobs == []


def test_plan_refusals(taken) -> None:
    cluster, point, root = taken
    cluster.storage_class = ""
    with pytest.raises(RestorePointError) as error:
        _plan(cluster, point, root)
    assert error.value.code == "restore-verify-scratch-unsupported"
    cluster.storage_class = "standard"
    del cluster.live["StatefulSet/db"]
    with pytest.raises(RestorePointError) as error:
        _plan(cluster, point, root)
    assert error.value.code == "restore-verify-workload-missing"
    cluster.claim_names = ["data-db-1"]
    with pytest.raises(RestorePointError) as error:
        _plan(cluster, point, root, declared={})
    assert error.value.code == "restore-point-claim-missing"


def test_app_restore_verify_declaration() -> None:
    app = App("shop")
    db = app.stateful_set(
        "db",
        image=NEW,
        volumes={"/var/lib/db": ClaimTemplate("data", size="1Gi")},
    )
    web = app.deployment("web", image=NEW)
    app.restore_verify(db, RestoreVerify(["db", "verify", "/var/lib/db"]))
    assert app.restore_verifies() == {
        "StatefulSet/db": {
            "command": ["db", "verify", "/var/lib/db"],
            "timeout_seconds": 300,
        }
    }
    with pytest.raises(ValueError, match="already"):
        app.restore_verify(db, RestoreVerify(["x"]))
    with pytest.raises(ValueError, match="no retained claim"):
        app.restore_verify(web, RestoreVerify(["x"]))
    with pytest.raises(ValueError, match="argv"):
        RestoreVerify("db verify")  # type: ignore[arg-type]
    # A declaration renders nothing: the release is unchanged.
    plain = App("shop")
    plain.stateful_set(
        "db",
        image=NEW,
        volumes={"/var/lib/db": ClaimTemplate("data", size="1Gi")},
    )
    plain.deployment("web", image=NEW)
    assert workloads(app) == workloads(plain)


# ------------------------------------------------------------------ the CLI

PIPELINE = """
from piceli import App, ClaimTemplate, Pipeline, RestorePoints, RestoreVerify, Target

app = App("shop")
db = app.stateful_set("db", image="{image}", replicas=2,
                      volumes={{"/var/lib/db": ClaimTemplate("data", size="1Gi")}})
app.restore_verify(db, RestoreVerify({command!r}))
target = Target.kubeconfig("kubeconfig", context="fake", namespace="shop")
pipeline = Pipeline(app, target, state_dir="state", restore_points=RestorePoints())
"""


def invoke(*args: str) -> tuple[int, dict[str, Any], str]:
    result = CliRunner().invoke(cli, list(args))
    lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
    return result.exit_code, json.loads(lines[-1]) if lines else {}, result.stderr


@pytest.fixture
def cli_setup(
    taken, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[FakeCluster, str, FakeJobs]:
    cluster, point, _root = taken
    (tmp_path / "kubeconfig").write_text("")
    monkeypatch.chdir(tmp_path)
    jobs = FakeJobs(cluster)
    jobs.close = lambda: None  # type: ignore[attr-defined]
    cluster.close = lambda: None  # type: ignore[attr-defined]
    monkeypatch.setattr(
        Backend, "restore_cluster", lambda self, target, say=None: cluster
    )
    monkeypatch.setattr(
        Backend, "prerollout_cluster", lambda self, target, poll_seconds=2.0: jobs
    )
    return cluster, point, jobs


def _write(tmp_path: Path, command: list[str]) -> None:
    (tmp_path / "app.py").write_text(
        textwrap.dedent(PIPELINE.format(image=NEW, command=command))
    )


def test_cli_plans_then_needs_the_hash_then_passes(cli_setup, tmp_path) -> None:
    cluster, point, _jobs = cli_setup
    _write(tmp_path, GOOD)
    args = ("restore", "app.py:pipeline", "--point", point, "--to-new-claim")
    code, body, stderr = invoke(*args)
    assert code == 3, stderr
    assert body["state"] == "approval-required"
    assert cluster.scratch == {} and not any("create" in c for c in cluster.calls)
    code, body, _ = invoke(*args, "--approve", "sha256:" + "0" * 64)
    assert (code, body["reason"]) == (2, "restore-verify-plan-changed")
    code, body, stderr = invoke(*args, "--approve", body["verify_hash"])
    assert code == 0, stderr
    assert body["state"] == "passed"
    (receipt,) = body["receipts"]
    assert [item["result"] for item in receipt["claims"]] == ["PASS", "PASS"]
    assert "v1-0" not in stderr and "v1-0" not in json.dumps(body)
    assert cluster.scratch == {}


def test_cli_a_failing_check_exits_1_with_fail(cli_setup, tmp_path) -> None:
    cluster, _point, _jobs = cli_setup
    _write(tmp_path, ["sh", "-c", "exit 1"])
    args = ("restore", "app.py:pipeline", "--all", "--to-new-claim")
    code, body, _ = invoke(*args)
    assert code == 3
    code, body, stderr = invoke(*args, "--approve", body["verify_hash"])
    assert code == 1, stderr
    assert (body["state"], body["reason"]) == ("failed", "restore-verify-failed")
    assert "FAIL" in stderr
    assert cluster.scratch == {}


def test_cli_option_combinations(cli_setup, tmp_path) -> None:
    _cluster, point, _jobs = cli_setup
    _write(tmp_path, GOOD)
    for args in (
        ("restore", "app.py:pipeline"),
        ("restore", "app.py:pipeline", "--all"),
        ("restore", "app.py:pipeline", "--point", point, "--keep"),
        ("restore", "app.py:pipeline", "--point", point, "--all", "--to-new-claim"),
    ):
        code, body, _ = invoke(*args)
        assert (code, body["reason"]) == (2, "restore-options-invalid"), args


def test_a_copy_whose_digest_differs_is_fail_and_skips_the_check(taken) -> None:
    cluster, point, root = taken
    planned = _plan(cluster, point, root)
    planned["claims"][0]["content_sha256"] = "0" * 64  # what the record promised
    jobs = FakeJobs(cluster)
    receipt = _run(cluster, jobs, root, planned)
    first, second = receipt["claims"]
    assert (first["result"], first["reason"]) == ("FAIL", "restore-verify-mismatch")
    assert first["verify"] == "skipped" and second["result"] == "PASS"
    assert len(jobs.jobs) == 1  # only replica 1's check ran
    assert cluster.scratch == {}
