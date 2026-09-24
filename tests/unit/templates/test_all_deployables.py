"""
Every deployable template must produce complete manifests (apiVersion, kind,
metadata.name) and must be discoverable by the module loader.
"""

from collections.abc import Callable
from types import ModuleType

import pytest

from piceli.k8s import templates
from piceli.k8s.constants import secret_type
from piceli.k8s.constants.verbs import APIRequestVerb
from piceli.k8s.ops import loader


def _container(**kwargs: object) -> templates.Container:
    return templates.Container(name="app", image="busybox", **kwargs)  # type: ignore


def _resources() -> templates.Resources:
    return templates.Resources(cpu="100m", memory="128Mi")


# name -> (factory, expected kinds)
CASES: dict[str, tuple[Callable[[], templates.Deployable], list[str]]] = {
    "deployment": (
        lambda: templates.Deployment(name="web", containers=[_container()]),
        ["Deployment"],
    ),
    "deployment-service-hpa-vpa": (
        lambda: templates.Deployment(
            name="web",
            containers=[_container(ports=[templates.Port(name="http", port=80)])],
            create_service=True,
            hpa=templates.HPA(
                min_replicas=1, max_replicas=3, target_cpu_utilization_percentage=80
            ),
            vpa=templates.VPA(min_allowed=_resources(), max_allowed=_resources()),
        ),
        ["Deployment", "Service", "HorizontalPodAutoscaler", "VerticalPodAutoscaler"],
    ),
    "statefulset": (
        lambda: templates.StatefulSet(
            name="db",
            containers=[_container(ports=[templates.Port(name="pg", port=5432)])],
            create_service=True,
        ),
        ["StatefulSet", "Service"],
    ),
    "job": (
        lambda: templates.Job(name="task", containers=[_container()]),
        ["Job"],
    ),
    "cronjob": (
        lambda: templates.CronJob(
            name="cron", schedule="*/5 * * * *", containers=[_container()]
        ),
        ["CronJob"],
    ),
    "service": (
        lambda: templates.Service(
            name="svc",
            ports=[{"name": "http", "port": 80, "target_port": 8080}],
            selector={"app": "web"},
        ),
        ["Service"],
    ),
    "configmap": (
        lambda: templates.ConfigMap(name="cfg", data={"k": "v"}),
        ["ConfigMap"],
    ),
    "secret": (
        lambda: templates.Secret(
            name="sec",
            secret_type=secret_type.SecretType.OPAQUE,
            string_data={"k": "v"},
        ),
        ["Secret"],
    ),
    "serviceaccount": (
        lambda: templates.ServiceAccount(
            name="sa",
            roles=[
                templates.Role(
                    name="reader",
                    api_group="core",
                    resource="pods",
                    verbs=[APIRequestVerb.GET],
                )
            ],
        ),
        ["ServiceAccount", "Role", "RoleBinding"],
    ),
    "role": (
        lambda: templates.Role(
            name="reader", api_group="core", resource="pods", verbs=[APIRequestVerb.GET]
        ),
        ["Role"],
    ),
    "rolebinding": (
        lambda: templates.RoleBinding(
            name="rb", role_name="reader", service_account_name="sa"
        ),
        ["RoleBinding"],
    ),
    "hpa": (
        lambda: templates.HorizontalPodAutoscaler(
            name="hpa",
            target_kind="Deployment",
            target_name="web",
            min_replicas=1,
            max_replicas=3,
            target_cpu_utilization_percentage=50,
        ),
        ["HorizontalPodAutoscaler"],
    ),
    "vpa": (
        lambda: templates.VerticalPodAutoscaler(
            name="vpa",
            target_kind="Deployment",
            target_name="web",
            container_name=None,
            min_allowed=_resources(),
            max_allowed=_resources(),
            control_cpu=True,
            control_memory=True,
        ),
        ["VerticalPodAutoscaler"],
    ),
    "pv": (
        lambda: templates.PersistentVolume(name="pv", storage="1Gi", disk_name="disk"),
        ["PersistentVolume"],
    ),
    "pvc": (
        lambda: templates.PersistentVolumeClaim(name="pvc", storage="1Gi"),
        ["PersistentVolumeClaim"],
    ),
}


@pytest.mark.parametrize("case", list(CASES))
def test_deployable_manifests_are_complete(case: str) -> None:
    factory, expected_kinds = CASES[case]
    template = factory()
    assert isinstance(template, templates.Deployable)
    manifests = template.api_data()
    assert isinstance(manifests, list)
    assert [m["kind"] for m in manifests] == expected_kinds
    for manifest in manifests:
        assert isinstance(manifest, dict)
        assert manifest.get("apiVersion"), manifest
        assert manifest.get("kind"), manifest
        assert manifest.get("metadata", {}).get("name"), manifest


@pytest.mark.parametrize("case", list(CASES))
def test_deployable_found_by_loader(case: str) -> None:
    factory, expected_kinds = CASES[case]
    module = ModuleType(f"piceli_test_{case.replace('-', '_')}")
    module.my_template = factory()  # type: ignore[attr-defined]
    objects = list(loader.load_models_from_module(module))
    assert [o.kind for o in objects] == expected_kinds
    assert all(o.name and o.spec["apiVersion"] for o in objects)


def test_replica_manager_long_name_allowed() -> None:
    """The old (never enforced) 15-char limit is not a Kubernetes rule"""
    name = "a-deployment-name-longer-than-fifteen"
    deployment = templates.Deployment(
        name=name,
        containers=[_container(ports=[templates.Port(name="http", port=80)])],
        create_service=True,
    )
    assert {m["metadata"]["name"] for m in deployment.api_data()} == {name}


def test_replica_manager_default_selector_labels() -> None:
    deployment = templates.Deployment(
        name="web",
        containers=[_container(ports=[templates.Port(name="http", port=80)])],
        create_service=True,
    )
    deploy_manifest, service_manifest = deployment.api_data()
    labels = {"app": "web"}
    assert deploy_manifest["spec"]["selector"]["matchLabels"] == labels
    assert deploy_manifest["spec"]["template"]["metadata"]["labels"] == labels
    assert service_manifest["spec"]["selector"] == labels


def test_replica_manager_name_too_long() -> None:
    with pytest.raises(ValueError):
        templates.Deployment(name="a" * 64, containers=[_container()])


def test_replica_manager_service_name_must_start_with_letter() -> None:
    templates.Deployment(name="1web", containers=[_container()])
    with pytest.raises(ValueError, match="must start with a letter"):
        templates.Deployment(
            name="1web",
            containers=[_container(ports=[templates.Port(name="http", port=80)])],
            create_service=True,
        )
