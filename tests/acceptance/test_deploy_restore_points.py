"""Acceptance: the ``backup`` stage of ``piceli deploy`` (restore points).

The release engine runs against the in-process fake API; the restore point's
reads (workloads, claims) go to the same fake API through the real client,
while stopping writers, the helper Job and its byte streams are faked with
local directories (``tests/unit/restore/fake_cluster.py``); kind covers them.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import textwrap
from collections.abc import Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli import app as cli
from piceli.pipeline import PipelineError
from piceli.pipeline.backend import Backend
from piceli.restore.cluster import RestoreCluster
from piceli.restore.store import load_record
from tests.acceptance.fake_api import TARGET, serve
from tests.unit.restore.fake_cluster import FakeCluster

pytestmark = pytest.mark.skipif(
    shutil.which("sha256sum") is None, reason="needs sha256sum"
)

OLD = "example/db@sha256:" + "1" * 64
NEW = "example/db@sha256:" + "2" * 64

PIPELINE = f"""
from piceli import App, ClaimTemplate, Pipeline, Quiesce, RestorePoints, Target

target = Target.kubeconfig(
    "kubeconfig", context="fake", namespace="{TARGET.namespace}",
    transport="loopback-http",
)
app = App("shop")
db = app.stateful_set("db", image="IMAGE", replicas=2,
                      volumes={{"/var/lib/db": ClaimTemplate("data", size="1Gi")}})
app.quiesce(db, Quiesce.exec(["sync"]))
app.deployment("web", image="{OLD}")
pipeline = Pipeline(
    app, target, state_dir="state", restore_points=RestorePoints(),
    execution={{"max_seconds": 30, "readiness_seconds": 1, "poll_seconds": 0.05}},
)
"""


class HybridCluster(RestoreCluster):
    """Reads from the fake API; writes and streams through a FakeCluster."""

    fake: FakeCluster

    def scale(self, kind: str, name: str, replicas: int) -> None:
        self.fake.calls.append(f"scale {kind}/{name} {replicas}")

    def run_hook(self, writer: Any, hook: Any) -> None:
        self.fake.run_hook(writer, hook)

    def wait_stopped(self, writers: Any, claims: Any, seconds: float) -> None:
        self.fake.wait_stopped(writers, claims, seconds)

    def helper(
        self, name: str, manifest: Any, seconds: float
    ) -> AbstractContextManager[str]:
        return self.fake.helper(name, manifest, seconds)

    def exec(self, pod: str, command: list[str], **kwargs: Any) -> int:  # type: ignore[override]
        return self.fake.exec(pod, command, **kwargs)


@pytest.fixture
def shop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Any, Path, FakeCluster]]:
    fake = FakeCluster(tmp_path, [])

    def restore_cluster(self: Backend, target: Any, say: Any = None) -> RestoreCluster:
        cluster = HybridCluster(self._api(target), target.namespace, say=say)
        cluster.fake = fake
        return cluster

    monkeypatch.setattr(Backend, "restore_cluster", restore_cluster)
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
                """
            )
        )
        write_app(tmp_path, PIPELINE.replace("IMAGE", OLD))
        yield api, tmp_path, fake


def write_app(tmp_path: Path, text: str) -> None:
    """Replace app.py; drop the module ``load_target`` cached for it (and the
    bytecode: OLD and NEW have the same size, so a same-second edit would
    otherwise load the stale ``.pyc``)."""
    path = tmp_path / "app.py"
    path.write_text(text)
    shutil.rmtree(tmp_path / "__pycache__", ignore_errors=True)
    name = (
        "_piceli_render_"
        + hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:16]
    )
    sys.modules.pop(name, None)


def deploy(tmp_path: Path, *args: str) -> tuple[int, list[dict[str, Any]], Any]:
    result = CliRunner().invoke(
        cli, ["deploy", str(tmp_path / "app.py:pipeline"), *args]
    )
    lines = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    return result.exit_code, lines, result


def _claims(api: Any, fake: FakeCluster) -> None:
    for index in (0, 1):
        name = f"data-db-{index}"
        api.put(
            {
                "apiVersion": "v1",
                "kind": "PersistentVolumeClaim",
                "metadata": {"name": name, "namespace": TARGET.namespace},
                "spec": {"accessModes": ["ReadWriteOnce"]},
                "status": {"phase": "Bound"},
            }
        )
        (fake.claim_dir(name) / "marker").write_text(f"before the release {index}\n")


