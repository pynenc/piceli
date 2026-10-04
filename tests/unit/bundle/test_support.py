"""``piceli support-bundle``: GET only, never Secrets or env values, redacted logs."""

from __future__ import annotations

import gzip
import io
import json
import tarfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from piceli.bundle.support import Collector, SupportError, archive, write
from piceli.k8s.cli import app as cli
from piceli.testing import FakeCluster, fake_cluster, manifest

SECRET_VALUE = "hunter2-very-secret"
ENV_VALUE = "env-value-never-collected"


@pytest.fixture
def cluster() -> Iterator[FakeCluster]:
    with fake_cluster() as running:
        api = running.api
        namespace = running.namespace
        api.put(manifest("Secret", "shop-private", value="aHVudGVyMg=="))
        api.put(manifest("ConfigMap", "settings", value=ENV_VALUE))
        api.put(
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {
                    "name": "web-1",
                    "namespace": namespace,
                    "labels": {"app.kubernetes.io/part-of": "shop"},
                },
                "spec": {
                    "containers": [
                        {
                            "name": "web",
                            "image": "registry.example/web@sha256:" + "a" * 64,
                            "env": [
                                {"name": "MODE", "value": ENV_VALUE},
                                {
                                    "name": "TOKEN",
                                    "valueFrom": {"secretKeyRef": {"name": "shop-private", "key": "token"}},
                                },
                            ],
                            "readinessProbe": {"httpGet": {"path": "/", "port": 8080}},
                        }
                    ]
                },
                "status": {
                    "phase": "Running",
                    "containerStatuses": [
                        {"name": "web", "ready": True, "restartCount": 1, "image": "web", "imageID": "sha256:x",
                         "state": {"running": {}}}
                    ],
                },
            }
        )  # fmt: skip
        api.pod_logs[("web-1", "web", False)] = (
            f"started\nconnecting with password={SECRET_VALUE}\nready\n"
        )
        api.pod_logs[("web-1", "web", True)] = f"Authorization: Bearer {SECRET_VALUE}\n"
        api.events.append(
            {
                "type": "Warning",
                "reason": "Failed",
                "involvedObject": {"kind": "Pod", "name": "web-1"},
                "message": f"probe failed token={SECRET_VALUE}",
                "count": 2,
            }
        )
        yield running


def _transport(cluster: FakeCluster, tmp_path: Path):
    from piceli.bundle.support import api_transport
    from piceli.k8s.ops.provider_factory import api_client_from_kubeconfig

    kubeconfig = cluster.kubeconfig(tmp_path / "kubeconfig")
    client = api_client_from_kubeconfig(
        Path(kubeconfig), "fake", transport="loopback-http"
    )
    return api_transport(client)


def _files(data: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(data))) as tar:
        return {
            info.name.removeprefix("support-bundle/"): tar.extractfile(info).read()  # type: ignore[union-attr]
            for info in tar.getmembers()
        }


def test_collects_status_logs_and_events_with_get_only(
    cluster: FakeCluster, tmp_path: Path
) -> None:
    collector = Collector(
        _transport(cluster, tmp_path), cluster.namespace, log_lines=50
    )
    cluster.api.requests.clear()
    files = collector.collect()
    methods = {request["method"] for request in cluster.api.requests}
    assert methods == {"GET"}
    assert not any("/secrets" in request["path"] for request in cluster.api.requests)
    pod = files["pods.json"][0]
    assert pod["containers"][0]["env_names"] == ["MODE", "TOKEN"]
    assert pod["statuses"][0]["restarts"] == 1
    assert files["configmaps.json"] == [{"name": "settings", "keys": ["mode"]}]
    log = files["logs/web-1.web.log"]
    assert "started" in log and "ready" in log and "[REDACTED]" in log
    assert "[REDACTED]" in files["logs/web-1.web.previous.log"]
    assert "[REDACTED]" in files["events.json"][0]["message"]
    data = archive(
        files,
        namespace=cluster.namespace,
        context="fake",
        requests=collector.requests,
        problems=collector.problems,
        created=0,
    )
    everything = b"".join(_files(data).values())
    assert SECRET_VALUE.encode() not in everything
    assert ENV_VALUE.encode() not in everything
    unpacked = _files(data)
    manifest_ = json.loads(unpacked["manifest.json"])
    assert manifest_["requests"]["methods"] == ["GET"]
    assert set(manifest_["files"]) == set(unpacked) - {"manifest.json"}
    assert manifest_["redaction"]["secrets"] == "never requested"


def test_a_write_method_is_refused_before_it_is_sent() -> None:
    sent: list[str] = []
    collector = Collector(
        lambda method, path, query: sent.append(method) or (200, b"{}"), "shop"
    )
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        with pytest.raises(SupportError) as caught:
            collector._send(method, "/api/v1/namespaces/shop/pods", {})
        assert caught.value.code == "support-bundle-write-refused"
    assert sent == []


def test_a_missing_namespace_is_refused(cluster: FakeCluster, tmp_path: Path) -> None:
    collector = Collector(_transport(cluster, tmp_path), "elsewhere")
    with pytest.raises(SupportError) as caught:
        collector.collect()
    assert caught.value.code == "support-bundle-namespace-missing"


def test_the_archive_is_private_and_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "support.tar.gz"
    write(path, b"one")
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(SupportError) as caught:
        write(path, b"two")
    assert caught.value.code == "support-bundle-out-refused"
    assert path.read_bytes() == b"one"


def test_cli_writes_the_archive(cluster: FakeCluster, tmp_path: Path) -> None:
    kubeconfig = cluster.kubeconfig(tmp_path / "kubeconfig")
    out = tmp_path / "support.tar.gz"
    result = CliRunner().invoke(
        cli,
        [
            "support-bundle", "--namespace", cluster.namespace, "--out", str(out),
            "--kubeconfig", str(kubeconfig), "--context", "fake",
            "--transport", "loopback-http",
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["state"] == "written" and body["methods"] == ["GET"]
    assert "manifest.json" in _files(out.read_bytes())
