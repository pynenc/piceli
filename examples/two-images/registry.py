"""The node-local registry, released on its own.

The registry is platform, not part of the workload release: it is installed
once by this composition and the workload release (``composition.py``) only
pulls from it, so a workload release never manages its own registry.
"""

from __future__ import annotations

from piceli.k8s import templates
from piceli.k8s.ops.plan import DeploymentComposition
from piceli.k8s.release_spec import ReleaseContext


def build(ctx: ReleaseContext) -> DeploymentComposition:
    registry = templates.NodeLocalRegistry(
        node_name=ctx.nodes["primary"].name,
        image=ctx.image("registry"),
        port=int(ctx.values.get("registry_port", 5000)),
        storage=str(ctx.values.get("registry_storage", "1Gi")),
    )
    return DeploymentComposition((registry.component(ctx.namespace),))
