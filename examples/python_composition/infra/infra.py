"""A Python composition: the app, its build and its environments in one repository.

This directory is the content of the composition repository (``infra``);
``../api`` and ``../worker`` are the content of two product repositories
that carry nothing Piceli-specific. Here:

- ``example_app.py`` is the typed app (a :class:`piceli.Pipeline`) and
  ``host-build.toml`` how its images are built, its contexts reading the
  sources below;
- this module says where and when it deploys: ``main`` follows ``main`` of
  every repository, ``rc`` new ``v*`` tags of ``api``, and every ``wp-*``
  branch of ``api`` gets a small environment.

The controller follows this repository too (branch ``main``) and imports
this module at its commit: a change here re-renders every environment. A
push to ``worker`` rebuilds only the ``worker`` image (each image lists the
contexts it reads); the other images keep their digest and apply as no-op.

    piceli gitops enable infra.py --repo https://git.example.com/team/infra.git \\
        --image piceli@sha256:… --builder-image piceli@sha256:…   # plan, then --approve HASH

The ``EXAMPLE_*`` environment variables point it at other repositories and
nodes (the tests use local bare repositories).
"""

from __future__ import annotations

import os

from example_app import REGISTRY_NODE, pipeline, registry

from piceli.envs import Branches, Environment, Stack, Tag
from piceli.infra import Cluster, Controller, Source

name = "example"

cluster = Cluster(
    "my-cluster",
    api="https://127.0.0.1:6443",
    credentials="my-cluster",  # a `piceli login` profile, never in Git
    registry=registry,
    controller=Controller(on=REGISTRY_NODE),
)

infra = Source(
    os.environ.get("EXAMPLE_INFRA_URL", "https://example.com/infra.git"), name="infra"
)
api = Source(
    os.environ.get("EXAMPLE_API_URL", "https://example.com/api.git"), name="api"
)
worker = Source(
    os.environ.get("EXAMPLE_WORKER_URL", "https://example.com/worker.git"),
    name="worker",
)

full = Stack("full", workloads=["api", "worker", "cache"])
small = Stack("small", workloads=["api", "cache"])

environments = [
    Environment(
        "main",
        namespace="example-main",
        pipeline=pipeline,
        stack=full,
        cluster=cluster,
        follow={infra: "main", api: "main", worker: "main"},
        auto_approve=True,
    ),
    Environment(
        "rc",
        namespace="example-rc",
        pipeline=pipeline,
        stack=full,
        cluster=cluster,
        follow={infra: "main", api: Tag("v*"), worker: "main"},
    ),
    Environment.per_branch(
        Branches("wp-*"),
        namespace="example-{branch}",
        pipeline=pipeline,
        stack=small,
        cluster=cluster,
        follow={infra: "main", api: "{branch}", worker: "main"},
        on_nodes=[os.environ.get("EXAMPLE_BRANCH_NODE", "my-cluster-worker2")],
        limit=2,
        idle_stop="24h",
        auto_approve=True,
    ),
]
