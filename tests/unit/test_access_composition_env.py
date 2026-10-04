"""``piceli access ENV --cluster infra.py:CLUSTER``: a composition environment's forwards."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.k8s.access import AccessTargetError
from piceli.k8s.env_access import composition_env_target, service_forwards
from piceli.profiles import save_profile

ROOT = Path(__file__).resolve().parents[2]
CLUSTER = "examples/composition/infra.py:cluster"


def service(name: str, *ports: int) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": "shop-main", "uid": f"uid-{name}"},
        "spec": {"ports": [{"port": port} for port in ports]},
    }


@pytest.fixture
def profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("PICELI_PROFILES_DIR", str(tmp_path / "profiles"))
    monkeypatch.delenv("PICELI_IN_CLUSTER", raising=False)
    monkeypatch.setenv("PICELI_SERVICE_ACCOUNT_DIR", str(tmp_path / "no-sa"))
    kubeconfig = tmp_path / "owner.kubeconfig"
    kubeconfig.write_text(
        "apiVersion: v1\nkind: Config\n"
        "clusters: [{name: c, cluster: {server: 'https://192.0.2.1'}}]\n"
        "users: [{name: u, user: {}}]\n"
        "contexts: [{name: owner, context: {cluster: c, user: u}}]\n"
    )
    save_profile("my-cluster", kubeconfig, "owner")
    return kubeconfig


def test_environment_forwards_come_from_its_services_with_the_cluster_profile(
    profile: Path,
) -> None:
    seen: list[tuple[Path, str, str]] = []

    def services(target: Any) -> list[dict[str, Any]]:
        seen.append((target.kubeconfig, target.context, target.namespace))
        return [service("web", 80, 8080), service("cache", 6379)]

    free = iter(range(41000, 41010))
    resolved = composition_env_target(
        CLUSTER, "main", ROOT, services=services, port=lambda: next(free)
    )
    assert seen == [(profile, "owner", "shop-main")]
    assert (resolved.kubeconfig, resolved.context, resolved.namespace) == (
        profile,
        "owner",
        "shop-main",
    )
    assert resolved.name == "shop environment main"
    assert [
        (item.id, item.target, item.remote_port, item.local_port)
        for item in resolved.shortcuts
    ] == [
        ("cache-6379", "service/cache", 6379, 41000),
        ("web-80", "service/web", 80, 41001),
        ("web-8080", "service/web", 8080, 41002),
    ]
    assert all(item.namespace == "shop-main" for item in resolved.shortcuts)


def test_unknown_environment_and_non_cluster_are_refused(profile: Path) -> None:
    with pytest.raises(AccessTargetError) as unknown:
        composition_env_target(CLUSTER, "nope", ROOT, services=lambda _t: [])
    assert unknown.value.code == "env-not-found"
    with pytest.raises(AccessTargetError) as invalid:
        composition_env_target(
            "examples/composition/infra.py:shop", "main", ROOT, services=lambda _t: []
        )
    assert invalid.value.code == "access-target-invalid"


def test_service_forwards_skip_services_without_ports() -> None:
    shortcuts = service_forwards(
        "shop-main", [service("api", 8080), service("headless")], port=lambda: 42000
    )
    assert [item.id for item in shortcuts] == ["api-8080"]


def test_cli_accepts_an_environment_with_cluster(
    profile: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import piceli.k8s.cli.access as cli
    from piceli.k8s.cli import app

    def fake(target: str, env: str, root: Path, **_kwargs: Any) -> Any:
        raise AccessTargetError("access-none-declared", f"{env} via {target}")

    monkeypatch.setattr(cli, "composition_env_target", fake)
    monkeypatch.chdir(ROOT)
    result = CliRunner().invoke(app, ["access", "main", "--cluster", CLUSTER, "--json"])
    assert result.exit_code == 2
    assert '"reason": "access-none-declared"' in result.stdout


_PIPELINE_INFRA = """from piceli import App, Pipeline, Target
from piceli.envs import Environment
from piceli.infra import Cluster, Controller, Source
from piceli.pipeline.model import Registry

name = "shop"
cluster = Cluster(
    "my-cluster",
    api="https://127.0.0.1:6443",
    credentials="my-cluster",
    registry=Registry.in_cluster(on="node-a"),
    controller=Controller(on="node-a"),
)
shop = Source("https://example.com/shop.git", name="shop")
app = App("shop")
web = app.deployment("web", image="example/web@sha256:" + "1" * 64, ports=[8080])
app.service(web, port=80, access=app.access.forward(local=18080, path="/login", health="/"))
cache = app.deployment("cache", image="example/cache@sha256:" + "2" * 64, ports=[6379])
app.service(cache, port=6379)
pipeline = Pipeline(app, Target("owner.kubeconfig", context="owner", namespace="shop"))
environments = [
    Environment("main", namespace="shop-main", pipeline=pipeline, cluster=cluster,
                follow={shop: "main"}),
]
"""


def test_a_pipeline_environment_forwards_what_its_app_declares(
    profile: Path, tmp_path: Path
) -> None:
    """0.14.7: the declared forwards (local port, path, health) come first;
    the other live Service ports get free ports; a declared Service that is not
    live is left out."""
    (tmp_path / "infra.py").write_text(_PIPELINE_INFRA)
    free = iter(range(43000, 43010))
    resolved = composition_env_target(
        "infra.py:cluster",
        "main",
        tmp_path,
        services=lambda _t: [service("web", 80), service("cache", 6379)],
        port=lambda: next(free),
    )
    rows = [
        (item.id, item.target, item.remote_port, item.local_port, item.path)
        for item in resolved.shortcuts
    ]
    assert rows == [
        ("web", "service/web", 80, 18080, "/login"),
        ("cache-6379", "service/cache", 6379, 43000, "/"),
    ]
    assert resolved.shortcuts[0].probe.type == "http"
    gone = composition_env_target(
        "infra.py:cluster", "main", tmp_path, services=lambda _t: [], port=lambda: 1
    )
    assert gone.shortcuts == ()
