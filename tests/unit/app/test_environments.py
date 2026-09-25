"""Environments: typed overrides turn one App into dev, staging and prod."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from piceli import App, Resources
from piceli.app.environment import (
    Environment,
    EnvironmentInvalid,
    apply_environment,
    diff_text,
    environment_diff,
    field_changes,
)
from piceli.app.render import rendered
from piceli.k8s.ops.plan import DeploymentComponent, ResourceIntent

IMAGE = "registry.example/shop/api@sha256:" + "1" * 64
PROD_IMAGE = "registry.example/shop/api@sha256:" + "2" * 64


class CertSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    hosts: list[str]


def _shop() -> App:
    app = App("shop")
    settings = app.config("settings", {"LOG_LEVEL": "debug", "LEGACY": "on"})
    api = app.deployment(
        "api",
        image=IMAGE,
        ports=[8080],
        env={"LOG_LEVEL": settings.key("LOG_LEVEL")},
        node_selector={"kubernetes.io/arch": "amd64"},
    )
    app.service(api, port=8080)
    tools = app.config("tools-config", {"MODE": "debug"}, component="tools")
    app.deployment(
        "tools", image=IMAGE, env={"MODE": tools.key("MODE")}, component="tools"
    )
    app.resource("example.test/v1", "Cert", "shop-tls", CertSpec(hosts=["dev.shop"]))
    return app


def _objects(app: App, namespace: str = "shop") -> dict[tuple[str, str], dict]:
    return {
        (r.ref.kind, r.ref.name): r.manifest
        for component in app.render(namespace).components
        for r in component.resources
    }


def test_overrides_apply_to_a_new_app_and_the_declaring_app_is_unchanged():
    app = _shop()
    before = _objects(app)
    app.environment(
        "prod",
        replicas={"api": 3},
        images={"api": PROD_IMAGE},
        resources={"api": Resources(cpu="500m", memory="512Mi")},
        config={"settings": {"LOG_LEVEL": "warning", "LEGACY": None}},
        node_selector={"api": {"node-pool": "general"}},
        specs={"shop-tls": {"hosts": ["shop.example.com"]}},
        enabled={"tools": False},
    )
    prod = app.for_environment("prod")
    assert prod.selected_environment is not None
    assert prod.selected_environment.name == "prod"
    assert app.selected_environment is None
    assert _objects(app) == before  # the declaring app never changes
    objects = _objects(prod)
    deployment = objects[("Deployment", "api")]
    assert deployment["spec"]["replicas"] == 3
    pod = deployment["spec"]["template"]["spec"]
    assert pod["containers"][0]["image"] == PROD_IMAGE
    assert pod["containers"][0]["resources"] == {
        "requests": {"cpu": "500m", "memory": "512Mi"}
    }
    assert pod["nodeSelector"] == {
        "kubernetes.io/arch": "amd64",
        "node-pool": "general",
    }
    assert objects[("ConfigMap", "settings")]["data"] == {"LOG_LEVEL": "warning"}
    assert objects[("Cert", "shop-tls")]["spec"] == {"hosts": ["shop.example.com"]}
    # The disabled component is gone, whole.
    assert ("Deployment", "tools") not in objects
    assert ("ConfigMap", "tools-config") not in objects
    assert ("Service", "api") in objects


def test_a_typed_spec_override_is_validated_into_the_declared_model():
    app = _shop()
    with pytest.raises(EnvironmentInvalid, match=r"specs\['shop-tls'\]"):
        app.environment("prod", specs={"shop-tls": {"hosts": "not-a-list"}})

    class Other(BaseModel):
        hosts: list[str]

    with pytest.raises(EnvironmentInvalid, match="must be a CertSpec, got Other"):
        app.environment("prod", specs={"shop-tls": Other(hosts=[])})
    env = app.environment("prod", specs={"shop-tls": CertSpec(hosts=["a.example"])})
    spec = app.for_environment(env.name).objects[-1].spec  # type: ignore[union-attr]
    assert isinstance(spec, CertSpec)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"replicas": {"apii": 2}}, r"replicas\['apii'\] names no declared workload"),
        ({"replicas": {"settings": 2}}, "names no declared workload"),
        ({"config": {"api": {"A": "b"}}}, "names no declared config"),
        ({"specs": {"api": {}}}, "names no declared resource"),
        ({"hosts": {"api": ["a.example"]}}, "names no declared object with hosts"),
        ({"enabled": {"nope": False}}, "unknown components \\['nope'\\]"),
        ({"images": {"Service/api": IMAGE}}, "names no declared workload"),
    ],
)
def test_overrides_must_name_a_suitable_declared_object(overrides, message):
    with pytest.raises(EnvironmentInvalid, match=message) as error:
        _shop().environment("prod", **overrides)
    assert error.value.code == "environment-invalid"


def test_kind_qualified_keys_resolve_ambiguity():
    app = _shop()
    app.deployment("shop-tls", image=IMAGE)  # same name as the Cert
    app.environment("prod", replicas={"shop-tls": 2})  # only one has replicas
    app.environment("qa", replicas={"Deployment/shop-tls": 2})
    assert (
        _objects(app.for_environment("qa"))[("Deployment", "shop-tls")]["spec"][
            "replicas"
        ]
        == 2
    )


def test_values_are_type_checked_at_declaration():
    with pytest.raises(ValidationError):
        Environment("prod", replicas={"api": -1})
    with pytest.raises(ValidationError):
        Environment("prod", images={"api": "has space"})
    with pytest.raises(ValidationError):
        Environment("Prod")  # not a DNS label
    with pytest.raises(ValidationError):
        Environment("prod", surprise=1)  # type: ignore[call-arg]


def test_an_override_the_model_refuses_names_the_environment():
    app = _shop()
    app.deployment("pinned", image=IMAGE, node="primary")
    with pytest.raises(
        EnvironmentInvalid, match=r"environment 'prod': node_selector\['pinned'\]"
    ):
        app.environment(
            "prod", node_selector={"pinned": {"kubernetes.io/hostname": "node-a"}}
        )


def test_disabling_a_component_another_workload_reads_is_refused():
    app = _shop()
    with pytest.raises(EnvironmentInvalid, match="reads ConfigMap 'settings'"):
        app.environment("prod", enabled={"settings": False})


def test_disabled_components_drop_their_edges_extras_and_overrides():
    app = _shop()
    app.add(
        DeploymentComponent(
            "extra",
            (
                ResourceIntent.from_manifest(
                    {
                        "apiVersion": "v1",
                        "kind": "ConfigMap",
                        "metadata": {"name": "extra", "namespace": "shop"},
                    }
                ),
            ),
        )
    )
    tools = next(item for item in app.objects if item.name == "tools")
    app.override(tools, {"metadata": {"annotations": {"a": "b"}}})
    app.depends("api", on=["tools", "extra"])
    app.environment("lean", enabled={"tools": False, "extra": False})
    components = {
        c.name: c for c in app.for_environment("lean").render("shop").components
    }
    assert set(components) == {"api", "settings", "shop-tls"}
    assert components["api"].dependencies == ("settings",)


def test_environment_names_are_unique_and_must_be_declared():
    app = _shop()
    app.environment("prod")
    with pytest.raises(ValueError, match="already declared"):
        app.environment("prod")
    with pytest.raises(
        EnvironmentInvalid, match="declares no environment 'qa'"
    ) as error:
        app.for_environment("qa")
    assert error.value.code == "environment-unknown"
    with pytest.raises(EnvironmentInvalid, match="already environment 'prod'"):
        app.for_environment("prod").for_environment("prod")
    # An undeclared Environment object can still be applied explicitly.
    adhoc = app.for_environment(Environment("adhoc", replicas={"api": 5}))
    assert _objects(adhoc)[("Deployment", "api")]["spec"]["replicas"] == 5
    assert [env.name for env in app.environments] == ["prod"]


def test_environments_declared_before_their_objects_fail_early():
    app = App("shop")
    with pytest.raises(EnvironmentInvalid, match="names no declared workload"):
        app.environment("prod", replicas={"api": 2})


class Route(BaseModel):
    """A stand-in for a routed object with hosts (Ingress, HTTPRoute)."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    hosts: tuple[str, ...]
    component: str | None = None

    @property
    def component_name(self) -> str:
        return self.component or self.name


