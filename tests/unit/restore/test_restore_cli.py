"""``piceli restore-points`` and ``piceli restore`` (cluster side faked)."""

from __future__ import annotations

import json
import shutil
import textwrap
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli import app as cli
from piceli.pipeline.backend import Backend
from piceli.restore import RestorePoints
from piceli.restore.plan import plan
from piceli.restore.runner import take
from tests.unit.restore.conftest import NEW, OLD, claims, stateful_app, workloads
from tests.unit.restore.fake_cluster import FakeCluster

pytestmark = pytest.mark.skipif(
    shutil.which("sha256sum") is None, reason="needs sha256sum"
)

PIPELINE = """
from piceli import App, ClaimTemplate, ExistingClaim, Pipeline, RestorePoints, Target

app = App("shop")
app.stateful_set("db", image="{image}", replicas=2,
                 volumes={{"/var/lib/db": ClaimTemplate("data", size="1Gi")}})
target = Target.kubeconfig("kubeconfig", context="fake", namespace="shop")
pipeline = Pipeline(app, target, state_dir="state", restore_points=RestorePoints())
"""


def invoke(*args: str) -> tuple[int, dict[str, Any], str]:
    result = CliRunner().invoke(cli, list(args))
    lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
    return result.exit_code, json.loads(lines[-1]) if lines else {}, result.stderr


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[FakeCluster, str]:
    (tmp_path / "app.py").write_text(textwrap.dedent(PIPELINE.format(image=NEW)))
    (tmp_path / "kubeconfig").write_text("")
    monkeypatch.chdir(tmp_path)
    live = workloads(stateful_app(OLD))
    cluster = FakeCluster(tmp_path, live)
    cluster.claim_names = ["data-db-0", "data-db-1"]
    for index in (0, 1):
        (cluster.claim_dir(f"data-db-{index}") / "marker").write_text(f"v1-{index}\n")
    record = take(
        cluster,  # type: ignore[arg-type]
        plan(workloads(stateful_app(NEW)), live, claims()),
        RestorePoints(),
        tmp_path / "state" / "restore-points",
        context={"namespace": "shop", "app": "shop"},
    )
    cluster.calls.clear()
    monkeypatch.setattr(
        Backend, "restore_cluster", lambda self, target, say=None: cluster
    )
    cluster.close = lambda: None  # type: ignore[attr-defined]
    return cluster, record["id"]


def test_restore_points_lists_and_verifies(
    setup: tuple[FakeCluster, str], tmp_path: Path
) -> None:
    _cluster, point = setup
    code, body, stderr = invoke("restore-points", "app.py:pipeline", "--verify")
    assert code == 0, stderr
    assert body["schema"] == "piceli.restore-points.v1"
    (listed,) = body["points"]
    assert listed["id"] == point and listed["state"] == "verified"
    assert {item["check"] for item in listed["claims"]} == {"verified"}
    archive = next((tmp_path / "state" / "restore-points" / point).glob("00-*.tar.gz"))
    archive.write_bytes(archive.read_bytes()[:-8])
    code, body, _ = invoke("restore-points", "app.py:pipeline", "--verify")
    assert code == 1
    assert body["points"][0]["claims"][0]["check"] == "restore-point-checksum-mismatch"


def test_restore_plans_then_needs_the_hash_and_brings_the_data_back(
    setup: tuple[FakeCluster, str],
) -> None:
    cluster, point = setup
    marker = cluster.claim_dir("data-db-0") / "marker"
    marker.write_text("v2 after a storage-format release\n")
    (cluster.claim_dir("data-db-0") / "new-file").write_text("written by v2\n")

    code, body, stderr = invoke("restore", "app.py:pipeline", "--point", point)
    assert code == 3, stderr
    assert body["state"] == "approval-required"
    planned = body["plan"]
    assert [item["claim"] for item in planned["claims"]] == ["data-db-0", "data-db-1"]
    assert planned["writers"] == ["StatefulSet/db"]
    assert cluster.calls == []  # planning changed nothing
    assert "v2" in marker.read_text()

    code, body, _ = invoke(
        "restore",
        "app.py:pipeline",
        "--point",
        point,
        "--approve",
        "sha256:" + "0" * 64,
    )
    assert (code, body["reason"]) == (2, "restore-plan-changed")
    assert cluster.calls == []

    code, body, stderr = invoke(
        "restore",
        "app.py:pipeline",
        "--point",
        point,
        "--approve",
        planned["restore_hash"],
    )
    assert code == 0, stderr
    assert body["state"] == "restored"
    assert marker.read_text() == "v1-0\n"
    assert not (cluster.claim_dir("data-db-0") / "new-file").exists()
    assert all(item["verified"] for item in body["receipt"]["claims"])
    assert cluster.calls[0] == "scale StatefulSet/db 0"
    assert cluster.calls[-1] == "scale StatefulSet/db 2"
    assert any("ro=False" in call for call in cluster.calls)
    # Claim content never reaches the output.
    assert "v1-0" not in stderr and "v1-0" not in json.dumps(body)


def test_restore_refuses_unknown_points_and_claims(
    setup: tuple[FakeCluster, str],
) -> None:
    _cluster, point = setup
    code, body, _ = invoke("restore", "app.py:pipeline", "--point", "rp-nope")
    assert (code, body["reason"]) == (2, "restore-point-unknown")
    code, body, _ = invoke(
        "restore", "app.py:pipeline", "--point", point, "--claim", "other"
    )
    assert (code, body["reason"]) == (2, "restore-point-claim-unknown")


def test_restore_refuses_a_claim_that_no_longer_exists(
    setup: tuple[FakeCluster, str],
) -> None:
    cluster, point = setup
    cluster.claim_names = ["data-db-0"]
    code, body, _ = invoke("restore", "app.py:pipeline", "--point", point)
    assert (code, body["reason"]) == (2, "restore-point-claim-missing")
