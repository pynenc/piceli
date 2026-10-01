"""The in-cluster UI: pinned image, its node, read-only RBAC, no exposed Service."""

from __future__ import annotations

import pytest

from piceli.infra import Cluster, Controller, Node, Ui
from piceli.infra.ui_install import (
    LAUNCH_SECRET,
    NAMESPACE,
    PORT,
    UiInstallError,
    render_ui,
)

IMAGE = "registry.example/piceli@sha256:" + "a" * 64
OTHER = "registry.example/piceli-ui@sha256:" + "b" * 64
WRITE = {"create", "update", "patch", "delete", "deletecollection", "*"}


def _cluster(**changes: object) -> Cluster:
    values: dict[str, object] = {
        "name": "my-cluster",
        "api": "https://192.0.2.10:6443",
        "credentials": "my-cluster",
        "nodes": (
            Node("node-a", arch="amd64", roles=("controller",)),
            Node("node-b", arch="arm64"),
        ),
        "controller": Controller(on="node-a", image=IMAGE),
        "ui": Ui(access="forward"),
    }
    values.update(changes)
    return Cluster(**values)  # type: ignore[arg-type]


def _by_kind(objects: list[dict]) -> dict[str, list[dict]]:
    found: dict[str, list[dict]] = {}
    for item in objects:
        found.setdefault(item["kind"], []).append(item)
    return found


def test_no_ui_declared_renders_nothing() -> None:
    assert render_ui(_cluster(ui=None)) == []


def test_ui_runs_the_controller_image_on_the_controller_node() -> None:
    objects = _by_kind(render_ui(_cluster()))
    (deployment,) = objects["Deployment"]
    pod = deployment["spec"]["template"]["spec"]
    (container,) = pod["containers"]
    assert container["image"] == IMAGE
    assert pod["nodeSelector"] == {"kubernetes.io/hostname": "node-a"}
    assert pod["serviceAccountName"] == "piceli-ui"
    assert container["args"][:2] == ["ui", "forward-serve"]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert deployment["metadata"]["namespace"] == NAMESPACE
    # No OIDC, no credential or token in the manifest.
    text = str(objects)
    assert "oidc" not in text.lower() and "token=" not in text


def test_ui_image_and_node_override_the_controller() -> None:
    objects = _by_kind(
        render_ui(_cluster(ui=Ui(access="forward", on="node-b", image=OTHER)))
    )
    pod = objects["Deployment"][0]["spec"]["template"]["spec"]
    assert pod["containers"][0]["image"] == OTHER
    assert pod["nodeSelector"] == {"kubernetes.io/hostname": "node-b"}


def test_without_a_node_the_ui_is_not_pinned() -> None:
    objects = _by_kind(render_ui(_cluster(controller=None, ui=Ui(image=IMAGE))))
    assert "nodeSelector" not in objects["Deployment"][0]["spec"]["template"]["spec"]


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"controller": Controller(on="node-a")}, "ui-install-image-unpinned"),
        (
            {"controller": Controller(on="node-a", image="registry.example/p:1.0")},
            "ui-install-image-unpinned",
        ),
        ({"ui": Ui(on="node-z", image=IMAGE)}, "ui-install-node-unknown"),
        ({"ui": Ui(access="public")}, "ui-install-access-unsupported"),  # type: ignore[arg-type]
    ],
)
def test_refusals_have_fixed_codes(changes: dict[str, object], code: str) -> None:
    with pytest.raises(UiInstallError) as refused:
        render_ui(_cluster(**changes))
    assert refused.value.code == code


def test_service_is_cluster_ip_only_and_nothing_is_exposed() -> None:
    objects = _by_kind(render_ui(_cluster()))
    (service,) = objects["Service"]
    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["ports"][0]["port"] == PORT
    assert "nodePort" not in str(service)
    assert "Ingress" not in objects and "Gateway" not in objects
    assert "hostNetwork" not in str(objects) and "hostPort" not in str(objects)


def test_rbac_reads_only_and_writes_exactly_two_objects() -> None:
    objects = _by_kind(render_ui(_cluster()))
    (cluster_role,) = objects["ClusterRole"]
    for rule in cluster_role["rules"]:
        assert not set(rule["verbs"]) & WRITE, rule
        assert "secrets" not in rule["resources"]
        assert "configmaps" not in rule["resources"]
    assert any("pods/log" in rule["resources"] for rule in cluster_role["rules"])
    (role,) = objects["Role"]
    writes = [rule for rule in role["rules"] if set(rule["verbs"]) & WRITE]
    assert {
        (resource, name)
        for rule in writes
        for resource in rule["resources"]
        for name in rule["resourceNames"]
    } == {("configmaps", "piceli-gitops-requests"), ("secrets", LAUNCH_SECRET)}
    assert all(
        not set(rule["verbs"]) & {"create", "delete", "*"} for rule in role["rules"]
    )
    assert all(rule.get("resourceNames") for rule in role["rules"])
    (binding,) = objects["ClusterRoleBinding"]
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": "piceli-ui", "namespace": NAMESPACE}
    ]


def test_precreated_objects_carry_no_data_so_reapply_keeps_them() -> None:
    objects = _by_kind(render_ui(_cluster()))
    (secret,) = objects["Secret"]
    (inbox,) = objects["ConfigMap"]
    assert secret["metadata"]["name"] == LAUNCH_SECRET and "data" not in secret
    assert inbox["metadata"]["name"] == "piceli-gitops-requests"
    assert "data" not in inbox


def test_rendering_is_deterministic() -> None:
    assert render_ui(_cluster()) == render_ui(_cluster())
