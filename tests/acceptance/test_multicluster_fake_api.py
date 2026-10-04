"""Several clusters (0.15) against one fake API per cluster.

- ``piceli secrets cluster`` writes another cluster's kubeconfig Secret on
  the home cluster, from a kubeconfig file or a token on stdin, and never
  prints a credential; an exec plugin is refused before any request;
- the controller's :class:`RemoteClusters` reads that Secret, writes the
  kubeconfig privately and probes the cluster: reachable, unreachable (a
  closed port), missing credentials;
- an image is copied by digest into another cluster's in-cluster registry
  through that cluster's API server proxy (no registry exposed);
- removing a placement deletes only the app's objects there, keeps claims
  (with their delete command) and the namespace that holds them.
"""

from __future__ import annotations

import base64
import json
import socket
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.gitops.install import Api
from piceli.infra import cluster_init
from piceli.infra.multicluster import (
    ProxiedRegistryClient,
    RemoteClusters,
    copy_images,
    remove_placement,
    secret_name,
)
from piceli.k8s.cli import app
from piceli.k8s.ops.provider_factory import api_client_from_kubeconfig
from piceli.profiles import save_profile
from piceli.testing import FakeAPI, fake_cluster, write_kubeconfig
from tests.oci_registry import oci_registry

TOKEN = "edge-token-not-real-0123456789"


def _token_kubeconfig(url: str, path: Path, *, user: dict[str, Any]) -> Path:
    path.write_text(
        json.dumps(
            {
                "apiVersion": "v1",
                "kind": "Config",
                "clusters": [{"name": "edge", "cluster": {"server": url}}],
                "users": [{"name": "edge", "user": user}],
                "contexts": [
                    {"name": "edge", "context": {"cluster": "edge", "user": "edge"}}
                ],
            }
        )
    )
    return path


@pytest.fixture
def clusters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, Any]]:
    monkeypatch.setenv("PICELI_PROFILES_DIR", str(tmp_path / "profiles"))
    monkeypatch.setattr(cluster_init, "ui_renderer", lambda: None)
    home_api = FakeAPI(namespace="piceli-system")
    home_api.put(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": cluster_init.CLUSTER_CONFIG,
                "namespace": "piceli-system",
            },
            "data": {},
        }
    )
    edge_api = FakeAPI(namespace="shop-edge")
    with fake_cluster(home_api) as home, fake_cluster(edge_api) as edge:
        save_profile(
            "home", write_kubeconfig(home.url, tmp_path / "home.kubeconfig"), "fake"
        )
        edge_kubeconfig = _token_kubeconfig(
            edge.url, tmp_path / "edge.kubeconfig", user={"token": TOKEN}
        )
        save_profile("edge-a", edge_kubeconfig, "edge")
        (tmp_path / "infra.py").write_text(
            f"""
from piceli import Registry
from piceli.envs import Environment, Stack
from piceli.infra import Cluster, Component, Controller, Node, Source

home = Cluster("home", api="{home.url}", credentials="home",
               nodes=[Node("node-a", arch="amd64", roles=["controller", "registry"])],
               registry=Registry.in_cluster(on="node-a"),
               controller=Controller(on="node-a"))
edge_a = Cluster("edge-a", api="{edge.url}", credentials="edge-a",
                 nodes=[Node("edge-1", arch="amd64", roles=["registry"])],
                 registry=Registry.in_cluster(on="edge-1", namespace="shop-edge"))
shop = Source("https://example.com/shop.git", name="shop")
cache = Component.image(
    "docker.io/library/busybox:1.37.0", pin="sha256:" + "b" * 64, name="cache",
    contract={{"ports": {{"cache": 6379}}}},
)
environments = [
    Environment("edge", namespace="shop-edge", stack=Stack("s", [cache]),
                follow={{shop: "main"}}, clusters=[edge_a]),
]
"""
        )
        yield {
            "home": home_api,
            "edge": edge_api,
            "home_url": home.url,
            "edge_url": edge.url,
            "edge_kubeconfig": edge_kubeconfig,
            "ref": f"{tmp_path / 'infra.py'}:edge_a",
            "tmp": tmp_path,
        }


def _secrets(*args: str, stdin: str | None = None) -> Any:
    return CliRunner().invoke(
        app,
        ["secrets", "cluster", *args, "--transport", "loopback-http"],
        input=stdin,
    )


def _stored(api: FakeAPI, name: str = "edge-a") -> dict[str, Any]:
    secret = api.objects[("Secret", secret_name(name))]
    return dict(json.loads(base64.b64decode(secret["data"]["kubeconfig"])))


