"""Pure composition example. The caller supplies an image and a private version."""

from piceli.k8s.ops.plan import (
    DeploymentComponent,
    DeploymentComposition,
    ResourceIntent,
)
from piceli.k8s.ops.secret_versions import SecretVersionRef


def composition(
    namespace: str, worker_image: str, credential: SecretVersionRef
) -> DeploymentComposition:
    secret = ResourceIntent.from_manifest(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "worker-credential", "namespace": namespace},
            "data": {"credential": "<private>"},
        }
    ).with_secret("/data/credential", credential)
    worker = ResourceIntent.from_manifest(
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "worker", "namespace": namespace},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": {"app": "worker"}},
                "template": {
                    "metadata": {"labels": {"app": "worker"}},
                    "spec": {
                        "containers": [
                            {
                                "name": "worker",
                                "image": worker_image,
                                "volumeMounts": [
                                    {
                                        "name": "credential",
                                        "mountPath": "/run/credential",
                                        "readOnly": True,
                                    }
                                ],
                            }
                        ],
                        "volumes": [
                            {
                                "name": "credential",
                                "secret": {"secretName": "worker-credential"},
                            }
                        ],
                    },
                },
            },
        }
    )
    return DeploymentComposition(
        (
            DeploymentComponent("credentials", (secret,)),
            DeploymentComponent("workers", (worker,), dependencies=("credentials",)),
        )
    )
