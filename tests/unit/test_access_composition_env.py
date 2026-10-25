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
    """0.14.7: the declared forwards (local port, path, health); 0.20: the
    other live Service ports only with --all-ports; a declared Service that
    is not live is left out."""
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
    # 0.20: only the declared forwards (no internal ports)...
    assert rows == [("web", "service/web", 80, 18080, "/login")]
    # ...and every live Service port with --all-ports, as before.
    every = composition_env_target(
        "infra.py:cluster", "main", tmp_path,
        services=lambda _t: [service("web", 80), service("cache", 6379)],
        port=lambda: next(free), all_ports=True,
    )  # fmt: skip
    assert [
        (item.id, item.target, item.remote_port, item.local_port, item.path)
        for item in every.shortcuts
    ] == [
        ("web", "service/web", 80, 18080, "/login"),
        ("cache-6379", "service/cache", 6379, 43000, "/"),
    ]
    assert resolved.shortcuts[0].probe.type == "http"
    gone = composition_env_target(
        "infra.py:cluster", "main", tmp_path, services=lambda _t: [], port=lambda: 1
    )
    assert gone.shortcuts == ()


def _companion(name: str, port: int, host: str | None) -> dict[str, Any]:
    raw = service(f"{name}-tailnet", port)
    raw["metadata"]["labels"] = {
        "piceli.io/access": "tailnet",
        "piceli.io/access-service": name,
    }
    if host:
        raw["status"] = {
            "loadBalancer": {"ingress": [{"hostname": host, "ip": "100.64.0.7"}]}
        }
    return raw


def test_tailnet_exposed_services_are_reached_on_the_tailnet(
    profile: Path, tmp_path: Path
) -> None:
    """0.19: a forward whose Service has a tailnet companion with a MagicDNS
    name gets its tailnet URL; one without a name yet is not ready."""
    (tmp_path / "infra.py").write_text(_PIPELINE_INFRA)
    free = iter(range(43000, 43010))
    live = [
        service("web", 80),
        service("cache", 6379),
        _companion("web", 80, "main-web.tail1.ts.net"),
    ]
    resolved = composition_env_target(
        "infra.py:cluster",
        "main",
        tmp_path,
        services=lambda _t: live,
        port=lambda: next(free),
    )
    assert resolved.tailnet == (
        {
            "id": "web",
            "label": "web",
            "scheme": "http",
            "web": True,
            "service": "web",
            "port": 80,
            "host": "main-web.tail1.ts.net",
            "ready": True,
            "url": "http://main-web.tail1.ts.net:80/login",
            "companion": "web-tailnet",
        },
    )
    pending = composition_env_target(
        "infra.py:cluster", "main", tmp_path,
        services=lambda _t: [service("web", 80), _companion("web", 80, None)], port=lambda: 1,
    )  # fmt: skip
    assert pending.tailnet[0]["ready"] is False and pending.tailnet[0]["url"] is None


