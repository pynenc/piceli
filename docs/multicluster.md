# Several clusters from one controller

A composition can declare several clusters: a *home* cluster that runs the
GitOps controller and builds every image, and other clusters the controller
deploys to through their API servers (small single-node clusters at the
edge, for example, reached over a private network). Each environment says
where it runs, with what differs per cluster, and in which order a revision
reaches them. Added in 0.15.

## Declaring the clusters

```python
from piceli import Registry
from piceli.envs import Environment, Placement, Rollout
from piceli.infra import Cluster, Controller, Node

home = Cluster(
    "home", api="https://10.0.0.1:6443", credentials="home",
    nodes=[Node("server", arch="amd64", roles=["builder", "controller", "registry"])],
    registry=Registry.in_cluster(on="server"),
    controller=Controller(on="server"),              # this one runs the controller
)
edge_canary = Cluster(
    "edge-canary", api="https://100.64.0.10:6443", credentials="edge-canary",
    nodes=[Node("edge-1", arch="amd64", roles=["workloads", "registry"])],
    registry=Registry.in_cluster(on="edge-1"),       # its own registry
)
edge_b = Cluster(
    "edge-b", api="https://100.64.0.11:6443", credentials="edge-b",
    nodes=[Node("edge-2", arch="amd64", roles=["workloads", "registry"])],
    registry=Registry.in_cluster(on="edge-2"),
)

environments = [
    Environment("main", namespace="shop-main", pipeline=pipeline, cluster=home,
                follow={infra: "main", web: "main"}, auto_approve=True),
    Environment(
        "edge", namespace="shop-edge", pipeline=pipeline,
        follow={infra: "main", web: "main"}, auto_approve=True,
        clusters=[
            Placement(edge_canary, on_nodes=["edge-1"]),
            Placement(edge_b, namespace="shop", replicas={"web": 2}),
        ],
        rollout=Rollout(order=["edge-canary", "edge-*"]),
    ),
]
```

- The **home** cluster is the one with `Controller(...)` (with a single
  cluster nothing changes: it is the home cluster, as before). An
  environment without `clusters=` runs there, as in 0.14.
- `clusters=[...]` lists a `Placement` per cluster (a bare `Cluster` is
  `Placement(cluster)`). A placement overrides, for that cluster only:
  `namespace` (a branch rule's is `"<prefix>{branch}"`), `on_nodes`,
  `values` (`{component: {key: value}}`, the settings of contract
  components), `replicas` (`{workload: count}`) or `pipeline` (a pipeline
  environment's pipeline there, for example
  `pipeline.for_environment("edge-b")`).
- `Environment(..., cluster=edge_b)` (another cluster than home, alone) is
  the same as `clusters=[edge_b]`.
- `Environment.per_branch(..., clusters=[...], rollout=...)` places branch
  environments too.
- `Rollout(order=[...])` orders the clusters in waves of name globs (a
  cluster no pattern matches is in a last wave). A pattern that matches no
  placed cluster is refused when the module loads.

## Credentials

Every cluster keeps its own credentials, never in Git:

```text
piceli login edge-canary --kubeconfig ~/edge-canary.yaml --context default    # your laptop
piceli cluster init infra.py:edge_canary                                        # its registry and node labels (plan, then --approve HASH)
piceli secrets cluster --cluster infra.py:edge_canary \
    --kubeconfig ~/edge-canary.yaml --context default                           # for the controller
piceli gitops enable infra.py --image … --builder-image …                        # plan, then --approve HASH
```

`piceli secrets cluster` writes the Secret `piceli-cluster-<name>` (key
`kubeconfig`) in `piceli-system` of the home cluster (`--home MODULE:ATTR`
when the module has no cluster with `Controller(...)`). The kubeconfig it
stores has one context with the certificates and token inlined; an exec
plugin, an auth provider, a token file, a user name and password, or a
context that skips TLS verification are refused
(`cluster-credentials-unsupported`): the controller runs no plugin. The
context's server must be the cluster's `api` (`cluster-api-mismatch`);
`--server URL` sets the address the *controller* uses when it differs from
yours (a private address only the home cluster reaches). With `--prompt`,
it reads a bearer token from stdin instead (typed without echo, or piped)
and takes the CA from the cluster's credential profile. It prints names
only, never a credential.

`gitops enable` grants the controller `get` on exactly those Secrets (a Role
`piceli-gitops-clusters` with their names); the controller reads a Secret
again at each use, so new credentials apply at the next poll. Inside the
controller each kubeconfig is a private file (mode `0600`) that goes with
the process.

Registering a provisioned machine from Python does the same as `piceli
login` plus `piceli secrets cluster`:

```python
from piceli.infra.multicluster import register_cluster

register_cluster(edge_b, kubeconfig=Path("edge-b.yaml"), context="default", home=home)
```

## Deploying

The controller keeps one record per environment and cluster:

- **Builds stay home.** Every image is built (or mirrored) once, on the home
  cluster, into its registry.
- **Each cluster pulls from its own registry.** Before deploying to another
  cluster the controller copies the images that cluster runs, by digest,
  from the home registry into that cluster's `Registry.in_cluster`, through
  the cluster's API server (the registry Service's proxy, with the
  cluster's credentials over TLS): no registry is exposed, and a cluster
  never pulls from the home one. An image already there is only verified.
  Run `piceli cluster init` on each cluster first: a cluster without its
  registry fails with `cluster-registry-copy-failed`.
- **Rollout order.** With `Rollout(order=["edge-canary", "edge-*"])` a
  revision deploys to `edge-canary` first; the clusters of the next wave
  wait (`reason: rollout-waiting`) until every cluster of the earlier waves
  runs that revision, healthy, with its checks passed. If the canary fails
  (its checks fail and the pipeline rolls it back once, as in 0.14.7, or the
  release is degraded), the later clusters are `held`
  (`reason: rollout-stopped`, `held_by`) at the release they run, until a
  new revision or `piceli gitops sync ENV`.
- **An unreachable cluster blocks nobody.** The controller probes each other
  cluster's API at every poll (`GET /version`, a few seconds). While one does
  not answer, or a deploy step loses it, its record waits and retries with
  backoff, without counting a failed attempt; the other clusters deploy.
  When it answers again it deploys the current revision. A cluster of an
  earlier rollout wave that is unreachable makes the later waves wait.
- **Approvals, syncs, stops** name the environment: `piceli gitops approve
  edge HASH` approves every cluster of `edge` waiting for that plan,
  `piceli gitops sync edge` and `piceli env stop|start edge` act on all of
  them.
- **History, restore points, pruning, retention** are per cluster: each
  cluster's runs have their own journal (the deployment history lists them
  all, each with its `cluster`), restore points and pruning run in the
  cluster they concern, and the registry retention of a cluster
  (`piceli artifacts retention --cluster infra.py:edge_b`) cleans that
  cluster's registry.

