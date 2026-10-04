# Compositions in Python

Maturity: **preview** (the API may change before 1.0).

A *composition* deploys an app built from several Git repositories. The
main path keeps everything Piceli-specific in **one** repository, the
composition repository, in typed Python:

- the app is a {doc}`Pipeline <deploy>` (`App`, sidecars, generated Secrets,
  RBAC, checks, restore points: everything a single-repository pipeline can
  declare);
- its images come from a {doc}`host build <host_builds>` spec whose
  contexts read the other repositories;
- a module, usually `infra.py`, says where the app runs and when it deploys.

The product repositories carry nothing Piceli-specific: a push there only
triggers a build and a sync. For simple components, a `piceli.toml` in each
repository is an optional alternative (see {doc}`components`); both kinds of
environment can live in one composition.

```text
infra repo (the composition)       api repo        worker repo
├── infra.py      where and when   └── site/ …     └── jobs/ …
├── example_app.py  the Pipeline
├── host-build.toml contexts: infra, api, worker
└── config/ …
```

The example in `examples/python_composition/` is this layout: `infra/` is
the content of the composition repository, `api/` and `worker/` of the two
product repositories. The tests in `tests/unit/infra/test_pipeline_composition.py`
run its controller against local bare Git remotes.

## The composition module

```{literalinclude} ../examples/python_composition/infra/infra.py
:language: python
:start-at: "from piceli.envs import"
```

- `Environment(name, namespace=…, pipeline=PIPELINE, follow={Source: rule},
  stack=Stack(name, workloads=[…]), cluster=…, on_nodes=…, quota=…,
  auto_approve=…)`: the environment deploys the pipeline's app. `stack`
  names the app's workloads it runs; checks, `rollback_on_failed_checks`,
  restore points and the approval policy are the pipeline's
  (`auto_approve=True` with no pipeline policy uses the default
  `ApprovalPolicy()`: creates and updates). A rule is a branch name,
  `Tag("v*")`, `Promote()` or a list of them, as for {doc}`components`.
- `Environment.per_branch(Branches("wp-*"), namespace="<prefix>{branch}",
  pipeline=PIPELINE, follow={source: "{branch}", …}, stack=…, on_nodes=…,
  limit=…, idle_stop=…, claim_sizes=…, auto_approve=…)`: one environment
  per branch, removed with its branch.
- A pipeline environment never mixes with contract components: a `Stack` of
  `Component`s, `settings=` or `secrets=` next to `pipeline=` is refused
  (`env-config-invalid`). The pipeline has one target (a pipeline with one
  target per app environment: pass `pipeline.for_environment(NAME)`).
