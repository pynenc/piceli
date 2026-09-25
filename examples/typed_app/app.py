"""A typed app: a cache on an existing claim, an API in front of it.

piceli render examples/typed_app/app.py:build --spec examples/typed_app/release.toml
piceli status examples/typed_app/release.toml   # is it up, and how to reach it
piceli access examples/typed_app/release.toml   # forward the declared ports
"""

from __future__ import annotations

from piceli import App, ExistingClaim, Resources
from piceli.k8s.release_spec import ReleaseContext


def build(ctx: ReleaseContext) -> App:
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
    app.service(
        api,
        port=80,
        target_port=8080,
        # How to reach it from a laptop; renders to no Kubernetes object.
        access=app.access.forward(local=18080, health="/healthz"),
    )
    app.network_policy(cache, allow_from=[api], ports=[6379])
    app.depends(api, on=cache)
    # Return the App (not app.composition(ctx)) so `piceli access` and
    # `piceli status` can read its access declarations.
    return app
