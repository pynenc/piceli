"""Compositions: the module, the follow mapping, the branch rule, the rendering."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from piceli.envs import Branch, Branches, EnvError, Environment, Stack, Tag
from piceli.envs.ops import env_pipeline
from piceli.infra import Cluster, Component, CompositionError, Source
from piceli.infra.composition import Composition, composition_from, load_composition
from piceli.infra.contract import image_contract, parse_contracts
from piceli.infra.render import check_needs, environment_pipeline
from piceli.pipeline.compose import offline_composition

ROOT = Path(__file__).resolve().parents[3]
EXAMPLE = ROOT / "examples" / "composition"
PIN = "sha256:" + "b" * 64


def _example() -> Composition:
    return load_composition(EXAMPLE / "infra.py")


def _contracts(composition: Composition) -> dict[str, Any]:
    found = parse_contracts((EXAMPLE / "shop" / "piceli.toml").read_text())
    found.update(parse_contracts((EXAMPLE / "catalog" / "piceli.toml").read_text()))
    cache = composition.component("cache")
    found["cache"] = image_contract("cache", cache.options["contract"])  # type: ignore[arg-type]
    return found


def _images(names: Any) -> dict[str, str]:
    return {n: f"piceli-registry.piceli-system.svc:5000/shop/{n}@{PIN}" for n in names}


def _objects(pipeline: Any) -> dict[tuple[str, str], dict[str, Any]]:
    _, composition = offline_composition(pipeline)
    return {
        (r.ref.kind, r.ref.name): r.manifest
        for c in composition.components
        for r in c.resources
    }


def test_the_example_loads_and_round_trips() -> None:
    composition = _example()
    assert [e.name for e in composition.environments] == ["main", "rc", "branches"]
    assert [s.key for s in composition.sources] == ["catalog", "shop"]
    assert {c.name for c in composition.components} == {
        "web",
        "api",
        "catalog",
        "cache",
    }
    data = composition.to_dict()
    assert Composition.from_dict(data).to_dict() == data
    assert "credentials" not in str(data["cluster"])


def test_follow_maps_sources_to_rules() -> None:
    shop = Source("https://example.com/shop.git")
    assert shop.key == "shop"
    env = Environment("main", namespace="m", follow={shop: "main"})
    assert env.follow == (Branch("main"),)
    assert env.sources == ((shop, (Branch("main"),)),)
    assert env.describe()["sources"] == {"shop": [{"kind": "branch", "branch": "main"}]}
    # A single follow keeps its 0.14 meaning and description.
    plain = Environment("main", namespace="m", follow=Branch("main"))
    assert "sources" not in plain.describe() and plain.sources == ()
    with pytest.raises(EnvError):
        Environment("main", namespace="m", follow={"shop": "main"})
    with pytest.raises(EnvError):
        Environment("main", namespace="m", follow={shop: "{branch}"})


def test_per_branch_builds_the_branch_rule() -> None:
    shop = Source("https://example.com/shop.git")
    other = Source("git@example.com:team/other.git")
    rule = Environment.per_branch(
        Branches("wp-*"),
        namespace="app-{branch}",
        follow={shop: "{branch}", other: Tag("v*")},
        on_nodes=["node-b"],
        limit=2,
        idle_stop="24h",
    )
    config = rule.env_config()
    assert config.prefix == "app-" and config.max_envs == 2
    assert config.branch_nodes == ("node-b",) and config.idle_stop_seconds == 86400
    assert config.namespace_for("wp-login") == "app-wp-login"
    for bad in ("app-{branch}-x", "app"):
        with pytest.raises(EnvError):
            Environment.per_branch("wp-*", namespace=bad, follow={shop: "{branch}"})
    with pytest.raises(EnvError):
        Environment.per_branch("wp-*", namespace="app-{branch}", follow={shop: "main"})


def test_stacks_hold_components() -> None:
    shop = Source("https://example.com/shop.git")
    web = Component("web", source=shop)
    stack = Stack("small", [web, "db"])
    assert stack.workloads == ("web", "db")
    assert stack.components == [web]
    assert stack.describe() == {"name": "small", "workloads": ["web", "db"]}


@pytest.mark.parametrize(
    "case",
    [
        "no-envs",
        "unfollowed-source",
        "two-sources-one-name",
        "two-clusters",
        "image-pin",
    ],
)
def test_invalid_compositions_are_refused(case: str) -> None:
    shop = Source("https://example.com/shop.git")
    other = Source("https://example.com/other.git")
    web = Component("web", source=shop)
    api = Component("api", source=other)
    module: dict[str, Any] = {"web": web}
    if case == "no-envs":
        module["environments"] = []
    elif case == "unfollowed-source":
        module["api"] = api
        module["environments"] = [
            Environment("main", namespace="m", follow={shop: "main"})
        ]
    elif case == "two-sources-one-name":
        twin = Source("https://example.com/elsewhere/shop.git")
        module["api"] = Component("api", source=twin)
        module["environments"] = [
            Environment("main", namespace="m", follow={shop: "main"})
        ]
    elif case == "two-clusters":
        one = Cluster("a", api="https://a:6443", credentials="a")
        two = Cluster("b", api="https://b:6443", credentials="b")
        module["c1"], module["c2"] = one, two
        module["environments"] = [
            Environment("main", namespace="m", follow={shop: "main"})
        ]
    else:
        with pytest.raises(CompositionError):
            Component.image("redis:7.2", pin="sha256:a7cee7c8...")
        return
    with pytest.raises((CompositionError, EnvError)):
        composition_from(module)


def test_main_renders_every_component(tmp_path: Path) -> None:
    composition = _example()
    main = composition.environment("main")
    assert main is not None
    contracts = _contracts(composition)
    pipeline = environment_pipeline(
        composition,
        main,
        contracts,
        _images(contracts),
        kubeconfig=tmp_path / "kc",
        context="c",
        state_dir=tmp_path / "state",
    )
    objects = _objects(env_pipeline(pipeline, "main"))
    web = objects[("Deployment", "web")]
    container = web["spec"]["template"]["spec"]["containers"][0]
    assert container["image"].endswith(f"/shop/web@{PIN}")
    env = {item["name"]: item["value"] for item in container["env"]}
    assert env["GREETING"] == "hello from the composition"  # composition wins
    assert container["readinessProbe"]["httpGet"] == {
        "path": "/index.html",
        "port": 8080,
    }
    assert objects[("Service", "web")]["spec"]["ports"][0]["port"] == 8080
    catalog = objects[("StatefulSet", "catalog")]
    template = catalog["spec"]["volumeClaimTemplates"][0]
    assert template["metadata"]["name"] == "data"
    assert template["spec"]["resources"]["requests"]["storage"] == "64Mi"
    cache = objects[("Deployment", "cache")]["spec"]["template"]["spec"]["containers"][
        0
    ]
    assert cache["command"][:2] == ["httpd", "-f"]
    assert ("Deployment", "api") in objects
    assert (
        pipeline.app.pre_rollouts and pipeline.app.pre_rollouts[0].workload == "catalog"
    )


def test_branch_envs_run_their_stack_on_their_nodes(tmp_path: Path) -> None:
    composition = _example()
    rule = composition.branch_rule
    assert rule is not None
    contracts = _contracts(composition)
    small = {name: contracts[name] for name in ("web", "catalog")}
    pipeline = environment_pipeline(
        composition,
        rule,
        small,
        _images(small),
        kubeconfig=tmp_path / "kc",
        context="c",
        state_dir=tmp_path / "state",
    )
    derived = env_pipeline(pipeline, "wp-login")
    assert derived.target.namespace == "shop-wp-login"
    objects = _objects(derived)
    assert ("Deployment", "api") not in objects
    pod = objects[("Deployment", "web")]["spec"]["template"]["spec"]
    terms = pod["affinity"]["nodeAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"
    ]
    assert "my-cluster-worker2" in str(terms)


def test_unmet_needs_fail_the_plan() -> None:
    composition = _example()
    main = composition.environment("main")
    assert main is not None
    contracts = _contracts(composition)
    broken = parse_contracts(
        (EXAMPLE / "shop" / "piceli.toml")
        .read_text()
        .replace('needs = ["catalog"]', 'needs = ["catalog", "secret:api-token"]')
    )
    with pytest.raises(CompositionError) as raised:
        check_needs(composition, main, {**contracts, "api": broken["api"]})
    assert raised.value.code == "component-need-unmet"
    rule = composition.branch_rule
    assert rule is not None
    # web needs catalog (in the small stack) and cache? (optional): met.
    check_needs(composition, rule, {k: contracts[k] for k in ("web", "catalog")})
