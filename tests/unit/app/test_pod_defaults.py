"""App-level pod defaults: security, node selector and grace period on every workload."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from piceli import App, Container, PodDefaults, Security
from piceli.k8s.release_spec import NodeRef

NODES = {"primary": NodeRef("node-a", "uid-a")}


def _pod(composition, name: str) -> dict:
    for component in composition.components:
        for resource in component.resources:
            if (resource.ref.kind, resource.ref.name) == ("Deployment", name):
                return resource.manifest["spec"]["template"]["spec"]
    raise AssertionError(name)


def _defaults() -> PodDefaults:
    return PodDefaults(
        security=Security.restricted(user=10001, fs_group=10001),
        node_selector={"kubernetes.io/arch": "amd64"},
        termination_grace_seconds=30,
    )


def test_defaults_apply_to_every_workload_and_container():
    app = App("shop", pod_defaults=_defaults())
    app.deployment(
        "api",
        image="registry.example/api@sha256:" + "1" * 64,
        init=[Container(name="migrate", image="registry.example/api:1")],
        sidecars=[Container(name="proxy", image="registry.example/proxy:1")],
    )
    app.deployment("web", image="registry.example/web:1", node="primary")
    composition = app.render("shop", nodes=NODES)
    for name in ("api", "web"):
        pod = _pod(composition, name)
        assert pod["securityContext"] == {
            "runAsNonRoot": True,
            "runAsUser": 10001,
            "runAsGroup": 10001,
            "fsGroup": 10001,
            "seccompProfile": {"type": "RuntimeDefault"},
        }
        assert pod["terminationGracePeriodSeconds"] == 30
        for container in (*pod.get("initContainers", []), *pod["containers"]):
            assert container["securityContext"] == {
                "allowPrivilegeEscalation": False,
                "capabilities": {"drop": ["ALL"]},
            }
    assert _pod(composition, "api")["nodeSelector"] == {"kubernetes.io/arch": "amd64"}
    # The node pin merges with the extra selector.
    assert _pod(composition, "web")["nodeSelector"] == {
        "kubernetes.io/arch": "amd64",
        "kubernetes.io/hostname": "node-a",
    }


def test_workload_fields_win_over_defaults():
    app = App("shop", pod_defaults=_defaults())
    app.deployment(
        "api",
        image="registry.example/api:1",
        security=Security(run_as_user=2000, read_only_root_filesystem=True),
        node_selector={"kubernetes.io/arch": "arm64", "pool": "fast"},
        termination_grace_seconds=5,
    )
    pod = _pod(app.render("shop"), "api")
    assert pod["securityContext"]["runAsUser"] == 2000
    assert pod["securityContext"]["fsGroup"] == 10001  # inherited field by field
    assert pod["containers"][0]["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    assert pod["nodeSelector"] == {"kubernetes.io/arch": "arm64", "pool": "fast"}
    assert pod["terminationGracePeriodSeconds"] == 5


def test_typed_fields_without_app_defaults():
    app = App("shop")
    app.deployment(
        "api",
        image="registry.example/api:1",
        security=Security(fs_group=3000),
        termination_grace_seconds=0,
    )
    pod = _pod(app.render("shop"), "api")
    assert pod["securityContext"] == {"fsGroup": 3000}
    assert "securityContext" not in pod["containers"][0]
    assert pod["terminationGracePeriodSeconds"] == 0


def test_override_still_patches_after_defaults():
    app = App("shop", pod_defaults=_defaults())
    api = app.deployment("api", image="registry.example/api:1")
    app.override(
        api,
        {
            "spec": {
                "template": {
                    "spec": {
                        "securityContext": {"runAsUser": None, "runAsGroup": 7},
                        "terminationGracePeriodSeconds": 60,
                    }
                }
            }
        },
    )
    pod = _pod(app.render("shop"), "api")
    assert "runAsUser" not in pod["securityContext"]
    assert pod["securityContext"]["runAsGroup"] == 7
    assert pod["terminationGracePeriodSeconds"] == 60


def test_rendering_is_unchanged_without_defaults():
    app = App("shop")
    app.deployment("api", image="registry.example/api:1", node="primary")
    pod = _pod(app.render("shop", nodes=NODES), "api")
    assert set(pod) == {"nodeSelector", "containers"}
    assert pod["nodeSelector"] == {"kubernetes.io/hostname": "node-a"}
    assert set(pod["containers"][0]) == {"name", "image"}


def test_node_selector_hostname_clashes_with_a_node_pin():
    app = App(
        "shop",
        pod_defaults=PodDefaults(node_selector={"kubernetes.io/hostname": "node-b"}),
    )
    with pytest.raises(ValueError, match=r"kubernetes\.io/hostname.*node="):
        app.deployment("api", image="registry.example/api:1", node="primary")
    with pytest.raises(ValueError, match=r"kubernetes\.io/hostname.*node="):
        App("shop").deployment(
            "api",
            image="registry.example/api:1",
            node="primary",
            node_selector={"kubernetes.io/hostname": "node-a"},
        )


@pytest.mark.parametrize(
    "fields, message",
    [
        ({"run_as_non_root": True, "run_as_user": 0}, "run_as_non_root"),
        ({"drop_capabilities": ["net_admin"]}, "capabilit"),
        ({"seccomp": "Localhost"}, "seccomp_localhost_profile"),
        ({"seccomp_localhost_profile": "p.json"}, "Localhost"),
        ({"run_as_user": -1}, "run_as_user"),
    ],
)
def test_security_validation(fields, message):
    with pytest.raises(ValidationError, match=message):
        Security(**fields)


def test_pod_defaults_validation():
    with pytest.raises(ValidationError):
        PodDefaults(node_selector={"bad key!": "x"})
    with pytest.raises(ValidationError):
        PodDefaults(termination_grace_seconds=-1)
    with pytest.raises(ValidationError):
        App("shop", pod_defaults={"unknown": 1})