## Removing a cluster

Remove its `Placement` (or the whole `Cluster`) from the composition and
push. The controller, still holding the cluster's credential Secret, deletes
in that cluster what Piceli created for the environment: the objects of the
app (`app.kubernetes.io/part-of=<app>`) in its namespace. Claims and Secrets
are kept and listed under `removals` in the status, each with the command
deleting it; the namespace is deleted only when Piceli created it for that
environment and nothing was kept. Nothing else in the cluster is touched,
nor anything in the other clusters. Delete the Secret
`piceli-cluster-<name>` afterwards, and uninstall the cluster's registry
when you no longer need it.

## Status

`piceli gitops status --json` keeps one entry per environment (its state is
the most urgent of its clusters': `failed`, then `approval-required`,
`retrying`, `pending`, `held`, `stopped`, `deployed`) and adds, additively:

```json
{"envs": {"edge": {"state": "retrying", "health": "healthy",
  "clusters": {
    "edge-canary": {"state": "deployed", "health": "healthy", "checks": {"state": "passed", "passed": 6, "total": 6},
                    "revision": {"web": "8c1d2e3…"}, "reason": null,
                    "last_contact": "2026-10-04T10:00:00Z", "namespace": "shop-edge",
                    "api": "https://100.64.0.10:6443", "home": false, "wave": 0},
    "edge-b": {"state": "unreachable", "reason": "cluster-unreachable",
               "last_contact": "2026-10-04T09:40:00Z", "wave": 1}}}},
 "clusters": {"home": {"home": true, "reachable": true},
              "edge-b": {"api": "https://100.64.0.11:6443", "reachable": false,
                         "reason": "cluster-unreachable", "last_contact": "2026-10-04T09:40:00Z"}},
 "removals": []}
```

The human output prints a line per cluster under its environment. The web
UI's environment page has a Clusters panel and the environment inventory a
line per cluster (state, reason, last contact).

## Not yet

- `piceli access ENV` and `piceli status` reach the home cluster only.
- Third-party images a pipeline mirrors come from the home registry's and
  the pipeline's `mirror` lists; images go under the same repository paths
  in every registry.
- A pipeline's node aliases (`Target(nodes=…)`) and cluster UID pin apply
  to the home cluster only; on another cluster use `on_nodes`.