- The cluster's registry is `Registry.in_cluster(...)`: built images go
  there, and so do the third-party images of its `mirror=[ref@digest]` (and
  of the pipeline's own delivery `mirror=`). They are copied when an
  environment syncs (the whole index, by digest) and the app's references are
  rewritten to the copy: nodes never pull from a hosted registry at run time.

## Builds: contexts from sources, change-aware per image

The pipeline's builds are host builds
(`Build.spec("host-build.toml", builder="host")`; a Docker build needs a
Docker engine and is refused with `component-build-unsupported`):

```{literalinclude} ../examples/python_composition/infra/host-build.toml
:language: toml
:start-at: "[context.infra]"
```

- `[context.X] source = "X"` reads the composition's `Source` named `X` at
  the **environment's revision** (its commit of that source). The
  composition repository is a source too: name it in `follow` (as the
  example does) or leave it out and it is read at the commit the controller
  imported. A context without `source` reads the composition repository,
  relative to the spec's directory. A context of a source the environment
  does not follow is refused (`composition-invalid`, at `gitops enable` and
  at sync).
- `[[output.image]] contexts = ["api", "infra"]` (optional, default every
  context) lists what an image reads. Each image has a **change key**: its
  output table, the spec's `[build]` table and the Git blob id of every file
  its contexts include at their commits. No changed key: nothing is built.
  Otherwise **one** build runs (one Job, every source fetched at its commit
  with the one Git Secret `piceli-build-git`); only the images whose key
  changed are pushed and take the new image; the others keep their cached
  digest, render identically and apply as no-op.
- `Build(node_facts=…)` declared on the build is used for the build's
  platform (page size); otherwise the facts of the controller's first
  `--platform`.

### Several images from one source

Two images built from different paths of the **same** repository each get
a change key over their own paths only: declare one context per path, each
with its own `include`, both with the same `source`, and let each image read
its own context. A push that changes only `jobs/` rebuilds only `worker`; one
that changes only `site/` rebuilds only `api`; a file neither includes
(`README.md`) builds nothing. The build fetches the source once; each context
selects its own files from that checkout.

```toml
[context.site]
source = "api"
include = ["site/**"]

[context.api_jobs]
source = "api"
include = ["jobs/**"]

[[output.image]]
name = "api"
repository = "example/api"
contexts = ["site"]
files = { "site/site" = "/srv/site" }

[[output.image]]
name = "worker"
repository = "example/worker"
contexts = ["api_jobs"]
files = { "api_jobs/jobs" = "/opt/jobs" }
```

One context with both paths in its `include` would give both images the
same change key, so a change in either path would rebuild both.

## Deploying it

```text
piceli login my-cluster --kubeconfig ~/my-cluster.yaml   # once per machine
piceli cluster init infra.py:cluster                      # registry, nodes, controller
piceli secrets git --cluster infra.py:cluster --prompt    # the one Git Secret
piceli gitops enable infra.py --repo https://git.example.com/team/infra.git \
    --image REPO@sha256:… --builder-image REPO@sha256:… \
    --credentials-secret piceli-build-git                 # plan, then --approve HASH
piceli gitops status
piceli gitops sync main --component worker               # rebuild one image now
```

`gitops enable infra.py` (run from a clone of the composition repository,
`--root`, default `.`) imports the module on your machine, checks every
pipeline environment's builds, and records in the controller's config:

- the composition repository: `--repo` (default: the `origin` remote of
  `--root`), the branch to follow (`--main-branch`, default `main`) and the
  module's path in it; its source name is that of the `Source` with the same
  URL, if any;
- a summary of every environment, so the install plan hash covers the rules
  the controller starts with.

The controller follows the composition repository like any source: at each
poll it resolves the branch, and when it moved it imports the module at that
commit from its own checkout (the helper modules next to it too). Every
environment's revision includes that commit, so **a change in the
composition repository re-renders every environment**; images whose key did
not change are not rebuilt. A module that fails to import is recorded
(`controller.composition_repo.error`, `composition-invalid`) and the last
good one keeps deploying until the branch moves again.

### Status

The status keeps the `piceli.gitops-status.v1` schema of {doc}`components`;
an output image is a component:

- `envs.<env>.components.<image>`: `source` and `commit` (the first source
  it reads), `sources` (every source it reads, at its commit),
  `source_digest` (the change key), `digest` and `image` (the pushed image),
  `state` (`building`, `rolling`, `synced`, `unchanged`, `failed`), `health`
  and `updated_at`;
- `envs.<env>.revision` holds the composition repository's commit too;
- `controller.composition_repo`: `source`, `branch`, `entry`, `commit` (the
  imported one), `error` and `failed` (a commit that did not import).
- `envs.<env>.deleted` and `envs.<env>.kept_orphaned`: what the last
  deploy pruned (objects the render no longer declares) and kept (claims,
  Secrets, retained objects, each with the command deleting it); a pending
  plan (`envs.<env>.pending_plan`) lists its `delete` changes and
  `kept_orphaned` too, and every run of the deployment history
  (`piceli-gitops-history`) has `deleted` and `kept_orphaned`. See
  {doc}`gitops` (removed objects are deleted).

### Several clusters and replicas per environment

A composition may declare several clusters: environments then name where
they run (`clusters=[Placement(...)]`) and in which order
(`Rollout(order=[...])`); see {doc}`multicluster`.

`Environment(..., replicas={"web": 3})` and `Environment.per_branch(...,
replicas={"db": 1})` set the replica count of Deployments and StatefulSets
in that environment (0.15): a branch environment can run one member of a
replicated StatefulSet while main runs three. Each name must be a
Deployment or StatefulSet the environment renders; a workload a
HorizontalPodAutoscaler scales is refused (set its `min_replicas` and
`max_replicas` instead). Undeclared, nothing changes (the same config and
plan hashes as before).

## Not yet

- A pipeline's `secrets=` generators run in the controller's pod (their
  store on its state volume); this is not yet tested end to end.
- A build reads its node facts from `Build(node_facts=…)` or the platform's
  defaults; the controller does not read a node's page size.
- Branch environments are not seeded from restore points.
