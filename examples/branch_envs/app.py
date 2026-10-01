"""One repository, three kinds of environment: ``main``, ``rc`` and one per branch.

- ``main`` (namespace ``shop-main``) follows every push to the ``main``
  branch, with the full stack;
- ``rc`` (namespace ``shop-rc``) follows ``v*-rc*`` tags and ``piceli
  promote rc main@<sha>``;
- every ``wp-*`` branch gets ``shop-<branch>`` with a small stack on
  ``node-c``, its own 1Gi database claim seeded from ``main``, scaled to zero
  after a day without a push and deleted with the branch.

piceli envs --pipeline examples/branch_envs/app.py:pipeline
piceli env up rc --pipeline examples/branch_envs/app.py:pipeline --plan
piceli gitops enable examples/branch_envs/app.py:pipeline --repo URL \\
    --branches 'wp-*' --image REPO@sha256:… --kubeconfig F --context C

See docs/environments.md (Named environments, stacks and placement).
"""

from __future__ import annotations

from pathlib import Path

from piceli import (
    App,
    Branch,
    EnvConfig,
    ExistingClaim,
    Pipeline,
    Promote,
    Resources,
    RestorePoints,
    Stack,
    Tag,
    Target,
)
from piceli.envs import Environment

IMAGE = "registry.example/shop/{name}@sha256:{digest}"

app = App("shop")
web = app.deployment(
    "web",
    image=IMAGE.format(name="web", digest="a1" * 32),
    ports=[8080],
    resources=Resources(cpu="100m", memory="128Mi"),
)
app.service(web, 8080)
worker = app.deployment(
    "worker",
    image=IMAGE.format(name="worker", digest="b2" * 32),
    resources=Resources(cpu="100m", memory="128Mi"),
)
db = app.deployment(
    "db",
    image=IMAGE.format(name="db", digest="c3" * 32),
    volumes={"/var/lib/db": ExistingClaim("db-data")},
    resources=Resources(cpu="100m", memory="256Mi"),
)

full = Stack("full", workloads=[web, worker, db])
small = Stack("small", workloads=[web, db])

pipeline = Pipeline(
    app,
    Target(Path("kubeconfig.yaml"), context="my-cluster", namespace="shop"),
    restore_points=RestorePoints(),
    envs=EnvConfig(
        prefix="shop-",
        branches=["wp-*"],
        environments=[
            Environment(
                "main", namespace="shop-main", follow=Branch("main"), stack=full
            ),
            Environment(
                "rc",
                namespace="shop-rc",
                follow=[Tag("v*-rc*"), Promote()],
                stack=full,
                on_nodes=["node-a", "node-b"],
            ),
        ],
        branch_stack=small,
        branch_nodes=["node-c"],
        claim_sizes={"db-data": "1Gi"},
        seed_from="main",
        max_envs=2,
        idle_stop="24h",
    ),
)
