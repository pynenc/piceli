"""A typed app: a cache on an existing claim, an API in front of it.

piceli render examples/typed_app/app.py:build --spec examples/typed_app/release.toml
"""

from __future__ import annotations

from piceli import App, ExistingClaim, Resources
from piceli.k8s.ops.plan import DeploymentComposition
from piceli.k8s.release_spec import ReleaseContext


def build(ctx: ReleaseContext) -> DeploymentComposition:
    app = App("shop")
    password = app.secret("cache-password", {"password": ctx.secret("cache-password")})
    cache = app.deployment(
        "cache",
        image=ctx.image("cache"),
        command=["sh", "-c", 'exec redis-server --requirepass "$REDIS_PASSWORD"'],
        ports=[6379],
        ready=app.probe.tcp(6379),
        env={"REDIS_PASSWORD": password.key("password")},
        volumes={"/data": ExistingClaim("cache-state")},  # never managed
        resources=Resources(cpu="100m", memory="128Mi", memory_limit="256Mi"),
        strategy="Recreate",
    )
    api = app.deployment(
        "api",
        image=ctx.image("api"),
        ports=[8080],
        ready=app.probe.http("/healthz", 8080),
        env={"CACHE_PASSWORD": password.key("password")},
    )
    app.service(cache, port=6379)
    app.service(api, port=80, target_port=8080)
    app.network_policy(cache, allow_from=[api], ports=[6379])
    app.depends(api, on=cache)
    return app.composition(ctx)
