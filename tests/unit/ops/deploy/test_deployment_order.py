from piceli.k8s.k8s_objects.base import K8sObject
from piceli.k8s.ops.deploy import deployment_order


def test_classify_k8s_objects(resources: list[K8sObject]) -> None:
    classified = deployment_order.classify_k8s_objects(resources)

    assert len(classified) == 13
    for kind in [
        "Namespace",
        "Role",
        "ServiceAccount",
        "RoleBinding",
        "Secret",
        "ConfigMap",
        "PersistentVolume",
        "PersistentVolumeClaim",
        "Deployment",
        "Service",
        "Job",
        "CronJob",
        "HorizontalPodAutoscaler",
    ]:
        assert kind in classified
        assert len(classified[kind]) == 1
        assert classified[kind][0].name == f"example-{kind.lower()}"
