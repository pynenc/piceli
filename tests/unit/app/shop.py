"""A typed composition with a sidecar, an init container, a memory volume and an
existing claim: no manifest dicts anywhere."""

from __future__ import annotations

from typing import Any

from piceli import (
    App,
    Container,
    ExistingClaim,
    MemoryVolume,
    Probe,
    Resources,
    SecretVolume,
)
from piceli.k8s.ops.plan import DeploymentComposition


def build(ctx: Any) -> DeploymentComposition:
    app = App("shop", owner="shop-prod")
    settings = app.config("cache-settings", {"maxmemory": "64mb"})
    password = app.secret("cache-password", {"password": ctx.secret("cache-password")})
    tls = app.secret(
        "cache-tls",
        {
            "tls.crt": ctx.secret("cache-tls.crt"),
            "tls.key": ctx.secret("cache-tls.key"),
        },
        type="kubernetes.io/tls",
    )
    scratch = MemoryVolume(size_limit="16Mi")
    cache = app.deployment(
        "cache",
        image=ctx.image("cache"),
        command=["redis-server", "/run/cache/redis.conf"],
        env={"MAXMEMORY": settings.key("maxmemory")},
        ports=[6380],
        ready=app.probe.tcp(6380, period_seconds=5),
        resources=Resources(cpu="100m", memory="128Mi", memory_limit="256Mi"),
        volumes={
            "/data": ExistingClaim("cache-state"),
            "/run/cache": scratch,
            "/etc/cache-tls": SecretVolume(tls),
        },
        init=[
            Container(
                name="render-config",
                image=ctx.image("tools"),
                command=[
                    "sh",
                    "-c",
                    'echo "requirepass $PASSWORD" > /run/cache/redis.conf',
                ],
                env={"PASSWORD": password.key("password")},
                volumes={"/run/cache": scratch},
            )
        ],
        sidecars=[
            Container(
                name="exporter",
                image=ctx.image("exporter"),
                ports=[9121],
                live=Probe.http("/metrics", 9121),
                volumes={"/run/cache": scratch},
            )
        ],
        share_process_namespace=True,
        node="primary",
        strategy="Recreate",
    )
    api = app.deployment(
        "api",
        image=ctx.image("api"),
        ports=[8080],
        replicas=2,
        env={"CACHE_HOST": "cache", "CACHE_PASSWORD": password.key("password")},
        ready=app.probe.http("/healthz", 8080),
    )
    app.service(cache, port=6380)
    app.service(api, port=80, target_port=8080)
    app.network_policy(cache, allow_from=[api], ports=[6380])
    app.depends(api, on=cache)
    return app.composition(ctx)
