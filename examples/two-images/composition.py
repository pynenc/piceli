"""Two workloads, one Deployment per image, pulled from the node-local registry.

Every image comes from a delivery receipt, so ``ctx.image(name)`` is
``127.0.0.1:<port>/<repository>@sha256:<manifest digest>``: an immutable
reference. Rebuilding and re-delivering one image changes exactly one
Deployment in the next plan.
"""

from __future__ import annotations

from piceli.k8s.ops.plan import (
    DeploymentComponent,
    DeploymentComposition,
    ResourceIntent,
)
from piceli.k8s.release_spec import ReleaseContext


def _deployment(ctx: ReleaseContext, name: str, node: str) -> ResourceIntent:
    labels = {"app.kubernetes.io/name": name}
    return ResourceIntent.from_manifest(
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": name, "namespace": ctx.namespace, "labels": labels},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": labels},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        # the registry answers only on this node's loopback
                        "nodeSelector": {"kubernetes.io/hostname": node},
                        "containers": [{"name": name, "image": ctx.image(name)}],
                    },
                },
            },
        }
    )


def build(ctx: ReleaseContext) -> DeploymentComposition:
    node = ctx.nodes["primary"].name
    return DeploymentComposition(
        tuple(
            DeploymentComponent(name, (_deployment(ctx, name, node),))
            for name in sorted(ctx.images)
        )
    )
