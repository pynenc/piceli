"""Release composition with a node-local registry and a workload that pulls from it.

``piceli release`` calls ``build`` with a
:class:`~piceli.k8s.release_spec.ReleaseContext`. The registry component
(ConfigMap, retained PVC, host-network Deployment) comes from the typed
``NodeLocalRegistry`` template; the workload pulls
``127.0.0.1:<port>/<repository>@sha256:…`` on the same node, so the node needs
no registry configuration.

The workload image must already be in the registry (push it by digest through
``kubectl port-forward deployment/registry 5000:5000``) before the first
apply. Release with ``app_replicas = 0`` first, push, then release again with
``app_replicas = 1``.
"""

from __future__ import annotations

from piceli.k8s import templates
from piceli.k8s.ops.plan import (
    DeploymentComponent,
    DeploymentComposition,
    ResourceIntent,
)
from piceli.k8s.release_spec import ReleaseContext


def build(ctx: ReleaseContext) -> DeploymentComposition:
    node = ctx.nodes["primary"].name if "primary" in ctx.nodes else ctx.values["node"]
    registry = templates.NodeLocalRegistry(
        node_name=node,
        image=ctx.image("registry"),
        port=int(ctx.values.get("registry_port", 5000)),
        storage=str(ctx.values.get("registry_storage", "10Gi")),
    )
    app_image = registry.pull_reference(
        str(ctx.values["app_repository"]), str(ctx.images["app"].digest)
    )
    labels = {"app.kubernetes.io/name": "app"}
    app = ResourceIntent.from_manifest(
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "app", "namespace": ctx.namespace, "labels": labels},
            "spec": {
                # 0 on the first release, before the image has been pushed
                "replicas": int(ctx.values.get("app_replicas", 1)),
                "selector": {"matchLabels": labels},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        # the registry is only reachable from this node's loopback
                        "nodeSelector": {"kubernetes.io/hostname": node},
                        "containers": [
                            {
                                "name": "app",
                                "image": app_image,
                                "command": ["sh", "-c", "sleep infinity"],
                            }
                        ],
                    },
                },
            },
        }
    )
    return DeploymentComposition(
        (
            registry.component(ctx.namespace),
            DeploymentComponent("app", (app,), dependencies=("registry",)),
        )
    )
