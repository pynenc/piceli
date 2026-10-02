"""A composition shaped like a small product team's: what runs where, and when.

Three repositories: this one (``infra``, the composition: the app, its build
and these rules) and two product repositories (``web`` and ``store``) that
carry nothing Piceli-specific. One cluster with a server node (builder,
controller, in-cluster registry) and two workload nodes.

- ``main`` follows ``main`` of every repository and deploys without asking
  (inside the pipeline's approval policy);
- ``rc`` deploys a ``v*-rc*`` tag of ``web`` or ``piceli promote rc
  main@<sha>``, then waits for ``piceli gitops approve rc <hash>``;
- every ``wp-*`` branch of ``web`` gets a small isolated environment on the
  second workload node, which may reach the API server (``allow_api``) and is
  deleted with its branch, volumes included (``delete_volumes``).

    piceli login lifecycle --kubeconfig FILE --context NAME   # once per machine
    piceli cluster init infra.py:cluster                       # plan, then --approve HASH
    piceli secrets git --cluster infra.py:cluster --prompt     # the one Git token
    piceli gitops enable infra.py --repo URL --image … --builder-image …   # plan, then --approve HASH

``lifecycle_site.py`` holds the cluster-specific values (API, node names,
Git remotes, the controller image); ``tests/acceptance_k3s/lifecycle.py``
runs this composition through its whole lifecycle on a disposable k3s
cluster.
"""

from __future__ import annotations

from lifecycle_app import pipeline, registry
from lifecycle_site import (
    AGENT_A,
    AGENT_B,
    API,
    ARCH,
    CONTROLLER_IMAGE,
    GIT_BASE,
    POLL,
    SERVER,
)

from piceli.envs import Branches, Environment, Promote, Stack, Tag
from piceli.infra import Cluster, Controller, Node, Source, Ui

name = "lifecycle"

cluster = Cluster(
    "lifecycle",
    api=API,
    credentials="lifecycle",  # a `piceli login` profile, never in Git
    nodes=[
        Node(SERVER, arch=ARCH, roles=["builder", "controller", "registry"]),
        Node(AGENT_A, arch=ARCH, roles=["workloads"]),
        Node(AGENT_B, arch=ARCH, roles=["workloads", "branch-envs"]),
    ],
    # A local-path class that keeps volumes (reclaimPolicy: Retain).
    storage_class="lifecycle-retain",
    registry=registry,
    # Branch teardown deletes the branch's retained volumes too.
    controller=Controller(
        on=SERVER, poll=POLL, image=CONTROLLER_IMAGE, delete_volumes=True
    ),
    ui=Ui(access="forward"),
)

# Source names are the build spec's context names (host-build.toml).
infra = Source(f"{GIT_BASE}/infra.git", name="infra")
web = Source(f"{GIT_BASE}/web.git", name="web")
store = Source(f"{GIT_BASE}/store.git", name="store")

minimal = Stack("minimal", workloads=["web", "store", "watcher", "cache"])
full = Stack("full", workloads=[*minimal.workloads, "reporter"])

environments = [
    Environment(
        "main",
        namespace="lc-main",
        pipeline=pipeline,
        stack=full,
        cluster=cluster,
        follow={infra: "main", web: "main", store: "main"},
        on_nodes=[AGENT_A],
        auto_approve=True,
    ),
    Environment(
        "rc",
        namespace="lc-rc",
        pipeline=pipeline,
        stack=full,
        cluster=cluster,
        follow={infra: "main", web: [Tag("v*-rc*"), Promote()], store: "main"},
        on_nodes=[AGENT_A],
    ),
    Environment.per_branch(
        Branches("wp-*", fallback="main"),
        namespace="lc-{branch}",
        pipeline=pipeline,
        stack=minimal,
        cluster=cluster,
        follow={infra: "main", web: "{branch}", store: "main"},
        on_nodes=[AGENT_B],
        quota={"requests.cpu": "1", "requests.memory": "1Gi"},
        claim_sizes={"store/data": "32Mi"},
        limit=2,
        idle_stop="24h",
        auto_approve=True,
        allow_api=True,
    ),
]
