"""Example release composition: a ConfigMap, two Secrets and a Deployment, typed.

``piceli release`` imports ``build`` and calls it with a ``ReleaseContext``.
The function is pure: it reads pinned images, the namespace, public
``[values]`` and opaque secret references. Secret values never reach this
code; ``ctx.secret(...)`` is a reference the executor resolves at apply time.

Preview the manifests without a cluster:
``piceli render --spec examples/release/release.toml``.
"""

from __future__ import annotations

from piceli import App, ConfigVolume, SecretVolume
from piceli.k8s.ops.plan import DeploymentComposition
from piceli.k8s.release_spec import ReleaseContext


def build(ctx: ReleaseContext) -> DeploymentComposition:
    app = App("web", labels={"app.kubernetes.io/name": "web"})
    config = app.config(
        "web-config",
        {"greeting": str(ctx.values.get("greeting", "hello"))},
        component="config",
    )
    token = app.secret(
        "web-token", {"token": ctx.secret("api-token")}, component="config"
    )
    tls = app.secret(
        "web-tls",
        {"tls.crt": ctx.secret("web-tls.crt"), "tls.key": ctx.secret("web-tls.key")},
        type="kubernetes.io/tls",
        component="config",
    )
    app.deployment(
        "web",
        image=ctx.image("web"),
        replicas=int(ctx.values.get("replicas", 1)),
        ports=[80],
        env={"API_TOKEN": token.key("token")},
        volumes={
            "/etc/web": ConfigVolume(config, name="config"),
            "/etc/web-tls": SecretVolume(tls, name="tls"),
        },
    )
    return app.composition(ctx)  # "web" depends on "config": it reads its objects
