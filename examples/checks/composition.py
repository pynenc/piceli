"""One web Deployment and its Service; the checks live in release.toml.

Readiness is a TCP probe, so an image that listens but serves the wrong
content becomes ready: only the post-deploy checks can catch it.
"""

from __future__ import annotations

from piceli.app import App, Probe
from piceli.k8s.ops.plan import DeploymentComposition
from piceli.k8s.release_spec import ReleaseContext


def build(ctx: ReleaseContext) -> DeploymentComposition:
    app = App("shop")
    web = app.deployment(
        "web", image=ctx.image("web"), ports=[8080], ready=Probe.tcp(8080)
    )
    app.service(web, port=80, target_port=8080)
    return app.composition(ctx)
