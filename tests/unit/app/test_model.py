"""Every typed model validates at declaration time with a clear pydantic error."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from piceli import (
    App,
    Config,
    ConfigKey,
    ConfigVolume,
    Container,
    ContainerPort,
    Deployment,
    ExistingClaim,
    FieldRef,
    MemoryVolume,
    Mount,
    NetworkPolicy,
    Probe,
    Resources,
    Secret,
    SecretKey,
    SecretVolume,
    Service,
    ServicePort,
)
from piceli.app.render import placeholder_inputs

REF = placeholder_inputs(["token"])["token"]


def _error(excinfo: pytest.ExceptionInfo[ValidationError]) -> str:
    return str(excinfo.value)


# ------------------------------------------------------------------- probes


def test_probe_factories_render():
    assert Probe.http("/healthz", 8080).manifest() == {
        "httpGet": {"path": "/healthz", "port": 8080}
    }
    assert Probe.http("/", "http", scheme="HTTPS", period_seconds=5).manifest() == {
        "httpGet": {"path": "/", "port": "http", "scheme": "HTTPS"},
        "periodSeconds": 5,
    }
    assert Probe.tcp(6379, failure_threshold=3).manifest() == {
        "tcpSocket": {"port": 6379},
        "failureThreshold": 3,
    }
    assert Probe.exec(["true"], initial_delay_seconds=0).manifest() == {
        "exec": {"command": ["true"]},
        "initialDelaySeconds": 0,
    }
    assert App.probe is Probe


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (lambda: Probe.http("healthz", 80), "path starting '/'"),
        (lambda: Probe.tcp(70000), "less than or equal to 65535"),
        (lambda: Probe.exec([]), "non-empty command"),
        (lambda: Probe(action="tcp", port=80, path="/x"), "only a port"),
        (lambda: Probe(action="exec", command=("x",), port=1), "only a command"),
        (lambda: Probe.tcp(80, period_seconds=0), "greater than 0"),
        (lambda: Probe.tcp(80, every=3), "Extra inputs are not permitted"),
        (lambda: Probe.tcp("Bad_Name"), "port names"),
    ],
)
def test_probe_validation(build, message):
    with pytest.raises(ValidationError) as excinfo:
        build()
    assert message in _error(excinfo)


# ---------------------------------------------------------------- resources


def test_resources_render_only_set_values():
    assert Resources().manifest() == {}
    assert Resources(cpu="100m", memory_limit="1Gi").manifest() == {
        "requests": {"cpu": "100m"},
        "limits": {"memory": "1Gi"},
    }
    assert Resources(ephemeral_storage="1Gi").manifest() == {
        "requests": {"ephemeral-storage": "1Gi"}
    }


@pytest.mark.parametrize("value", ["lots", "1GB", "-1", ""])
def test_resources_reject_invalid_quantities(value):
    with pytest.raises(ValidationError) as excinfo:
        Resources(memory=value)
    assert "memory" in _error(excinfo)


# ---------------------------------------------------------------------- env


def test_env_references_render():
    assert SecretKey(secret="s", key="k").manifest() == {
        "secretKeyRef": {"name": "s", "key": "k"}
    }
    assert ConfigKey(config="c", key="k", optional=True).manifest() == {
        "configMapKeyRef": {"name": "c", "key": "k", "optional": True}
    }
    assert FieldRef(field_path="status.podIP").manifest() == {
        "fieldRef": {"fieldPath": "status.podIP"}
    }


def test_env_rejects_raw_secret_versions_with_guidance():
    with pytest.raises(ValidationError) as excinfo:
        Container(name="api", image="api:1", env={"TOKEN": REF})
    assert "secret.key(key)" in _error(excinfo)


def test_env_rejects_invalid_names():
    with pytest.raises(ValidationError):
        Container(name="api", image="api:1", env={"1 BAD": "x"})


# ---------------------------------------------------------- config / secret


def test_config_and_secret_keys_are_checked():
    config = Config(name="settings", data={"mode": "blue"})
    assert config.key("mode") == ConfigKey(config="settings", key="mode")
    with pytest.raises(ValueError, match="has no key 'colour'"):
        config.key("colour")
    secret = Secret(name="creds", data={"token": REF})
    assert secret.key("token") == SecretKey(secret="creds", key="token")
    with pytest.raises(ValueError, match="keys: \\['token'\\]"):
        secret.key("password")
    assert secret.component_name == "creds"
    assert Secret(name="c", data={"t": REF}, component="cfg").component_name == "cfg"


def test_secret_values_must_be_opaque_versions():
    with pytest.raises(ValidationError) as excinfo:
        Secret(name="creds", data={"token": "hunter2"})
    assert "ctx.secret(...)" in _error(excinfo)
    with pytest.raises(ValidationError):
        Secret(name="creds", data={})
    with pytest.raises(ValidationError):
        Config(name="Bad_Name", data={})


# ------------------------------------------------------------------ volumes


def test_volume_sources_and_default_names():
    config = Config(name="web.config", data={"a": "b"})
    assert ConfigVolume(config).volume_name("/etc/x") == "web-config"
    assert ConfigVolume("c", items={"a": "a.conf"}, default_mode=0o444).source() == {
        "configMap": {
            "name": "c",
            "items": [{"key": "a", "path": "a.conf"}],
            "defaultMode": 0o444,
        }
    }
    assert SecretVolume("s", name="tls").source() == {"secret": {"secretName": "s"}}
    assert SecretVolume("s").volume_name("/x") == "s"
    assert MemoryVolume().volume_name("/run/cache") == "run-cache"
    assert MemoryVolume(size_limit="1Mi").source() == {
        "emptyDir": {"medium": "Memory", "sizeLimit": "1Mi"}
    }
    assert ExistingClaim("state").source() == {
        "persistentVolumeClaim": {"claimName": "state"}
    }
    assert ExistingClaim("state", read_only=True).source() == {
        "persistentVolumeClaim": {"claimName": "state", "readOnly": True}
    }


@pytest.mark.parametrize(
    "build",
    [
        lambda: ExistingClaim("Bad Claim"),
        lambda: MemoryVolume(size_limit="big"),
        lambda: ConfigVolume("c", default_mode=0o1000),
        lambda: SecretVolume("s", name="UPPER"),
        lambda: ExistingClaim("state", size="1Gi"),
    ],
)
def test_volume_validation(build):
    with pytest.raises(ValidationError):
        build()


def test_mounts_render_options_and_read_only_claims():
    container = Container(
        name="db",
        image="db:1",
        volumes={
            "/data": ExistingClaim("state", read_only=True),
            "/etc/app.conf": Mount(
                ConfigVolume("c"), sub_path="app.conf", read_only=True
            ),
        },
    )
    assert container.manifest()["volumeMounts"] == [
        {"name": "state", "mountPath": "/data", "readOnly": True},
        {
            "name": "c",
            "mountPath": "/etc/app.conf",
            "readOnly": True,
            "subPath": "app.conf",
        },
    ]
    with pytest.raises(ValidationError):
        Container(name="db", image="db:1", volumes={"relative": MemoryVolume()})


# --------------------------------------------------------------- containers


def test_container_renders_every_field():
    container = Container(
        name="api",
        image="api@sha256:" + "1" * 64,
        command=["serve"],
        args=["--port", "8080"],
        working_dir="/srv",
        env={"MODE": "blue", "IP": FieldRef(field_path="status.podIP")},
        ports=[8080, ContainerPort(port=9090, name="metrics", protocol="TCP")],
        ready=Probe.http("/ready", 8080),
        live=Probe.tcp(8080),
        startup=Probe.exec(["check"]),
        resources=Resources(cpu="1"),
        pull_policy="IfNotPresent",
    )
    assert container.manifest() == {
        "name": "api",
        "image": "api@sha256:" + "1" * 64,
        "imagePullPolicy": "IfNotPresent",
        "command": ["serve"],
        "args": ["--port", "8080"],
        "workingDir": "/srv",
        "ports": [
            {"containerPort": 8080},
            {"containerPort": 9090, "name": "metrics", "protocol": "TCP"},
        ],
        "env": [
            {"name": "MODE", "value": "blue"},
            {"name": "IP", "valueFrom": {"fieldRef": {"fieldPath": "status.podIP"}}},
        ],
        "resources": {"requests": {"cpu": "1"}},
        "readinessProbe": {"httpGet": {"path": "/ready", "port": 8080}},
        "livenessProbe": {"tcpSocket": {"port": 8080}},
        "startupProbe": {"exec": {"command": ["check"]}},
    }


@pytest.mark.parametrize(
    "fields",
    [
        {"name": "API", "image": "x"},
        {"name": "api", "image": ""},
        {"name": "api", "image": "has space"},
        {"name": "api", "image": "x", "ports": [0]},
        {"name": "api", "image": "x", "pull_policy": "Sometimes"},
        {"name": "api", "image": "x", "cmd": ["a"]},
    ],
)
def test_container_validation(fields):
    with pytest.raises(ValidationError):
        Container(**fields)


def test_container_is_frozen():
    container = Container(name="api", image="x")
    with pytest.raises(ValidationError):
        container.image = "y"  # type: ignore[misc]


# ---------------------------------------------------------------- workloads


def _main(**fields):
    return Container(name="main", image="main:1", **fields)


def test_deployment_selector_rule():
    deployment = Deployment(name="web", containers=(_main(),), component="front")
    assert deployment.selector_labels == {"app.kubernetes.io/name": "web"}
    explicit = Deployment(name="web", containers=(_main(),), selector={"app": "web"})
    assert explicit.selector_labels == {"app": "web"}


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"containers": ()}, "at least 1 item"),
        (
            {"containers": (_main(), Container(name="main", image="x"))},
            "container names must be unique",
        ),
        (
            {
                "init_containers": (_main(ready=Probe.tcp(1)),),
                "containers": (Container(name="b", image="x"),),
            },
            "cannot have probes",
        ),
        ({"containers": (_main(),), "replicas": -1}, "greater than or equal to 0"),
        ({"containers": (_main(),), "selector": {}}, "at least 1 item"),
        (
            {"containers": (_main(),), "labels": {"app.kubernetes.io/name": "other"}},
            "conflicts with the selector",
        ),
        ({"containers": (_main(),), "labels": {"bad key!": "x"}}, "label key"),
        ({"containers": (_main(),), "strategy": "BlueGreen"}, "strategy"),
        (
            {
                "containers": (
                    _main(volumes={"/a": MemoryVolume(name="v")}),
                    Container(
                        name="b",
                        image="x",
                        volumes={"/b": ExistingClaim("x", name="v")},
                    ),
                )
            },
            "used for two different volumes",
        ),
    ],
)
def test_deployment_validation(fields, message):
    with pytest.raises(ValidationError) as excinfo:
        Deployment(name="web", **fields)
    assert message in _error(excinfo)


def test_service_and_network_policy_validation():
    with pytest.raises(ValidationError) as excinfo:
        Service(
            name="web",
            selector={"a": "b"},
            ports=(ServicePort(port=80), ServicePort(port=81)),
            component="web",
        )
    assert "needs a name on each" in _error(excinfo)
    with pytest.raises(ValidationError):
        ServicePort(port=80, name="Http")
    with pytest.raises(ValidationError):
        NetworkPolicy(name="deny", pod_selector={}, component="web")
    policy = NetworkPolicy(name="deny", pod_selector={"a": "b"}, component="web")
    assert policy.manifest("ns", {})["spec"]["ingress"] == []


def test_app_validation():
    with pytest.raises(ValidationError):
        App("Shop")
    with pytest.raises(ValidationError):
        App("shop", labels={"bad key!": "x"})
    with pytest.raises(ValidationError):
        App("shop", colour="blue")
    assert App("shop").object_labels == {"app.kubernetes.io/part-of": "shop"}
    assert App("shop", labels={}).object_labels == {}
