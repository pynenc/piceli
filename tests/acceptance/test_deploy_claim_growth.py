"""Acceptance: ``piceli deploy`` grows or moves a retained claim.

The release engine and every claim read and write (storage classes, claim
patches, new claims) go to the in-process fake API through the real client;
stopping writers, the helper Job and its byte streams are faked with local
directories as in ``test_deploy_restore_points.py``. kind covers the real
copy (``tests/integration/test_claim_growth_kind.py``).
"""

from __future__ import annotations

import json
import shutil
import textwrap
from collections.abc import Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import pytest

from piceli.pipeline.backend import Backend
from piceli.restore.claims import MIGRATED_FROM, MIGRATED_TO
from piceli.restore.cluster import RestoreCluster
from tests.acceptance.fake_api import TARGET, serve
from tests.acceptance.test_deploy_restore_points import deploy, write_app
from tests.unit.restore.fake_cluster import FakeCluster

pytestmark = pytest.mark.skipif(
    shutil.which("sha256sum") is None, reason="needs sha256sum"
)

CACHE = "example/cache@sha256:" + "3" * 64
NS = TARGET.namespace

HEAD = f"""
from piceli import App, ClaimTemplate, ExistingClaim, Pipeline, RestorePoints, Target

target = Target.kubeconfig(
    "kubeconfig", context="fake", namespace="{NS}", transport="loopback-http",
)
app = App("shop")
"""
TAIL = """
pipeline = Pipeline(
    app, target, state_dir="state", restore_points=RestorePoints(), REPLACE
    execution={"max_seconds": 30, "readiness_seconds": 1, "poll_seconds": 0.05},
)
"""


def cache_app(volume: str) -> str:
    return (
        HEAD
        + f'app.deployment("cache", image="{CACHE}", replicas=1, volumes={{"/data": {volume}}})\n'
        + TAIL.replace("REPLACE", "")
    )


def db_app(template: str) -> str:
    return (
        HEAD
        + f'app.stateful_set("db", image="{CACHE}", replicas=2, volumes={{"/var/lib/db": {template}}})\n'
        + TAIL.replace("REPLACE", 'replace=["StatefulSet/db"],')
    )


class GrowingCluster(RestoreCluster):
    """Claim reads and writes on the fake API; writers and streams faked.

    A claim patched to a larger size reports that capacity at once (what a
    CSI driver with online expansion does).
    """

    fake: FakeCluster
    api: Any

    def scale(self, kind: str, name: str, replicas: int) -> None:
        self.fake.calls.append(f"scale {kind}/{name} {replicas}")
        self.api.objects[(kind, name)]["spec"]["replicas"] = replicas

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

    def expand_claim(self, name: str, size: str) -> None:
        super().expand_claim(name, size)
        self.fake.calls.append(f"expand {name} {size}")

    def claim(self, name: str) -> dict[str, Any] | None:
        value = super().claim(name)
        if value is not None:  # the volume has the size the claim requests
            requested = value["spec"].get("resources", {}).get("requests", {})
            value.setdefault("status", {})["capacity"] = dict(requested)
        return value


@pytest.fixture
def shop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Any, Path, FakeCluster]]:
    fake = FakeCluster(tmp_path, [])

    with serve() as (api, url):

        def restore_cluster(
            self: Backend, target: Any, say: Any = None
        ) -> RestoreCluster:
            cluster = GrowingCluster(self._api(target), target.namespace, say=say)
            cluster.fake, cluster.api = fake, api
            return cluster

        monkeypatch.setattr(Backend, "restore_cluster", restore_cluster)
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
        for name, expands in (("local-path", False), ("expandable", True)):
            api.put(
                {
                    "apiVersion": "storage.k8s.io/v1",
                    "kind": "StorageClass",
                    "metadata": {"name": name},
                    "provisioner": "example.test/" + name,
                    **({"allowVolumeExpansion": True} if expands else {}),
                }
            )
        yield api, tmp_path, fake


def put_claim(
    api: Any, fake: FakeCluster, name: str, size: str, storage_class: str
) -> None:
    api.put(
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": name, "namespace": NS},
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "storageClassName": storage_class,
                "resources": {"requests": {"storage": size}},
            },
            "status": {"phase": "Bound", "capacity": {"storage": size}},
        }
    )
    (fake.claim_dir(name) / "marker").write_text(f"secret-marker {name}\n")


