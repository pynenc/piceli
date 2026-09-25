"""A small shop described as a typed App and deployed by a Pipeline.

Two workloads from pinned images (no build, so no Docker is needed), a
generated secret and the owner's approval policy. The target is the explicit
kubeconfig file and context the owner gives you, through ``SHOP_KUBECONFIG``
and ``SHOP_CONTEXT``; never ``~/.kube/config`` and never the current context.

    piceli render examples/shop.py:pipeline                 # no cluster
    python scripts/plan.py examples/shop.py:pipeline        # plan, show the owner
    python scripts/deploy.py examples/shop.py:pipeline --approve <hash>
"""

from __future__ import annotations

import os

from piceli import App, ApprovalPolicy, Pipeline, Random, Secrets, Target

KUBECONFIG = os.environ.get("SHOP_KUBECONFIG")
CONTEXT = os.environ.get("SHOP_CONTEXT")
if not KUBECONFIG or not CONTEXT:
    raise SystemExit(
        "set SHOP_KUBECONFIG and SHOP_CONTEXT to the kubeconfig file and context "
        "the owner gave you (never ~/.kube/config or the current context)"
    )

target = Target.kubeconfig(
    KUBECONFIG,
    context=CONTEXT,
    namespace=os.environ.get("SHOP_NAMESPACE", "shop"),
    # "loopback-http" only for a local test API (piceli.testing); default https.
    transport=os.environ.get("SHOP_TRANSPORT", "https"),
)

app = App("shop", owner="shop")

# Generated after the owner approves the plan, stored privately; plans and
# receipts carry digests, never the value.
secrets = Secrets(cache_password=Random(24))
credentials = app.secret(
    "cache-credentials", {"password": secrets.ref("cache_password")}
)

GREETING = "hello"  # change it to make a new release

cache = app.deployment(
    "cache",
    image=(
        "docker.io/library/redis:7.2.3"
        "@sha256:a7cee7c8178ff9b5297cb109e6240f5072cdaaafd775ce6b586c3c704b06458e"
    ),
    command=["sh", "-c", 'exec redis-server --requirepass "$REDIS_PASSWORD"'],
    ports=[6379],
    env={"REDIS_PASSWORD": credentials.key("password")},
)
web = app.deployment(
    "web",
    image=(
        "docker.io/library/busybox:1.36"
        "@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662"
    ),
    command=[
        "sh",
        "-c",
        'mkdir -p /www && echo "$GREETING" > /www/index.html && exec httpd -f -p 8080 -h /www',
    ],
    ports=[8080],
    env={"GREETING": GREETING},
)
app.service(cache, port=6379)
app.service(web, port=8080)
app.depends(web, on=cache)

pipeline = Pipeline(
    app,
    target,
    secrets=secrets,
    state_dir=os.environ.get("SHOP_STATE_DIR", ".piceli-deploy"),
    execution={
        "readiness_seconds": int(os.environ.get("SHOP_READINESS_SECONDS", "300"))
    },
    # Declared by the owner, reviewed like any code, part of the plan hash.
    # `piceli deploy --approve-if-policy` runs a plan without the human hash
    # only when every action is inside it. Never add or widen it yourself.
    auto_approve=ApprovalPolicy(allow={"create", "apply", "no-op"}, max_objects=10),
)