def test_access_prints_tailnet_urls_instead_of_forwarding(
    profile: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import json

    import piceli.k8s.cli.access as cli
    from piceli.k8s.cli import app
    from piceli.k8s.env_access import composition_env_target as real

    (tmp_path / "infra.py").write_text(_PIPELINE_INFRA)
    live = [service("web", 80), _companion("web", 80, "main-web.tail1.ts.net")]
    monkeypatch.setattr(
        cli, "composition_env_target",
        lambda target, env, root, **_k: real(target, env, root, services=lambda _t: live, port=lambda: 43001),
    )  # fmt: skip
    monkeypatch.setattr(cli.shutil, "which", lambda _name: "/usr/bin/kubectl")
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        app, ["access", "main", "--cluster", "infra.py:cluster", "--json"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    started = json.loads(result.stdout.splitlines()[0])
    assert started["forwards"] == []
    assert started["tailnet"][0]["url"] == "http://main-web.tail1.ts.net:80/login"


def test_tailnet_notes_say_why_nothing_is_on_the_tailnet() -> None:
    """0.19 (P1): no companion, no operator, a companion without a name and
    the operator's latest event on it."""
    from piceli.envs.access import Access
    from piceli.k8s.env_access import tailnet_notes

    access = Access(tailnet=True)
    missing = tailnet_notes(
        access, [], [], {"deployments": [], "pods": [], "ingress_class": False}
    )
    assert [n["code"] for n in missing] == [
        "access-tailnet-missing",
        "access-operator-missing",
    ]
    pending = _companion("web", 80, None)
    pending["kind"] = "Service"
    events = [
        {"involvedObject": {"kind": "Service", "name": "web-tailnet"}, "reason": "Old",
         "message": "first", "lastTimestamp": "2026-10-12T08:00:00Z"},
        {"involvedObject": {"kind": "Service", "name": "web-tailnet"}, "reason": "ProxyError",
         "message": "requested tags [tag:k8s] are invalid or not permitted",
         "lastTimestamp": "2026-10-12T09:00:00Z"},
        {"involvedObject": {"kind": "Service", "name": "other"}, "reason": "X",
         "message": "not this one", "lastTimestamp": "2026-10-12T10:00:00Z"},
    ]  # fmt: skip
    crashing = {
        "deployments": [{"status": {"readyReplicas": 1}}],
        "pods": [{"metadata": {"name": "operator-7d9"}, "status": {"containerStatuses": [
            {"state": {"waiting": {"reason": "CrashLoopBackOff"}}}]}}],
        "ingress_class": True,
    }  # fmt: skip
    notes = tailnet_notes(access, [pending], events, crashing)
    assert [n["code"] for n in notes] == [
        "access-operator-not-ready",
        "access-tailnet-pending",
    ]
    assert "operator-7d9" in notes[0]["detail"] and "401" in notes[0]["detail"]
    assert notes[1]["companion"] == "web-tailnet"
    assert notes[1]["detail"].endswith(
        "ProxyError: requested tags [tag:k8s] are invalid or not permitted"
    )
    assert [n["code"] for n in tailnet_notes(access, [pending], None, None)] == [
        "access-operator-unknown", "access-tailnet-pending",
    ]  # fmt: skip
    ready = _companion("web", 80, "main-web.tail1.ts.net")
    assert tailnet_notes(access, [ready], [], None) == []


def test_an_https_companion_gives_an_https_url_without_a_port() -> None:
    from piceli.k8s.env_access import tailnet_urls
    from piceli.k8s.ui_config import UiShortcut

    ingress = {
        "kind": "Ingress",
        "metadata": {"name": "web-tailnet", "labels": {
            "piceli.io/access": "tailnet", "piceli.io/access-service": "web"}},
        "spec": {"defaultBackend": {"service": {"name": "web", "port": {"number": 80}}}},
        "status": {"loadBalancer": {"ingress": [{"hostname": "main-web.tail1.ts.net",
                                                  "ports": [{"port": 443}]}]}},
    }  # fmt: skip
    shortcut = UiShortcut(
        id="web", label="web", target="service/web", local_port=18080, remote_port=80,
        path="/login",
    )  # fmt: skip
    (found,) = tailnet_urls([shortcut], [ingress])
    assert found["url"] == "https://main-web.tail1.ts.net/login"


def test_access_prints_the_notes_when_the_tailnet_is_not_ready(
    profile: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import json

    import piceli.k8s.cli.access as cli
    from piceli.k8s.cli import app
    from piceli.k8s.env_access import composition_env_target as real

    infra = _PIPELINE_INFRA.replace(
        "from piceli.envs import Environment",
        "from piceli.envs import Access, Environment",
    ).replace(
        'follow={shop: "main"}),',
        'follow={shop: "main"}, access=Access(tailnet=True)),',
    )
    (tmp_path / "infra.py").write_text(infra)
    live = [service("web", 80), service("cache", 6379)]
    reads: list[str] = []

    def tailnet_reads(target: Any, access: Any, companions: Any) -> Any:
        reads.append(target.namespace)
        return [], [{"code": "access-tailnet-missing", "detail": "deploy again"}]

    monkeypatch.setattr(
        cli, "composition_env_target",
        lambda target, env, root, **_k: real(
            target, env, root, services=lambda _t: live, port=lambda: 43001,
            tailnet_reads=tailnet_reads,
        ),
    )  # fmt: skip
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        app,
        ["access", "main", "--cluster", "infra.py:cluster", "--json",
         "--kubectl", str(tmp_path / "no-kubectl")],
    )  # fmt: skip
    assert reads == ["shop-main"]
    assert "piceli: access-tailnet-missing: deploy again" in result.stderr
    assert json.loads(result.stdout)["reason"] == "access-kubectl-missing"