def claim_of(api: Any, name: str) -> dict[str, Any]:
    return api.objects[("PersistentVolumeClaim", name)]


def mounted(api: Any) -> str:
    (volume,) = api.objects[("Deployment", "cache")]["spec"]["template"]["spec"][
        "volumes"
    ]
    return str(volume["persistentVolumeClaim"]["claimName"])


def test_a_claim_without_expansion_moves_to_a_larger_claim(shop) -> None:
    api, tmp_path, fake = shop
    write_app(tmp_path, cache_app('ExistingClaim("cache-state")'))
    code, _events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 0, result.stdout + result.stderr
    put_claim(api, fake, "cache-state", "512Mi", "local-path")
    moved = 'ExistingClaim("cache-state-2", size="8Gi", migrate_from="cache-state")'
    write_app(tmp_path, cache_app(moved))

    code, events, result = deploy(tmp_path, "--plan", "--json")
    assert code == 0, result.stdout + result.stderr
    backup = next(e for e in events if e.get("stage") == "backup")["detail"]
    assert [
        (s["action"], s["claim"], s["target"], s["from"], s["to"], s["storage_class"])
        for s in backup["claim_changes"]
    ] == [("migrate", "cache-state", "cache-state-2", "512Mi", "8Gi", "local-path")]
    assert "move claim cache-state of Deployment/cache (512Mi) to new claim" in (
        result.stderr
    )
    assert fake.calls == []
    assert ("PersistentVolumeClaim", "cache-state-2") not in api.objects

    code, events, result = deploy(
        tmp_path, "--approve", events[-1]["combined_hash"], "--json"
    )
    assert code == 0, result.stdout + result.stderr
    final = events[-1]
    assert final["state"] == "ready"
    # Switched: the release mounts the new claim; the old one is kept.
    assert mounted(api) == "cache-state-2"
    new, old = claim_of(api, "cache-state-2"), claim_of(api, "cache-state")
    assert new["spec"]["resources"]["requests"]["storage"] == "8Gi"
    assert new["spec"]["storageClassName"] == "local-path"
    assert new["metadata"]["annotations"][MIGRATED_FROM] == "cache-state"
    assert old["metadata"]["annotations"][MIGRATED_TO] == "cache-state-2"
    assert old["spec"]["resources"]["requests"]["storage"] == "512Mi"
    assert (fake.claim_dir("cache-state-2") / "marker").read_text() == (
        "secret-marker cache-state\n"
    )
    assert (fake.claim_dir("cache-state") / "marker").exists()
    # Stopped once, never started on the old claim: the apply starts it.
    assert fake.calls.count("scale Deployment/cache 0") == 1
    assert "scale Deployment/cache 1" not in fake.calls
    summary = json.loads(Path(final["summary"]["json"]).read_text())
    assert summary["restore_point"]["claim_changes"] == [
        {
            "action": "migrate",
            "claim": "cache-state",
            "target": "cache-state-2",
            "to": "8Gi",
            "verify": "not-declared",
        }
    ]
    assert "secret-marker" not in result.stdout + result.stderr

    # Done: the next run has nothing to move and stops nothing.
    fake.calls.clear()
    code, events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 0, result.stdout + result.stderr
    assert events[-1]["stages"]["backup"] == "skipped"
    assert fake.calls == []