def test_secrets_cluster_stores_a_kubeconfig_and_never_prints_it(
    clusters: dict[str, Any],
) -> None:
    home = clusters["home"]
    created = _secrets(
        "--cluster",
        clusters["ref"],
        "--kubeconfig",
        str(clusters["edge_kubeconfig"]),
        "--context",
        "edge",
        "--server",
        "https://100.64.0.10:6443",
    )
    assert created.exit_code == 0, created.output
    body = json.loads(created.stdout)
    assert body == {
        "state": "created",
        "secret": {
            "namespace": "piceli-system",
            "name": "piceli-cluster-edge-a",
            "keys": ["kubeconfig"],
        },
        "cluster": "edge-a",
        "home": "home",
        "server": "https://100.64.0.10:6443",
    }
    stored = _stored(home)
    assert stored["clusters"][0]["cluster"]["server"] == "https://100.64.0.10:6443"
    assert stored["users"][0]["user"] == {"token": TOKEN}
    assert len(stored["contexts"]) == 1
    # A token on stdin (the CA from the cluster's profile) updates it.
    updated = _secrets(
        "--cluster", clusters["ref"], "--prompt", stdin="second-" + TOKEN + "\n"
    )
    assert updated.exit_code == 0, updated.output
    assert json.loads(updated.stdout)["state"] == "updated"
    assert _stored(home)["users"][0]["user"] == {"token": "second-" + TOKEN}
    assert _stored(home)["clusters"][0]["cluster"]["server"] == clusters["edge_url"]
    for result in (created, updated):
        assert TOKEN not in result.stdout and TOKEN not in result.stderr


def test_secrets_cluster_refusals_send_nothing(clusters: dict[str, Any]) -> None:
    home = clusters["home"]
    before = len(home.requests)
    plugin = _token_kubeconfig(
        clusters["edge_url"],
        clusters["tmp"] / "exec.kubeconfig",
        user={"exec": {"command": "get-token", "apiVersion": "v1"}},
    )
    refused = _secrets(
        "--cluster", clusters["ref"], "--kubeconfig", str(plugin), "--context", "edge"
    )
    assert refused.exit_code == 2
    assert json.loads(refused.stdout)["reason"] == "cluster-credentials-unsupported"
    argument = _secrets("--cluster", clusters["ref"], "--prompt", TOKEN)
    assert json.loads(argument.stdout)["reason"] == "secrets-token-refused"
    assert TOKEN not in argument.stdout + argument.stderr
    neither = _secrets("--cluster", clusters["ref"])
    assert json.loads(neither.stdout)["reason"] == "secrets-prompt-required"
    other = _token_kubeconfig(
        "http://127.0.0.1:1",
        clusters["tmp"] / "other.kubeconfig",
        user={"token": TOKEN},
    )
    mismatch = _secrets(
        "--cluster", clusters["ref"], "--kubeconfig", str(other), "--context", "edge"
    )
    assert json.loads(mismatch.stdout)["reason"] == "cluster-api-mismatch"
    assert len(home.requests) == before


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_remote_clusters_read_the_secret_and_probe(
    clusters: dict[str, Any], tmp_path: Path
) -> None:
    assert (
        _secrets(
            "--cluster",
            clusters["ref"],
            "--kubeconfig",
            str(clusters["edge_kubeconfig"]),
            "--context",
            "edge",
        ).exit_code
        == 0
    )
    home_client = api_client_from_kubeconfig(
        tmp_path / "home.kubeconfig", "fake", transport="loopback-http"
    )
    private = tmp_path / "private"
    remote = RemoteClusters(
        Api(home_client),
        namespace="piceli-system",
        private_dir=private,
        transport="loopback-http",
        probe_seconds=2,
    )
    try:
        reached = remote.probe("edge-a")
        assert reached.reachable and reached.last_contact is not None
        path, _ = remote.kubeconfig("edge-a")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        # The edge cluster is reached with the stored token.
        assert any(
            r.get("authorization") == f"Bearer {TOKEN}"
            for r in clusters["edge"].requests
        )
        missing = remote.probe("edge-b")
        assert not missing.reachable and missing.reason == "cluster-credentials-missing"
        # A cluster whose API does not answer.
        from piceli.infra.multicluster import token_kubeconfig, write_cluster_secret

        write_cluster_secret(
            Api(home_client),
            "edge-c",
            token_kubeconfig(f"http://127.0.0.1:{_closed_port()}", TOKEN, None),
            namespace="piceli-system",
        )
        down = remote.probe("edge-c")
        assert not down.reachable and down.reason == "cluster-unreachable"
        assert down.last_contact is None
    finally:
        home_client.close()


