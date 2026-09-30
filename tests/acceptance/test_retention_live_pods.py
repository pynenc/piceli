"""Retention reads the digests running workloads use through the real Kubernetes
client (loopback fake API) with an explicit kubeconfig and context."""

from __future__ import annotations

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