def test_a_failed_copy_restarts_the_writer_on_its_old_claim(shop) -> None:
    api, tmp_path, fake = shop
    write_app(tmp_path, cache_app('ExistingClaim("cache-state")'))
    code, _events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 0, result.stdout + result.stderr
    put_claim(api, fake, "cache-state", "512Mi", "local-path")
    moved = 'ExistingClaim("cache-state-2", size="8Gi", migrate_from="cache-state")'
    write_app(tmp_path, cache_app(moved))
    fake.fail["head -c"] = 2  # the extraction into the new claim fails

    code, events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 1, result.stdout + result.stderr
    final = events[-1]
    assert (final["state"], final["reason"], final["stage"]) == (
        "failed",
        "claim-migration-failed",
        "backup",
    )
    assert final["stages"]["apply"] == "pending"
    # Not switched, writer started again on the old claim, nothing deleted.
    assert mounted(api) == "cache-state"
    assert fake.calls.count("scale Deployment/cache 0") == 1
    assert fake.calls[-1] == "scale Deployment/cache 1"
    assert api.objects[("Deployment", "cache")]["spec"]["replicas"] == 1
    assert MIGRATED_TO not in (
        claim_of(api, "cache-state")["metadata"].get("annotations") or {}
    )
    assert (
        claim_of(api, "cache-state-2")["metadata"]["annotations"][MIGRATED_FROM]
        == "cache-state"
    )

    # Safe to run again: the new claim is filled again, then switched.
    fake.fail.clear()
    fake.calls.clear()
    code, events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 0, result.stdout + result.stderr
    assert mounted(api) == "cache-state-2"
    assert "filling it again" in result.stderr


def test_an_expandable_template_grows_its_claims_and_recreates_the_statefulset(
    shop,
) -> None:
    api, tmp_path, fake = shop
    write_app(
        tmp_path,
        db_app('ClaimTemplate("data", size="1Gi", storage_class="expandable")'),
    )
    code, _events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 0, result.stdout + result.stderr
    for index in (0, 1):
        put_claim(api, fake, f"data-db-{index}", "1Gi", "expandable")
    uid = api.objects[("StatefulSet", "db")]["metadata"]["uid"]
    write_app(
        tmp_path,
        db_app('ClaimTemplate("data", size="2Gi", storage_class="expandable")'),
    )

    code, events, result = deploy(tmp_path, "--plan", "--json")
    assert code == 0, result.stdout + result.stderr
    backup = next(e for e in events if e.get("stage") == "backup")["detail"]
    assert [(s["action"], s["claim"], s["to"]) for s in backup["claim_changes"]] == [
        ("expand", "data-db-0", "2Gi"),
        ("expand", "data-db-1", "2Gi"),
    ]
    assert "grow claim data-db-0 of StatefulSet/db from 1Gi to 2Gi" in result.stderr
    code, events, result = deploy(
        tmp_path, "--approve", events[-1]["combined_hash"], "--json"
    )
    assert code == 0, result.stdout + result.stderr
    for index in (0, 1):
        claim = claim_of(api, f"data-db-{index}")
        assert claim["spec"]["resources"]["requests"]["storage"] == "2Gi"
    stateful = api.objects[("StatefulSet", "db")]
    assert stateful["metadata"]["uid"] != uid  # recreated (orphan), claims kept
    (template,) = stateful["spec"]["volumeClaimTemplates"]
    assert template["spec"]["resources"]["requests"]["storage"] == "2Gi"
    assert fake.calls.index("scale StatefulSet/db 0") < fake.calls.index(
        "expand data-db-0 2Gi"
    )


def test_a_smaller_claim_is_refused_at_plan(shop) -> None:
    api, tmp_path, fake = shop
    write_app(
        tmp_path,
        db_app('ClaimTemplate("data", size="2Gi", storage_class="expandable")'),
    )
    code, _events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 0, result.stdout + result.stderr
    put_claim(api, fake, "data-db-0", "2Gi", "expandable")
    write_app(
        tmp_path,
        db_app('ClaimTemplate("data", size="1Gi", storage_class="expandable")'),
    )
    code, events, result = deploy(tmp_path, "--plan", "--json")
    assert code == 2, result.stdout + result.stderr
    assert events[-1]["reason"] == "claim-shrink-refused"
    assert fake.calls == []


def test_claim_growth_needs_restore_points(shop) -> None:
    _api, tmp_path, _fake = shop
    moved = 'ExistingClaim("cache-state-2", size="8Gi", migrate_from="cache-state")'
    write_app(
        tmp_path,
        cache_app(moved).replace(" restore_points=RestorePoints(),", ""),
    )
    code, events, result = deploy(tmp_path, "--plan", "--json")
    assert code == 2, result.stdout + result.stderr
    assert events[-1]["reason"] == "claim-growth-needs-restore-points"
