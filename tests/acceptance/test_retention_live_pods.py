"""Retention reads the digests running workloads use through the real Kubernetes
client (loopback fake API) with an explicit kubeconfig and context."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from piceli.artifacts.retention import LiveWorkloads, read_live_pods
from tests.acceptance import fake_api
from tests.acceptance.fake_api import TARGET, serve, write_kubeconfig

RUNNING = "sha256:" + "a" * 64
INIT = "sha256:" + "b" * 64


def test_live_digests_come_from_the_pods_of_the_cluster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(fake_api.TYPES, "pods", ("v1", "Pod", True))
    with serve() as (api, url):
        kubeconfig = write_kubeconfig(url, tmp_path / "kubeconfig")
        api.objects[("Pod", "web-1")] = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"name": "web-1", "namespace": TARGET.namespace},
            "spec": {
                "containers": [{"name": "web", "image": "127.0.0.1:5000/app/web:1"}]
            },
            "status": {
                "phase": "Running",
                "containerStatuses": [
                    {
                        "name": "web",
                        "image": "127.0.0.1:5000/app/web:1",
                        "imageID": f"127.0.0.1:5000/app/web@{RUNNING}",
                    }
                ],
                "initContainerStatuses": [
                    {"name": "i", "image": "x", "imageID": f"x/y@{INIT}"}
                ],
            },
        }
        pods = read_live_pods(
            kubeconfig, "fake", [TARGET.namespace], transport="loopback-http"
        )
    live = LiveWorkloads.from_pods(pods)
    assert live.known and live.digests == {RUNNING, INIT}
    assert ("app/web", "1") in live.tags


def test_templates_of_scaled_to_zero_and_scheduled_workloads_are_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(fake_api.TYPES, "pods", ("v1", "Pod", True))
    zero, cron = "sha256:" + "c" * 64, "sha256:" + "d" * 64

    def template(image: str) -> dict:
        return {"spec": {"containers": [{"name": "c", "image": image}]}}

    with serve() as (api, url):
        kubeconfig = write_kubeconfig(url, tmp_path / "kubeconfig")
        meta = {"namespace": TARGET.namespace}
        api.objects[("Deployment", "idle")] = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "idle", **meta},
            "spec": {"replicas": 0, "template": template(f"r/app/idle@{zero}")},
        }
        api.objects[("CronJob", "nightly")] = {
            "apiVersion": "batch/v1",
            "kind": "CronJob",
            "metadata": {"name": "nightly", **meta},
            "spec": {
                "schedule": "0 3 * * *",
                "jobTemplate": {"spec": {"template": template(f"r/app/job@{cron}")}},
            },
        }
        items = read_live_pods(
            kubeconfig, "fake", [TARGET.namespace], transport="loopback-http"
        )
    live = LiveWorkloads.from_objects(items)
    assert live.by[zero] == {"deployment"} and live.by[cron] == {"cronjob"}


def test_environment_records_and_rollout_history_are_read_from_the_cluster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(fake_api.TYPES, "pods", ("v1", "Pod", True))
    recorded, old = "sha256:" + "e" * 64, "sha256:" + "f" * 64
    meta = {"namespace": TARGET.namespace}
    with serve() as (api, url):
        kubeconfig = write_kubeconfig(url, tmp_path / "kubeconfig")
        api.objects[("ConfigMap", "piceli-env")] = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": "piceli-env",
                "labels": {"app.kubernetes.io/managed-by": "piceli"},
                **meta,
            },
            "data": {"record": json.dumps({"images": {"api": {"digest": recorded}}})},
        }
        api.objects[("ReplicaSet", "api-old")] = {
            "apiVersion": "apps/v1",
            "kind": "ReplicaSet",
            "metadata": {
                "name": "api-old",
                "ownerReferences": [
                    {
                        "apiVersion": "apps/v1",
                        "kind": "Deployment",
                        "name": "api",
                        "uid": "u",
                    }
                ],
                **meta,
            },
            "spec": {
                "replicas": 0,
                "template": {
                    "spec": {
                        "containers": [
                            {"name": "c", "image": f"r.example/app/api@{old}"}
                        ]
                    }
                },
            },
        }
        items = read_live_pods(
            kubeconfig, "fake", [TARGET.namespace], transport="loopback-http"
        )
    live = LiveWorkloads.from_objects(items)
    assert live.by[recorded] == {"environment"}
    assert old not in live.digests
    assert live.refs[("app/api", old)] == {"replicaset:history"}


def test_rollback_targets_are_read_from_replica_sets_and_controller_revisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(fake_api.TYPES, "pods", ("v1", "Pod", True))
    prev_api, prev_db = "sha256:" + "1" * 64, "sha256:" + "2" * 64
    meta = {"namespace": TARGET.namespace}

    def containers(image: str) -> dict:
        return {"spec": {"containers": [{"name": "c", "image": image}]}}

    with serve() as (api, url):
        kubeconfig = write_kubeconfig(url, tmp_path / "kubeconfig")
        api.objects[("ReplicaSet", "api-2")] = {
            "apiVersion": "apps/v1",
            "kind": "ReplicaSet",
            "metadata": {
                "name": "api-2",
                "annotations": {"deployment.kubernetes.io/revision": "2"},
                "ownerReferences": [
                    {
                        "apiVersion": "apps/v1",
                        "kind": "Deployment",
                        "name": "api",
                        "uid": "u",
                    }
                ],
                **meta,
            },
            "spec": {
                "replicas": 0,
                "template": containers(f"r.example/app/api@{prev_api}"),
            },
        }
        api.objects[("ControllerRevision", "db-r1")] = {
            "apiVersion": "apps/v1",
            "kind": "ControllerRevision",
            "metadata": {
                "name": "db-r1",
                "ownerReferences": [
                    {
                        "apiVersion": "apps/v1",
                        "kind": "StatefulSet",
                        "name": "db",
                        "uid": "v",
                    }
                ],
                **meta,
            },
            "revision": 1,
            "data": {"spec": {"template": containers(f"r.example/app/db@{prev_db}")}},
        }
        api.objects[("ControllerRevision", "db-r2")] = {
            **api.objects[("ControllerRevision", "db-r1")],
            "metadata": {
                **api.objects[("ControllerRevision", "db-r1")]["metadata"],
                "name": "db-r2",
            },
            "revision": 2,
            "data": {"spec": {"template": containers("r.example/app/db:2")}},
        }
        items = read_live_pods(
            kubeconfig, "fake", [TARGET.namespace], transport="loopback-http"
        )
    live = LiveWorkloads.from_objects(items)
    assert live.rollback == {
        prev_api: {"replicaset:rollback"},
        prev_db: {"controllerrevision:rollback"},
    }
    assert live.refs[("app/db", prev_db)] == {"controllerrevision:history"}
