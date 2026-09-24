"""Example release composition: a ConfigMap, two Secrets and a Deployment.

``piceli release`` imports ``build`` and calls it with a
:class:`~piceli.k8s.release_spec.ReleaseContext`. The function is pure: it
reads pinned images, the namespace, public ``[values]`` and opaque secret
references, and returns a ``DeploymentComposition``. Secret values never reach
this code; ``with_secret`` binds a reference that the executor resolves at
apply time.
"""

from __future__ import annotations

from piceli.k8s.ops.plan import (
    DeploymentComponent,
    DeploymentComposition,
    ResourceIntent,
)
from piceli.k8s.release_spec import ReleaseContext


def build(ctx: ReleaseContext) -> DeploymentComposition:
    labels = {"app.kubernetes.io/name": "web"}

    def meta(name: str) -> dict:
        return {"name": name, "namespace": ctx.namespace, "labels": labels}

    config = ResourceIntent.from_manifest(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": meta("web-config"),
            "data": {"greeting": str(ctx.values.get("greeting", "hello"))},
        }
    )
    token = ResourceIntent.from_manifest(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": meta("web-token"),
            "type": "Opaque",
            "data": {"token": "<private>"},
        }
    ).with_secret("/data/token", ctx.secret("api-token"))
    tls = (
        ResourceIntent.from_manifest(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": meta("web-tls"),
                "type": "kubernetes.io/tls",
                "data": {"tls.crt": "<private>", "tls.key": "<private>"},
            }
        )
        .with_secret("/data/tls.crt", ctx.secret("web-tls.crt"))
        .with_secret("/data/tls.key", ctx.secret("web-tls.key"))
    )
    web = ResourceIntent.from_manifest(
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": meta("web"),
            "spec": {
                "replicas": int(ctx.values.get("replicas", 1)),
                "selector": {"matchLabels": labels},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "containers": [
                            {
                                "name": "web",
                                "image": ctx.image("web"),
                                "ports": [{"containerPort": 80}],
                                "env": [
                                    {
                                        "name": "API_TOKEN",
                                        "valueFrom": {
                                            "secretKeyRef": {
                                                "name": "web-token",
                                                "key": "token",
                                            }
                                        },
                                    }
                                ],
                                "volumeMounts": [
                                    {"name": "config", "mountPath": "/etc/web"},
                                    {"name": "tls", "mountPath": "/etc/web-tls"},
                                ],
                            }
                        ],
                        "volumes": [
                            {"name": "config", "configMap": {"name": "web-config"}},
                            {"name": "tls", "secret": {"secretName": "web-tls"}},
                        ],
                    },
                },
            },
        }
    )
    return DeploymentComposition(
        (
            DeploymentComponent("config", (config, token, tls)),
            DeploymentComponent("web", (web,), dependencies=("config",)),
        )
    )