class SingleHost(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    host: str

    @property
    def component_name(self) -> str:
        return self.name


def test_hosts_apply_to_objects_with_hosts_or_a_host():
    app = App("shop")
    app._objects.extend(  # models from other declarations, applied directly
        [
            Route(name="web", hosts=("dev.example.com",)),
            SingleHost(name="api", host="x"),
        ]
    )
    env = Environment(
        "prod", hosts={"web": ["shop.example.com", "*.shop.example.com"], "api": "a.b"}
    )
    web, api = apply_environment(app, env)
    assert web.hosts == ("shop.example.com", "*.shop.example.com")  # type: ignore[union-attr]
    assert api.host == "a.b"  # type: ignore[union-attr]
    with pytest.raises(EnvironmentInvalid, match="takes exactly one host"):
        apply_environment(app, Environment("qa", hosts={"api": ["a.b", "c.d"]}))
    with pytest.raises(ValidationError):
        Environment("qa", hosts={"web": ["Not A Host"]})


def test_values_are_canonical_json_for_hashing():
    env = Environment(
        "prod",
        namespace="shop-prod",
        replicas={"b": 2, "a": 1},
        resources={"api": Resources(cpu="1")},
        specs={"shop-tls": CertSpec(hosts=["x.example"])},
        hosts={"web": "shop.example.com"},
    )
    assert env.values() == {
        "hosts": {"web": ["shop.example.com"]},
        "namespace": "shop-prod",
        "replicas": {"a": 1, "b": 2},
        "resources": {"api": {"cpu": "1"}},
        "specs": {"shop-tls": {"hosts": ["x.example"]}},
    }
    assert env.identity() == {"name": "prod", "values": env.values()}
    assert Environment("dev").values() == {}


def test_field_changes_match_named_list_items():
    a = {"spec": {"containers": [{"name": "api", "image": "a"}, {"name": "b"}]}}
    b = {"spec": {"containers": [{"name": "api", "image": "b"}], "x": 1}}
    assert field_changes(a, b) == [
        {"path": "spec.containers[api].image", "a": "a", "b": "b"},
        {"path": "spec.containers[b]", "a": {"name": "b"}},
        {"path": "spec.x", "b": 1},
    ]
    assert field_changes([1, 2], [1, 3]) == [{"path": "[1]", "a": 2, "b": 3}]
    assert field_changes([1], [1, 2]) == [{"path": ".", "a": [1], "b": [1, 2]}]


def test_environment_diff_is_typed_and_ignores_the_namespace():
    app = _shop()
    app.environment("dev", namespace="shop-dev", enabled={"tools": True})
    app.environment(
        "prod", namespace="shop-prod", replicas={"api": 3}, enabled={"tools": False}
    )
    sides = []
    for name in ("dev", "prod"):
        derived = app.for_environment(name)
        env = derived.selected_environment
        assert env is not None and env.namespace is not None
        sides.append(
            (name, env.namespace, env, rendered(derived.render(env.namespace), {}))
        )
    diff = environment_diff(sides[0], sides[1])
    assert diff["namespaces"] == {"a": "shop-dev", "b": "shop-prod"}
    assert {"path": "replicas.api", "b": 3} in diff["values"]
    assert {"path": "enabled.tools", "a": True, "b": False} in diff["values"]
    changes = {(o["kind"], o["name"]): o for o in diff["objects"]}
    assert changes[("Deployment", "api")]["fields"] == [
        {"path": "spec.replicas", "a": 1, "b": 3}
    ]
    assert changes[("Deployment", "tools")]["change"] == "only-a"
    assert diff["summary"] == {"changed": 1, "only-a": 2, "only-b": 0, "same": 3}
    text = diff_text(diff)
    assert "~ Deployment/api (component api):" in text
    assert "    spec.replicas: 1 -> 3" in text
    assert "- Deployment/tools (component tools): only in dev" in text
    assert text.endswith("1 changed, 2 only in dev, 0 only in prod, 3 identical\n")


def test_replicas_of_an_autoscaled_workload_are_refused() -> None:
    from piceli import App, Resources
    from piceli.app.environment import EnvironmentInvalid

    app = App("shop")
    api = app.deployment(
        "api",
        image="nginx@sha256:" + "a" * 64,
        resources=Resources(cpu="100m", memory="64Mi"),
    )
    app.autoscaler(api, max_replicas=5, cpu=70)
    with pytest.raises(EnvironmentInvalid, match="autoscaled"):
        app.environment("prod", replicas={"api": 3})


def test_autoscaler_bounds_are_typed_overrides() -> None:
    # Regression: the refusal above said "set the autoscaler's
    # min_replicas/max_replicas instead", but no override could.
    from piceli import App, Resources, Scaling
    from piceli.app.environment import EnvironmentInvalid

    app = App("shop")
    api = app.deployment(
        "api",
        image="nginx@sha256:" + "a" * 64,
        resources=Resources(cpu="100m", memory="64Mi"),
    )
    app.autoscaler(api, max_replicas=2, cpu=70)
    app.environment(
        "prod", autoscalers={"api": Scaling(min_replicas=3, max_replicas=10, cpu=60)}
    )
    app.environment("staging", autoscalers={"api": Scaling(max_replicas=4)})
    hpa = _objects(app.for_environment("prod"))[("HorizontalPodAutoscaler", "api")]
    assert (hpa["spec"]["minReplicas"], hpa["spec"]["maxReplicas"]) == (3, 10)
    metric = hpa["spec"]["metrics"][0]["resource"]["target"]
    assert metric["averageUtilization"] == 60
    staging = _objects(app.for_environment("staging"))
    hpa = staging[("HorizontalPodAutoscaler", "api")]
    assert (hpa["spec"]["minReplicas"], hpa["spec"]["maxReplicas"]) == (1, 4)
    assert app.environments[0].values()["autoscalers"] == {
        "api": {"cpu": 60, "max_replicas": 10, "min_replicas": 3}
    }
    with pytest.raises(EnvironmentInvalid, match="min_replicas cannot be above"):
        app.environment("bad", autoscalers={"api": Scaling(min_replicas=5)})
    with pytest.raises(EnvironmentInvalid, match="names no declared autoscaler"):
        app.environment("typo", autoscalers={"web": Scaling(max_replicas=3)})
    with pytest.raises(EnvironmentInvalid, match="autoscalers"):
        app.environment("replicas", replicas={"api": 3})


def test_resources_of_an_autoscaled_workload_keep_the_requests_it_needs() -> None:
    # Regression: an environment could drop the CPU request an autoscaler's
    # utilization target needs; app.autoscaler refuses that at declaration.
    from piceli import App, Resources
    from piceli.app.environment import EnvironmentInvalid

    app = App("shop")
    api = app.deployment(
        "api",
        image="nginx@sha256:" + "a" * 64,
        resources=Resources(cpu="100m", memory="64Mi"),
    )
    app.autoscaler(api, max_replicas=2, cpu=70)
    app.environment("prod", resources={"api": Resources(cpu="1", memory="1Gi")})
    with pytest.raises(EnvironmentInvalid, match="cpu request"):
        app.environment("dev", resources={"api": Resources(memory="64Mi")})


def test_environment_diff_ignores_the_namespace_inside_rbac_objects() -> None:
    # Regression: a RoleBinding's subject namespace and the namespaced names
    # of ClusterRoles showed as differences between two environments that
    # differ only in their namespace.
    from piceli import App, Rule

    app = App("shop")
    account = app.service_account(
        "watcher",
        rules=[Rule(resources=["pods"], verbs=["get"])],
        cluster_rules=[Rule(resources=["nodes"], verbs=["get"])],
    )
    app.deployment("watcher", image=IMAGE, service_account=account)
    app.environment("staging", namespace="shop-staging")
    app.environment("prod", namespace="shop-prod", replicas={"watcher": 2})
    sides = []
    for name in ("staging", "prod"):
        derived = app.for_environment(name)
        env = derived.selected_environment
        assert env is not None and env.namespace is not None
        sides.append(
            (name, env.namespace, env, rendered(derived.render(env.namespace), {}))
        )
    diff = environment_diff(sides[0], sides[1])
    assert [(o["kind"], o["change"]) for o in diff["objects"]] == [
        ("Deployment", "changed")
    ], diff["objects"]
    assert diff["summary"] == {"changed": 1, "only-a": 0, "only-b": 0, "same": 5}