def test_images_are_copied_through_the_clusters_api_proxy(
    clusters: dict[str, Any],
) -> None:
    from piceli.artifacts.registry import RegistryEndpoint, StreamedOciRegistryClient
    from piceli.pipeline.model import Registry

    registry = Registry.in_cluster(on="edge-1", namespace="shop-edge")
    with oci_registry() as home_registry, oci_registry() as edge_registry:
        digest, _ = home_registry.add_image("shop/web", arch="amd64")
        clusters["edge"].proxy_upstream(
            f"service/{registry.name}", registry.port, f"http://{edge_registry.host}"
        )
        client = api_client_from_kubeconfig(
            clusters["edge_kubeconfig"], "edge", transport="loopback-http"
        )
        source_host, source_port = home_registry.host.split(":")
        try:
            copied = copy_images(
                {
                    "web": f"{home_registry.host}/shop/web@{digest}",
                    "external": "docker.io/library/redis@sha256:" + "a" * 64,
                },
                home_host=home_registry.host,
                target_host=registry.host,
                source=StreamedOciRegistryClient(
                    RegistryEndpoint(host=source_host, port=int(source_port)),
                    actions="pull",
                ),
                target=ProxiedRegistryClient(client, registry),
            )
        finally:
            client.close()
        assert copied["web"] == f"{registry.host}/shop/web@{digest}"
        assert copied["external"].startswith("docker.io/")  # not the home registry's
        assert ("shop/web", digest) in edge_registry.manifests
        proxied = [r for r in clusters["edge"].requests if r.get("proxied")]
        assert proxied and all(r["authorization"] == f"Bearer {TOKEN}" for r in proxied)


def _labelled(kind: str, name: str, labels: dict[str, str]) -> dict[str, Any]:
    from piceli.testing.fake_api import manifest

    value = manifest(kind, name)
    value["metadata"]["namespace"] = "shop-edge"
    value["metadata"]["labels"] = labels
    return value


def test_removing_a_placement_deletes_only_the_apps_objects(
    clusters: dict[str, Any],
) -> None:
    edge = clusters["edge"]
    part_of = {"app.kubernetes.io/part-of": "shop"}
    edge.put(
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": "shop-edge",
                "labels": {
                    "app.kubernetes.io/managed-by": "piceli",
                    "piceli.io/env-name": "edge",
                },
            },
        }
    )
    edge.put(_labelled("Deployment", "web", part_of))
    edge.put(_labelled("Service", "web", part_of))
    edge.put(
        _labelled("ConfigMap", "other-app", {"app.kubernetes.io/part-of": "other"})
    )
    edge.put(_labelled("Secret", "web-token", part_of))
    client = api_client_from_kubeconfig(
        clusters["edge_kubeconfig"], "edge", transport="loopback-http"
    )
    try:
        api = Api(client)
        removed = remove_placement(api, namespace="shop-edge", app="shop", env="edge")
        assert {(d["kind"], d["name"]) for d in removed["deleted"]} == {
            ("Deployment", "web"),
            ("Service", "web"),
        }
        assert ("ConfigMap", "other-app") in edge.objects
        assert ("Secret", "web-token") in edge.objects
        assert removed["kept"] == [
            {
                "kind": "Secret",
                "name": "web-token",
                "namespace": "shop-edge",
                "command": "kubectl -n shop-edge delete secret web-token",
            }
        ]
        assert removed["namespace"] == "kept"  # it holds what was kept
        assert ("Namespace", "shop-edge") in edge.objects
        # Nothing kept any more: the namespace Piceli created goes too.
        del edge.objects[("Secret", "web-token")]
        again = remove_placement(api, namespace="shop-edge", app="shop", env="edge")
        assert again == {"deleted": [], "kept": [], "namespace": "deleted"}
    finally:
        client.close()


def test_register_cluster_saves_the_profile_and_the_secret(
    clusters: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The function infrastructure provisioning calls for a new machine."""
    from piceli.infra.composition import load_composition
    from piceli.infra.multicluster import register_cluster
    from piceli.profiles import load_profile, remove_profile

    composition = load_composition(clusters["tmp"] / "infra.py")
    edge = composition.cluster_named("edge-a")
    assert edge is not None and composition.cluster is not None
    remove_profile("edge-a")
    result = register_cluster(
        edge,
        kubeconfig=clusters["edge_kubeconfig"],
        context="edge",
        home=composition.cluster,
        transport="loopback-http",
    )
    assert result["state"] == "created" and result["profile"] == "edge-a"
    assert result["secret"]["name"] == "piceli-cluster-edge-a"
    assert TOKEN not in json.dumps(result)
    assert load_profile("edge-a").context == "edge"
    assert _stored(clusters["home"])["users"][0]["user"] == {"token": TOKEN}
