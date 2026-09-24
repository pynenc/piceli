"""A shop deployed from source with one command.

Two workloads (``web`` and ``api``) run the image built from
``examples/builds/rust-hello``; a Redis cache keeps its data on a claim the
release never manages. See docs/deploy.md.

    piceli render examples/shop/app.py:app --namespace shop       # no cluster
    piceli deploy examples/shop/app.py:pipeline --plan             # plan every stage
    piceli deploy examples/shop/app.py:pipeline --approve <hash>   # run them

The target defaults to a kind cluster named ``shop`` whose kubeconfig is
``examples/shop/shop.kubeconfig``; the ``SHOP_*`` environment variables
override it.
"""

from __future__ import annotations

import os

from piceli import (
    App,
    Build,
    ExistingClaim,
    NodeLoopbackRegistry,
    Pipeline,
    Random,
    Resources,
    Secrets,
    Target,
    Template,
)

target = Target.kubeconfig(
    os.environ.get("SHOP_KUBECONFIG", "shop.kubeconfig"),  # never ~/.kube/config
    context=os.environ.get("SHOP_CONTEXT", "kind-shop"),
    namespace=os.environ.get("SHOP_NAMESPACE", "shop"),
    nodes={"primary": (os.environ.get("SHOP_NODE", "shop-control-plane"), None)},
)
app = App("shop", owner="shop")

# Generated after the plan is accepted, stored privately, carried over.
secrets = Secrets(
    cache_password=Random(32),
    cache_url=Template("redis://:{cache_password}@cache:6379/0"),
)
credentials = app.secret(
    "cache-credentials",
    {"password": secrets.ref("cache_password"), "url": secrets.ref("cache_url")},
)

# One pinned Rust build; its image serves both web and api.
images = Build.spec("../builds/rust-hello/build.toml")

cache = app.deployment(
    "cache",
    image=(
        "docker.io/library/redis:7.2.3"
        "@sha256:a7cee7c8178ff9b5297cb109e6240f5072cdaaafd775ce6b586c3c704b06458e"
    ),
    command=["sh", "-c", 'exec redis-server --requirepass "$REDIS_PASSWORD"'],
    ports=[6379],
    env={"REDIS_PASSWORD": credentials.key("password")},
    volumes={"/data": ExistingClaim("cache-state")},  # data is never managed
    ready=app.probe.tcp(6379),
    resources=Resources(cpu="50m", memory="64Mi", memory_limit="256Mi"),
    strategy="Recreate",
)
api = app.deployment(
    "api",
    image=images["rust-hello"],
    ports=[8080],
    env={"PORT": "8080", "CACHE_URL": credentials.key("url")},
    ready=app.probe.http("/healthz", 8080),
)
web = app.deployment(
    "web",
    image=images["rust-hello"],
    ports=[3000],
    env={"PORT": "3000"},
    ready=app.probe.http("/", 3000),
)
app.service(cache, port=6379)
app.service(api, port=8080)
app.service(web, port=3000)
app.depends(api, on=cache)

try:  # post-deploy checks need piceli.checks
    from piceli.checks import Checks  # type: ignore[import-not-found,unused-ignore]

    checks = Checks.http(web, "/", expect=200)
except ImportError:
    checks = ()

pipeline = Pipeline(
    app,
    target,
    build=images,
    deliver=NodeLoopbackRegistry(port=5000),  # or NodeImport(), Registry(url)
    secrets=secrets,
    checks=checks,
    rollback_on_failed_checks=True,
    state_dir=os.environ.get("SHOP_STATE_DIR", ".piceli-deploy"),
)
