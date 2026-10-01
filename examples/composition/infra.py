"""A composition: two source repositories, three environments, one cluster.

The ``shop`` repository holds the ``web`` and ``api`` components, the
``catalog`` repository the ``catalog`` component; each repository carries
its ``piceli.toml`` (their contracts, see ``shop/piceli.toml`` and
``catalog/piceli.toml`` next to this file, the content of those two
repositories). This module only composes them:

- ``main`` deploys every push to ``main`` of either repository;
- ``rc`` deploys a new ``v*`` tag of either repository (the latest tag of
  each);
- every ``wp-*`` branch of ``shop`` gets a small environment
  (``shop-wp-…``) on the node ``BRANCH_NODE``, with ``catalog`` at ``main``.

A push rebuilds only the components whose files changed (their source
digest); the others keep their image and apply as no-op. Images go to the
in-cluster registry, which every node pulls from.

    piceli gitops enable examples/composition/infra.py --image piceli@sha256:… \\
        --kubeconfig FILE --context NAME                # plan, then --approve HASH
    piceli gitops status --kubeconfig FILE --context NAME
    piceli gitops sync main --component web --kubeconfig FILE --context NAME

The ``COMPOSITION_*`` environment variables point it at other repositories
and nodes (the kind test uses local bare repositories).
"""

from __future__ import annotations

import os

from piceli.envs import Branches, Environment, Stack, Tag
from piceli.infra import Cluster, Component, Source
from piceli.pipeline.model import Registry

REGISTRY_NODE = os.environ.get("COMPOSITION_REGISTRY_NODE", "my-cluster-worker")
BRANCH_NODE = os.environ.get("COMPOSITION_BRANCH_NODE", "my-cluster-worker2")

name = "shop"

cluster = Cluster(
    "my-cluster",
    api="https://127.0.0.1:6443",
    credentials="my-cluster",  # a `piceli login` profile, never in Git
    registry=Registry.in_cluster(on=REGISTRY_NODE, repository="shop", storage="1Gi"),
)

shop = Source(
    os.environ.get("COMPOSITION_SHOP_URL", "https://example.com/shop.git"), name="shop"
)
catalog = Source(
    os.environ.get("COMPOSITION_CATALOG_URL", "https://example.com/catalog.git"),
    name="catalog",
)

web = Component("web", source=shop, settings={"greeting": "hello from the composition"})
api = Component("api", source=shop)
items = Component("catalog", source=catalog)
# A third-party image, pinned: copied into the in-cluster registry on sync.
cache = Component.image(
    "docker.io/library/busybox:1.37.0",
    pin="sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e",
    name="cache",
    contract={
        "image": {"user": 65534, "cmd": ["httpd", "-f", "-p", "6379", "-h", "/tmp"]},
        "ports": {"cache": 6379},
        "health": {"ready": "TCP cache"},
    },
)

full = Stack("full", [web, api, items, cache])
small = Stack("small", [web, items])

environments = [
    Environment(
        "main",
        namespace="shop-main",
        stack=full,
        cluster=cluster,
        follow={shop: "main", catalog: "main"},
        auto_approve=True,
    ),
    Environment(
        "rc",
        namespace="shop-rc",
        stack=full,
        cluster=cluster,
        follow={shop: Tag("v*"), catalog: Tag("v*")},
        auto_approve=True,
    ),
    Environment.per_branch(
        Branches("wp-*"),
        namespace="shop-{branch}",
        stack=small,
        cluster=cluster,
        follow={shop: "{branch}", catalog: "main"},
        on_nodes=[BRANCH_NODE],
        limit=2,
        auto_approve=True,
    ),
]
