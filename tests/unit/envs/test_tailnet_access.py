"""``Access(tailnet=True)``: an environment's Services on the Tailscale tailnet."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from piceli import App, Branch, EnvConfig, Pipeline, Target
from piceli.envs import Access, EnvError, Environment, env_pipeline
from piceli.pipeline.compose import offline_composition

WEB = "example/web@sha256:" + "1" * 64
DB = "example/db@sha256:" + "2" * 64


def _app(*, policy: bool = True) -> App:
    app = App("shop")
    web = app.deployment("web", image=WEB, ports=[3000])
    db = app.deployment("db", image=DB, ports=[5432])
    app.service(web, port=3000, access=app.access.forward(local=18080, path="/login"))
    app.service(db, port=5432)
    if policy:
        app.network_policy(web, allow_from=[db], ports=[3000])
    return app


def _objects(
    access: Access | None, *, policy: bool = True, **env: Any
) -> dict[tuple[str, str], dict[str, Any]]:
    pipeline = Pipeline(
        _app(policy=policy),
        Target(Path("/nonexistent/kc"), context="c", namespace="shop"),
        state_dir=Path("/nonexistent/state"),
        envs=EnvConfig(
            prefix="shop-",
            environments=[
                Environment(
                    "main",
                    namespace="shop-main",
                    follow=Branch("main"),
                    access=access,
                    **env,
                )
            ],
        ),
    )
    _, composition = offline_composition(env_pipeline(pipeline, "main"))
    return {
        (r.ref.kind, r.ref.name): dict(r.manifest)
        for c in composition.components
        for r in c.resources
    }


def test_a_declared_forward_gets_a_tailnet_service() -> None:
    found = _objects(Access(tailnet=True, hostname="shop-{env}-{service}"))
    companion = found[("Service", "web-tailnet")]
    assert companion["spec"]["type"] == "LoadBalancer"
    assert companion["spec"]["loadBalancerClass"] == "tailscale"
    assert (
        companion["metadata"]["annotations"]["tailscale.com/hostname"]
        == "shop-main-web"
    )
    assert companion["metadata"]["labels"]["piceli.io/access"] == "tailnet"
    assert (
        companion["spec"]["selector"] == found[("Service", "web")]["spec"]["selector"]
    )
    assert [p["port"] for p in companion["spec"]["ports"]] == [3000]
    assert ("Service", "db-tailnet") not in found  # no forward declared for db


def test_the_policy_that_selects_the_pods_lets_the_operator_in() -> None:
    found = _objects(Access(tailnet=True))
    (policy,) = [m for (kind, _), m in found.items() if kind == "NetworkPolicy"]
    rules = policy["spec"]["ingress"]
    assert {
        "namespaceSelector": {
            "matchLabels": {"kubernetes.io/metadata.name": "tailscale"}
        }
    } in rules[-1]["from"]
    assert rules[-1]["ports"] == [{"port": 3000, "protocol": "TCP"}]
    assert len(rules) == 2  # the app's own rule is kept


def test_no_policy_is_added_where_the_app_has_none() -> None:
    found = _objects(Access(tailnet=True), policy=False)
    assert not [key for key in found if key[0] == "NetworkPolicy"]
    assert ("Service", "web-tailnet") in found


def test_named_services_expose_every_port_and_access_off_adds_nothing() -> None:
    found = _objects(Access(tailnet=True, services=["db"]))
    assert ("Service", "db-tailnet") in found and (
        "Service",
        "web-tailnet",
    ) not in found
    plain = _objects(None)
    assert not [key for key in plain if key[1].endswith("-tailnet")]


def test_the_cluster_access_is_the_environment_default() -> None:
    from piceli.infra import Cluster

    cluster = Cluster(
        "c", api="https://10.0.0.1:6443", credentials="c", access=Access(tailnet=True)
    )
    found = _objects(None, cluster=cluster)
    assert ("Service", "web-tailnet") in found


@pytest.mark.parametrize(
    "kwargs",
    [
        {"hostname": "Bad Name"},
        {"services": ["Not_A_Service"]},
        {"operator_namespace": "x_y"},
        {"tailnet": "yes"},
    ],
)
def test_bad_access_is_refused(kwargs: dict[str, Any]) -> None:
    with pytest.raises(EnvError) as raised:
        Access(**kwargs)
    assert raised.value.code == "env-config-invalid"


def test_hostnames_are_dns_labels() -> None:
    access = Access(tailnet=True, hostname="{namespace}-{service}")
    assert access.host("main", "Web_App", "shop-main") == "shop-main-web-app"
    assert len(Access(tailnet=True).host("e" * 40, "s" * 40, "n")) <= 63


def test_the_plan_shows_what_goes_on_the_tailnet_and_why_not_the_rest() -> None:
    """0.19 (P1): the plan stage's ``access`` (not hashed)."""
    from piceli.pipeline.compose import tailnet_report

    pipeline = Pipeline(
        _app(),
        Target(Path("/nonexistent/kc"), context="c", namespace="shop"),
        state_dir=Path("/nonexistent/state"),
        envs=EnvConfig(
            prefix="shop-",
            environments=[
                Environment(
                    "main", namespace="shop-main", follow=Branch("main"),
                    access=Access(tailnet=True, services=["web", "nope"]),
                )
            ],
        ),
    )  # fmt: skip
    report = tailnet_report(env_pipeline(pipeline, "main"))
    assert report is not None
    assert report["tailnet"] == [
        {
            "kind": "Service",
            "name": "web-tailnet",
            "service": "web",
            "host": "main-web",
            "ports": [3000],
            "scheme": "http",
        }
    ]
    assert len(report["policies"]) == 1  # the app's policy in front of web
    assert [(n["code"], n["service"], n["why"]) for n in report["notes"]] == [
        ("access-tailnet-skipped", "nope", "not-found")
    ]
    plain = Pipeline(
        _app(), Target(Path("/nonexistent/kc"), context="c", namespace="shop"),
        state_dir=Path("/nonexistent/state"),
        envs=EnvConfig(prefix="shop-", environments=[
            Environment("main", namespace="shop-main", follow=Branch("main"))
        ]),
    )  # fmt: skip
    assert tailnet_report(env_pipeline(plain, "main")) is None


def test_the_controllers_environment_keeps_its_access() -> None:
    """0.19.1: env_config_of (how the GitOps controller deploys a composition
    environment) dropped Access, so no companion was ever rendered there."""
    from types import SimpleNamespace

    from piceli.envs import Branches
    from piceli.infra import Cluster
    from piceli.infra.pipelines import env_config_of

    composition = SimpleNamespace(name="shop")
    own = Access(tailnet=True, hostname="own-{service}")
    shared = Access(tailnet=True)
    cluster = Cluster("c", api="https://10.0.0.1:6443", credentials="c", access=shared)
    named = Environment(
        "main", namespace="shop-main", follow=Branch("main"), access=own
    )
    assert env_config_of(composition, named).environments[0].access is own
    inherited = Environment(
        "rc", namespace="shop-rc", follow=Branch("main"), cluster=cluster
    )
    assert env_config_of(composition, inherited).environments[0].access is shared
    from piceli.infra import Source

    shop = Source("https://example.com/shop.git", name="shop")
    branches = Environment.per_branch(
        Branches("wp-*", fallback="main"),
        namespace="shop-{branch}",
        cluster=cluster,
        follow={shop: "{branch}"},
    )
    assert env_config_of(composition, branches).branch_access is shared
