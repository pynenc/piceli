from kubernetes import client

from piceli.k8s import templates
from piceli.k8s.constants import policies, secret_type
from tests.unit.templates import yaml_utils

DEPLOYMENT = templates.Deployment(
    name="test-deployment",
    image_pull_secrets=["docker-registry-credentials"],
    template_labels={"pod_name": "test-deployment"},
    labels={"deployment_name": "test-deployment"},
    containers=[
        templates.Container(
            name="test-deployment",
            image="postgres-image",
            image_pull_policy=policies.ImagePullPolicy.IF_NOT_PRESENT,
            ports=[templates.Port(name="test-deployment", port=5432)],
            env={"PGDATA": "/var/lib/postgresql/data"},
            resources=templates.Resources(
                cpu="100m", memory="256Mi", ephemeral_storage="11Mi"
            ),
            volumes=[
                templates.VolumeMountPVC(
                    mount_path="/var/lib/postgresql/data",
                    sub_path="postgres",
                    pvc=templates.PersistentVolumeClaim(name="test-pvc", storage="1Gi"),
                ),
                templates.VolumeMountConfigMap(
                    mount_path="/var/lib/postgresql/data/config",
                    config_map=templates.ConfigMap(
                        name="db-configmap",
                        data={
                            "postgresql.conf": "file content...",
                            "pg_hba.conf": "host file content...",
                        },
                    ),
                    default_mode=0o777,
                ),
                templates.VolumeMountSecret(
                    mount_path="/etc/postgresql/ssl",
                    secret=templates.Secret(
                        name="db-secret",
                        secret_type=secret_type.SecretType.OPAQUE,
                        data={
                            "server.key": "cmFuZG9tX3N0cg==",
                            "server.crt": "cmFuZG9tX3N0cg==",
                        },
                    ),
                ),
                templates.VolumeMountEmptyDir(
                    mount_path="/var/run/postgresql", name="pg-socket"
                ),
            ],
            readiness_command=["pg_isready", "-some_options"],
        ),
        # some other container sharing emptydirectory
        templates.Container(
            name="sidecar-container",
            image="some-other-image",
            volumes=[
                templates.VolumeMountEmptyDir(
                    mount_path="/var/run/postgresql", name="pg-socket"
                ),
            ],
            liveness_pre_stop_command=["/bin/sh", "-c", "echo 'pre-stop'"],
        ),
    ],
    replicas=1,
    create_service=True,
    hpa=templates.HPA(
        min_replicas=1, max_replicas=10, target_cpu_utilization_percentage=80
    ),
)


def test_deployment_service() -> None:
    """test deployment template service spec"""
    service = DEPLOYMENT.get_service()
    assert isinstance(service, templates.Service)
    service_dict = client.ApiClient().sanitize_for_serialization(service.get())
    assert service_dict == yaml_utils.get_yaml_dict("deployment_service.yml")


def test_deployment_hpa() -> None:
    """test deployment template hpa spec"""
    hpa = DEPLOYMENT.get_hpa()
    assert isinstance(hpa, templates.HorizontalPodAutoscaler)
    objects = hpa.get()
    assert len(objects) == 1
    assert isinstance(objects[0], client.V2HorizontalPodAutoscaler)
    hpa_dict = client.ApiClient().sanitize_for_serialization(objects[0])
    assert hpa_dict == yaml_utils.get_yaml_dict("deployment_hpa.yml")


def test_deployment_replica_manager() -> None:
    object = DEPLOYMENT.get_replica_manager()
    assert isinstance(object, client.V1Deployment)
    deployment_dict = client.ApiClient().sanitize_for_serialization(object)
    assert deployment_dict == yaml_utils.get_yaml_dict("deployment.yml")


def test_deployment_get() -> None:
    """test deployment template get method"""
    objects = DEPLOYMENT.get()
    assert len(objects) == 3
    assert isinstance(objects[0], client.V1Deployment)
    assert isinstance(objects[1], client.V1Service)
    assert isinstance(objects[2], client.V2HorizontalPodAutoscaler)

    deployment_dict = client.ApiClient().sanitize_for_serialization(objects[0])
    service_dict = client.ApiClient().sanitize_for_serialization(objects[1])
    hpa_dict = client.ApiClient().sanitize_for_serialization(objects[2])
    deployment_dict = client.ApiClient().sanitize_for_serialization(objects[0])
    assert service_dict == yaml_utils.get_yaml_dict("deployment_service.yml")
    assert hpa_dict == yaml_utils.get_yaml_dict("deployment_hpa.yml")
    assert deployment_dict == yaml_utils.get_yaml_dict("deployment.yml")