def test_an_image_change_backs_up_every_replica_claim_before_the_apply(shop) -> None:
    api, tmp_path, fake = shop
    code, events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 0, result.stdout + result.stderr
    first = events[-1]
    assert first["stages"]["backup"] == "skipped"  # nothing live yet
    assert "restore_point" not in first
    _claims(api, fake)

    write_app(tmp_path, PIPELINE.replace("IMAGE", NEW))
    code, events, result = deploy(tmp_path, "--plan", "--json")
    assert code == 0, result.stderr
    planned = next(e for e in events if e.get("stage") == "backup")["detail"]
    assert [(c["claim"], c["ordinal"], c["why"]) for c in planned["claims"]] == [
        ("data-db-0", 0, ["image of container db changes"]),
        ("data-db-1", 1, ["image of container db changes"]),
    ]
    assert planned["writers"] == [
        {
            "workload": "StatefulSet/db",
            "replicas": 2,
            "claims": ["data-db-0", "data-db-1"],
            "quiesce": [{"type": "exec", "command": ["sync"], "timeout_seconds": 30}],
        }
    ]
    assert "backup   claim data-db-0 of StatefulSet/db" in result.stderr
    assert fake.calls == []  # planning stops nothing
    combined = events[-1]["combined_hash"]

    code, events, result = deploy(tmp_path, "--approve", combined, "--json")
    assert code == 0, result.stdout + result.stderr
    final = events[-1]
    assert final["state"] == "ready"
    assert final["stages"]["backup"] == "done"
    point = final["restore_point"]
    # The hook ran, then the writers stopped and were gone before any copy.
    assert fake.calls[:4] == [
        "hook StatefulSet/db exec",
        "scale StatefulSet/db 0",
        "wait data-db-0,data-db-1",
        f"job piceli-backup-{point[-6:]}-00 ro=True",
    ]
    # The release declares replicas, so it starts the writers (new image).
    assert "scale StatefulSet/db 2" not in fake.calls
    stateful = api.objects[("StatefulSet", "db")]
    assert stateful["spec"]["template"]["spec"]["containers"][0]["image"] == NEW
    directory, record = load_record(tmp_path / "state" / "restore-points", point)
    assert record["state"] == "verified"
    assert [c["claim"] for c in record["claims"]] == ["data-db-0", "data-db-1"]
    assert record["run_id"] == final["run_id"]
    summary = json.loads(Path(final["summary"]["json"]).read_text())
    assert summary["restore_point"]["id"] == point
    assert [c["claim"] for c in summary["restore_point"]["claims"]] == [
        "data-db-0",
        "data-db-1",
    ]
    assert point in Path(final["summary"]["markdown"]).read_text()
    # Claim content never reaches the output.
    assert "before the release" not in result.stdout + result.stderr

    # Unchanged again: no restore point, nothing stopped.
    fake.calls.clear()
    code, events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 0, result.stderr
    assert events[-1]["stages"]["backup"] == "skipped"
    assert fake.calls == []


def test_a_pipeline_without_restore_points_has_no_backup_stage(shop) -> None:
    _api, tmp_path, _fake = shop
    text = (tmp_path / "app.py").read_text()
    write_app(tmp_path, text.replace(" restore_points=RestorePoints(),", ""))
    code, events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 0, result.stderr
    assert set(events[-1]["stages"]) == {
        "inputs",
        "build",
        "deliver",
        "plan",
        "apply",
        "checks",
    }


def test_more_claims_at_run_time_than_approved_are_refused(shop) -> None:
    api, tmp_path, fake = shop
    code, _events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 0, result.stderr
    write_app(tmp_path, PIPELINE.replace("IMAGE", NEW))
    from piceli.app.render import load_target
    from piceli.pipeline.runner import PipelineRunner

    pipeline = load_target(str(tmp_path / "app.py:pipeline"), tmp_path)
    runner = PipelineRunner(pipeline)
    with runner.locked():
        combined = runner.plan()
        assert combined.stages["backup"]["action"] == "skip"
        _claims(api, fake)  # claims appear after the approval
        with pytest.raises(PipelineError) as caught:
            runner.execute(combined, combined.combined_hash)
    assert caught.value.code == "restore-point-plan-changed"
    assert fake.calls == []
    assert (
        api.objects[("StatefulSet", "db")]["spec"]["template"]["spec"]["containers"][0][
            "image"
        ]
        == OLD
    )


