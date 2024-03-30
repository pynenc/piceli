# Deployment

`Deployment` in Piceli is a simplification of the Kubernetes `Deployment` resource, extending the `ReplicaManager` class. It provides an easy-to-use interface for defining deployments, which manage the deployment and scaling of a set of pods.

## Properties and Methods

Inherits all properties and methods from `ReplicaManager`.

## Overridden Methods

- `get_replica_manager()`: Constructs and returns a `V1Deployment` object representing the Kubernetes deployment, including specifications for replicas, template, selector, and strategy.

## Usage

The `Deployment` class simplifies the creation and management of a Kubernetes deployment, ensuring all necessary configurations are applied effectively.

### Example

This example meticulously outlines the creation of a PostgreSQL deployment, emphasizing best practices for security, scalability, and high availability. It includes configurations for persistent storage, secure credentials access, and inter-container communication. The deployment is made scalable with the integration of an HPA, and it ensures service discovery by automatically creating a related service. Through the combination of these features, this example serves as a comprehensive guide for deploying complex applications on Kubernetes with Piceli.

```python
from piceli.k8s import templates

deployment = templates.Deployment(
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
```