def test_a_failing_pre_rollout_check_stops_the_run_before_any_writer_stops(
    shop,
) -> None:
    api, tmp_path, fake = shop
    checked = PIPELINE.replace(
        'app.deployment("web"',
        'app.pre_rollout(db, ["db", "check-config"])\napp.deployment("web"',
    )
    write_app(tmp_path, checked.replace("IMAGE", OLD))
    code, events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 0, result.stdout + result.stderr
    assert {"prerollout", "backup"} <= set(events[-1]["stages"])
    _claims(api, fake)
    before = json.loads(json.dumps(api.objects[("StatefulSet", "db")]))

    write_app(tmp_path, checked.replace("IMAGE", NEW))
    api.job_result("db-cfg", exit_code=3, logs="store format 2 expected\n")
    code, events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 1, result.stdout + result.stderr
    final = events[-1]
    assert (final["state"], final["reason"], final["stage"]) == (
        "failed",
        "prerollout-failed",
        "prerollout",
    )
    assert final["stages"]["backup"] == "pending"
    assert final["stages"]["apply"] == "pending"
    # No hook ran and no writer was scaled down (nor started again).
    assert fake.calls == []
    assert "restore_point" not in final
    points = tmp_path / "state" / "restore-points"
    assert not points.exists() or not any(points.iterdir())
    stateful = api.objects[("StatefulSet", "db")]
    assert stateful["spec"]["replicas"] == before["spec"]["replicas"] == 2
    assert stateful["metadata"]["generation"] == before["metadata"]["generation"]
    assert stateful["spec"]["template"]["spec"]["containers"][0]["image"] == OLD

    # Once the check passes, the same run order takes the restore point,
    # then plans and applies the release from the stopped state.
    api.job_result("db-cfg", exit_code=0)
    code, events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 0, result.stdout + result.stderr
    final = events[-1]
    assert final["stages"]["prerollout"] == "done"
    assert final["stages"]["backup"] == "done"
    assert fake.calls[:2] == ["hook StatefulSet/db exec", "scale StatefulSet/db 0"]
    stateful = api.objects[("StatefulSet", "db")]
    assert stateful["spec"]["template"]["spec"]["containers"][0]["image"] == NEW


def test_a_failed_copy_leaves_the_stateful_set_at_its_replicas(
    shop, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An earlier attempt was killed mid-copy (writers left at 0); the retry's
    copy fails too: the StatefulSet runs again at its replicas on the old
    release before the deploy reports the failure, and the failure says so."""
    api, tmp_path, fake = shop
    code, _events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 0, result.stderr
    _claims(api, fake)

    def scale(self: HybridCluster, kind: str, name: str, replicas: int) -> None:
        self.fake.calls.append(f"scale {kind}/{name} {replicas}")
        api.objects[(kind, name)]["spec"]["replicas"] = replicas

    monkeypatch.setattr(HybridCluster, "scale", scale)
    from piceli.restore.store import write_record

    api.objects[("StatefulSet", "db")]["spec"]["replicas"] = 0
    interrupted = tmp_path / "state" / "restore-points" / "rp-20260101t000000z-abcdef"
    write_record(
        interrupted,
        {
            "schema": "piceli.restore-point.v1",
            "id": interrupted.name,
            "state": "running",
            "namespace": TARGET.namespace,
            "writers": [
                {
                    "workload": "StatefulSet/db",
                    "kind": "StatefulSet",
                    "name": "db",
                    "replicas": 2,
                }
            ],
            "claims": [],
        },
    )
    fake.fail["tar -czf"] = 2
    write_app(tmp_path, PIPELINE.replace("IMAGE", NEW))
    code, events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 1, result.stdout + result.stderr
    stateful = api.objects[("StatefulSet", "db")]
    assert stateful["spec"]["replicas"] == 2
    assert stateful["spec"]["template"]["spec"]["containers"][0]["image"] == OLD
    assert "started again on the running release: StatefulSet/db" in result.stderr
    assert events[-1]["reason"] == "restore-point-copy-failed"
