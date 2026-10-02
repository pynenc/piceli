# Environments: one app for dev, staging and prod

This page shows how to describe the differences between environments (more
replicas in prod, other hosts in staging, a debug component only in dev) as
typed overrides of one `App`, how to render and compare environments, and
how to deploy each one to its own target.

```{admonition} Maturity: preview
:class: note

`Environment`, `app.environment(...)` and `--env`/`--diff-env` are new in
0.7.0. Names and options may still change before 1.0.
```

## Prerequisites

- Piceli 0.7.0 or later and a typed app (see {doc}`typed_apps`).
  {doc}`reference_app` is a complete example with three environments.
- To deploy, a `Pipeline` (see {doc}`deploy`) with one `Target` per
  environment.

## Steps

1. **Declare the app once, then its environments.** Each environment names
   only what differs. Declare environments after the objects they change:
   every key is checked against the declared objects right away.

   ```{literalinclude} ../examples/environments/app.py
   :language: python
   :start-at: app = App("shop")
   :end-before: pipeline = Pipeline(
   ```

2. **Render one environment** (no cluster):

   ```text
   piceli render examples/environments/app.py:app --env prod
   ```

   Without `--namespace`, an `App` renders into the environment's
   `namespace` (else `default`); a pipeline renders into its target's.

3. **Compare two environments:**

   ```text
   piceli render examples/environments/app.py:app --env staging --diff-env prod
   piceli render examples/environments/app.py:pipeline --env staging --diff-env prod --format json
   ```

4. **Deploy each environment to its own target.** Give the pipeline one
   `Target` per environment:

   ```{literalinclude} ../examples/environments/app.py
   :language: python
   :start-at: pipeline = Pipeline(
   ```

   then plan and approve one environment at a time:

   ```text
   piceli deploy examples/environments/app.py:pipeline --env prod --plan
   piceli deploy examples/environments/app.py:pipeline --env prod --approve <combined hash>
   piceli release status --spec examples/environments/app.py:pipeline --env prod
   ```

   A portable plan file (`--plan --out FILE`, see {doc}`deploy`) records the
   environment, and `piceli deploy --apply FILE --approve HASH` deploys that
   environment; with `state="cluster"` ({doc}`state`) each environment's
   state and release lock live in its own target namespace.

### Expected output

`--diff-env` prints the environments' typed differences: the override values,
then every object that differs, field by field (namespaces are compared
separately, also where RBAC objects repeat them, so moving between namespaces
is not noise):

```text
environments: staging (a) -> prod (b)
namespace: shop-staging -> shop-prod
values:
    config.settings: (absent) -> {"LOG_LEVEL": "warning"}
    namespace: "shop-staging" -> "shop-prod"
    node_selector.api: (absent) -> {"node-pool": "general"}
    replicas.api: 2 -> 3
    resources.api: (absent) -> {"cpu": "500m", "memory": "512Mi", "memory_limit": "1Gi"}
    specs.shop-tls.dnsNames: ["shop.staging.example.com"] -> ["shop.example.com", "www.shop.example.com"]
~ Deployment/api (component api):
    spec.replicas: 2 -> 3
    spec.template.spec.containers[api].resources.limits: (absent) -> {"memory": "1Gi"}
    spec.template.spec.containers[api].resources.requests.cpu: "100m" -> "500m"
    spec.template.spec.containers[api].resources.requests.memory: "128Mi" -> "512Mi"
    spec.template.spec.nodeSelector: (absent) -> {"node-pool": "general"}
~ Certificate/shop-tls (component shop-tls):
    spec.dnsNames: ["shop.staging.example.com"] -> ["shop.example.com", "www.shop.example.com"]
~ ConfigMap/settings (component settings):
    data.LOG_LEVEL: "debug" -> "warning"
3 changed, 0 only in staging, 0 only in prod, 2 identical
```

With `--format json` it prints one object:
`{"state": "diffed", "environments": {"a", "b"}, "namespaces": {"a", "b"},
"values": [{"path", "a"?, "b"?}], "objects": [{"api_version", "kind", "name",
"component", "change": "changed" | "only-a" | "only-b", "fields": [{"path",
"a"?, "b"?}]}], "summary": {"changed", "only-a", "only-b", "same"}}`. A
side without a value omits `a` or `b`. Paths are dotted; list items with a
unique `name` (containers, env, ports) are matched by name
(`containers[api]`), other lists by position.

`piceli render --env NAME --format json` adds `"environment": NAME` to the
rendered object. `piceli deploy --env NAME --plan --json` adds
`"environment": {"name", "values"}` to the result.

### If it fails

| Reason | Meaning | Fix |
| --- | --- | --- |
| `environment-unknown` | `--env` names no declared environment, or the pipeline has no target for it | Use a declared name; add the target. |
| `environment-required` | The pipeline has one target per environment and no `--env` was given, or `--diff-env` came without `--env` | Add `--env NAME`. |
| `environment-invalid` | An override names no suitable object (`replicas` on a ConfigMap, a typo), is ambiguous, disables a component another workload reads, or holds a value the object refuses | Fix the override as the message says. |
| `environment-unsupported` | The target renders a `DeploymentComposition`, or the spec is a `release.toml` | Return the `App` from the composition function; use a pipeline for `release --env`. |

## Overrides

`app.environment(name, **overrides)` (or `Environment(name, ...)`) takes:

| Override | Keys | Effect |
| --- | --- | --- |
| `namespace` | – | Namespace for `piceli render --env` without `--namespace` or a spec. |
| `replicas` | workload | Replaces `replicas`. Refused for a workload an autoscaler targets: use `autoscalers`. |
| `autoscalers` | autoscaler (named after its workload by default) | A `Scaling(min_replicas=…, max_replicas=…, cpu=…, memory=…)`: the fields it sets replace the autoscaler's. |
| `images` | workload | Replaces the image of the main (first) container. In a pipeline it must still be a build handle or pinned by digest. |
| `resources` | workload | Replaces the main container's `Resources`. An autoscaled workload must keep the requests its utilization targets use. |
| `config` | config | Merges values into its data; `None` removes a key. |
| `node_selector` | workload | Merges labels over the workload's own `node_selector` (the app's `pod_defaults` still apply). |
| `hosts` | object with `hosts`/`host` | Replaces its host names (routes and ingresses). |
| `specs` | resource | Replaces an `app.resource(...)` spec. A mapping is validated into the declared spec's model; another model type is refused. |
| `enabled` | component | `False` leaves the whole component out. |

Keys name declared objects: `"api"`, or `"Deployment/api"` when two kinds
share the name. Every value is validated twice: its type when the
environment is built (`replicas={"api": -1}` is a `ValidationError`), and
against the object when it is applied (a `node_selector` that conflicts with a
`node=` pin fails with `environment-invalid` naming the environment and key).

`app.for_environment(name)` returns a new App with the overrides applied; the
declaring app never changes, and overrides from `app.override(...)` still
apply on top. A derived app knows its environment
(`app.selected_environment`) and cannot be derived again.

Anything the overrides do not cover can still be expressed in Python: an
environment is a plain value, so a module may also build its App in a
function of the environment name. Prefer the typed overrides: they are
checked, diffable and part of the plan hash.

## Pipelines and targets

`Pipeline(app, target, ...)` accepts a single `Target` (unchanged) or a
mapping of environment name to `Target`:

- every name must be an environment the app declares, and no two
  environments may share a context and namespace (the same namespace name in
  two clusters is fine);
- every command then needs `--env` (`environment-required` otherwise):
  `piceli render`, `piceli deploy` (including `--approve` and `--resume`)
  and every `piceli release … --spec MODULE:ATTR` command;
- each environment keeps its own state (journal, receipts, release catalog,
  secret store) in `<state_dir>/environments/<name>`, so environments never
  share or lock each other's runs; builds are cached per environment;
- `piceli status` and `piceli access` take a pipeline with one target: expose
  one per environment in the module (`prod = pipeline.for_environment("prod")`)
  and pass `app.py:prod`.

### The plan hash covers the environment

`piceli deploy --env NAME` binds its combined hash to the environment's
**name and resolved values** (`Environment.values()`, canonical JSON), besides
the target, the rendered manifests and every other stage. Approving one
environment's hash for another, or after any override changed, is refused
with `pipeline-plan-changed`, even when the rendered manifests happen to be
the same. Plans of pipelines without environments keep their hashes.

(branch-environments)=
## Per-branch environments

```{admonition} Maturity: experimental
:class: note

`EnvConfig`, `piceli env`, `piceli envs` and `piceli logs` are new in 0.13.0.
```

Named environments above are a few long-lived targets. A pipeline can also run
**one namespace per Git branch**: every branch that matches a pattern gets its
own isolated copy of the app next to main, in the same cluster.

```python
from piceli import EnvConfig, Pipeline

pipeline = Pipeline(
    app,
    target,
    build=images,
    deliver=NodeLoopbackRegistry(),
    envs=EnvConfig(
        prefix="shop-",  # branch wp-login -> namespace shop-wp-login
        main_namespace="shop",  # default: the target's namespace
        branches=["main", "wp-*"],
        max_envs=3,  # running branch environments
        quota={"pods": "20", "requests.storage": "20Gi"},
        claim_sizes={"db/data": "1Gi"},  # branch claims are small and start empty
        seed_from=None,  # "main": seed new environments from main
        auto_approve=False,  # True: branches need no hash (--approve-if-policy)
    ),
)
```

Declaring `envs` changes nothing for main: its rendering and plan hashes are
the same, and `piceli deploy` keeps deploying it.

### The namespace of a branch

`<prefix><slug>`: the branch lowercased, every other character run replaced
by `-` (`wp/Login_Page` → `shop-wp-login-page`). A name over 63 characters is
cut and ends with `-` and 8 hex of the branch's SHA-256. The main branch maps
to `main_namespace`, which env commands never create, stop or delete. Two
branches that map to the same namespace are refused
(`env-namespace-collision`).

### Bring a branch up

```text
piceli env up wp-login --pipeline deploy/app.py:pipeline --digest api=127.0.0.1:5000/shop/api@sha256:…
```

Branch environments never build or deliver: the images come from
`--digest NAME=REF` (or `NAME=sha256:…`, pulled from the pipeline's
registry), a build receipt (`--receipt FILE`: a host build, a cluster build
of `piceli build job`, or `{"images": {NAME: REF}}`), or, with neither, the
digests `piceli env push` recorded for the branch. Without `--approve` it
prints the plan and its `env_hash` (exit `3`): the namespace to create, the
environments the budget stops, the deploy's combined plan and the seed.
`--approve HASH` runs exactly that plan. `--approve-if-policy` runs a branch
when the owner declared `EnvConfig(auto_approve=True)`, or when the
pipeline's `auto_approve` policy allows the deploy and nothing is stopped;
the main branch only by that policy. A new namespace has nothing to plan
against: the hash covers the rendered model, and the deploy into it may only
create objects.

Each branch keeps its own state in `<state_dir>/branches/<namespace>`, so its
Secrets are generated from the app's declarations in that namespace and never
copied from main. The environment is recorded in the ConfigMap `piceli-env`
of its namespace (branch, commit, images, last push, deploy state).

### Isolation

A branch renders into its own namespace, and its plan is refused
(`env-isolation-*`) when it would reach another environment or the node:

- a Service named absolutely in another namespace
  (`api.shop.svc.cluster.local`): name Services by their relative name
  (`default` and `kube-system` stay reachable);
- a `NodePort` or `LoadBalancer` Service, a `hostPort`, `hostNetwork`,
  `hostPID`/`hostIPC`, or a `hostPath` volume;
- a role binding to a subject of another namespace, or a NetworkPolicy peer
  selecting every namespace or another branch's;
- a claim bound to a volume the app does not declare, and cluster objects
  that cannot be renamed (`CustomResourceDefinition`, `Namespace`).

Other cluster-scoped objects get the namespace in their name
(`reader` → `reader-shop-wp-login`, references rewritten; RBAC declared with
`cluster_rules=` already is). Every branch also gets a `NetworkPolicy`
`piceli-env-isolation` that denies traffic across namespaces (its own
namespace and the cluster DNS stay allowed; `allow_egress=["0.0.0.0/0"]`
opens the internet) and a `ResourceQuota` of the same name (`quota`; by
default only counts, which need no pod requests). A controller in another
namespace (an ingress controller) needs its own allowing policy in the app.

### Data

Branch claims use `claim_sizes` (by workload, `workload/claim template` or
claim name) and start empty. `piceli env seed wp-login` (or `env up
--seed-from main`) restores main's latest verified restore point (see
{doc}`restore_points`) into the branch's claims: it verifies the local
archives, stops the branch's writers, empties and restores each claim, checks
its content digest in the cluster and starts the writers again. Main's
claims are only read from the local archive, never touched.

### Budget and teardown

At most `max_envs` branch environments run. `env up` of one more stops the
least recently pushed one (its Deployments and StatefulSets scaled to zero,
recorded; the next `env up` of it starts them again); `--wait` refuses with
`env-budget-full` instead. `piceli env down wp-login` plans, then with the
`env_hash` deletes the branch's claims, its namespace and the volumes bound to
them, and its local state. It refuses the main branch and namespace always
(`env-main-protected`) and any namespace without this app's
`piceli.io/env-of` label (`env-namespace-not-managed`).

### Named environments, stacks and placement

```{admonition} New in 0.14.0
:class: note

`environments=`, `Stack`, `on_nodes`, `branch_stack`, `branch_nodes` and
`idle_stop` are new; a 0.13 `EnvConfig` renders and hashes as before.
```

Besides one namespace per branch, `EnvConfig(environments=[...])` declares
**named, long-lived environments**, each with its own trigger, namespace,
stack and placement. (`piceli.envs.Environment` is not the app overlay
`piceli.Environment` above: it says where the pipeline deploys and what
deploys it.)

```python
from piceli import Branch, EnvConfig, Promote, Stack, Tag
from piceli.envs import Environment

full = Stack("full", workloads=["web", "worker", "db", "cache"])
small = Stack("small", workloads=["web", "db"])

envs = EnvConfig(
    prefix="shop-",
    branches=["wp-*"],
    environments=[
        # every push to main deploys here
        Environment("main", namespace="shop-main", follow=Branch("main"), stack=full),
        # a new v*-rc* tag, or `piceli promote rc main@<sha>`
        Environment(
            "rc",
            namespace="shop-rc",
            follow=[Tag("v*-rc*"), Promote()],
            on_nodes=["node-a", "node-b"],
            quota={"pods": "40"},
        ),
    ],
    # branch environments: a small stack on chosen nodes
    branch_stack=small,
    branch_nodes=["node-c"],  # or {"example.com/pool": "branches"}
    claim_sizes={"db-data": "1Gi"},  # an ExistingClaim becomes the branch's own
    seed_from="main",
    idle_stop="24h",
)
```

- **Triggers.** `follow` takes `Branch("NAME")` (every push to that branch,
  the first one seen too), `Tag("GLOB")` (a new matching tag; tags present
  when the environment is first seen are its baseline) and `Promote()`
  (`piceli promote ENV BRANCH@SHA`), one or a list. The GitOps controller
  evaluates each environment on its own, so one branch can feed two
  environments (main deploys `main` on every push and `rc` on a tag). An
  environment named like `main_branch` replaces the implicit main
  environment (`main_namespace`, deployed on `v*` tags and promotions);
  without one, 0.13's main behaviour is unchanged.
- **Commands.** `piceli env up NAME` deploys a named environment like main:
  it builds unless `--digest`/`--receipt` give the images, keeps restore
  points (in main's store, so `seed_from` may name it), and creates the
  namespace when absent (label `piceli.io/env-name`). Env commands never
  delete, stop or seed it (`env-main-protected`); it is never counted in
  `max_envs`. `piceli envs` lists it after main with `"fixed": true` and its
  name as `branch`. `auto_approve=True` lets the controller deploy it
  without a hash when the plan is inside the pipeline's `auto_approve`
  policy (like `--main-auto-approve`). Environment names take precedence
  over branch names, and a branch whose namespace is a named environment's
  is refused (`env-namespace-collision`).
- **Stacks.** `Stack(name, workloads=[...])` names workloads of the app (by
  name or the declared object). An environment with a stack renders only
  them: a component whose workloads are all left out is left out whole (its
  Service, autoscaler, …); configs, Secrets and accounts always render. A
  name that is no workload is refused (`env-stack-unknown`), and so is a
  kept object that depends on a left-out component (`env-stack-incomplete`).
  The pipeline's checks follow the stack: a check whose Service or workload
  target the environment does not render is skipped and listed as
  `not-in-stack` under `skipped` in the checks stage; a check without such a
  target (`python`, `pod/NAME`) still runs.
- **Placement.** `on_nodes=["node-a", ...]` adds a required node affinity
  `kubernetes.io/hostname In [...]` to every workload; a mapping
  `{"label": "value"}` is merged into `nodeSelector`. A workload pinned to
  another node (`node=`, or the delivery node of a node-loopback registry)
  is refused (`env-nodes-conflict`): use a registry every node pulls from.
- **Branch claims.** In a branch environment, an `ExistingClaim` with a size
  in `claim_sizes` (by claim name, `workload/claim` or workload) becomes a
  claim the environment owns: created empty (or seeded with `seed_from`),
  deleted with the environment. Without a size it stays an existing claim,
  as in 0.13. Main and named environments keep `ExistingClaim` semantics.
- **Idle stop.** With `idle_stop="24h"` the GitOps controller scales a branch
  environment without a push for that long to zero (status `stopped`,
  reason `idle-stop`); the next push deploys and starts it again.
  `piceli.envs.env_stop` does the same by hand (planned, `env_hash`).
  Deleting the branch still deletes its environment.

### Environments of a composition

In a composition (see {doc}`components`) an environment follows several
repositories: `follow` maps each `piceli.infra.Source` to its rule,
`Environment("main", namespace="shop-main", follow={shop: "main", catalog:
Tag("v*")}, stack=Stack("full", [web, api]), cluster=cluster)`, and
`Environment.per_branch(Branches("wp-*"), namespace="shop-{branch}",
follow={shop: "{branch}", catalog: "main"}, on_nodes=["node-b"], limit=2)`
declares the branch environments. The controller deploys the environment at
one commit per source (its revision) and renders it from the components'
contracts; `Stack` then holds `Component`s. A single-source
`follow=Branch(...)` keeps its meaning here.

### One place to look

- `piceli envs --pipeline MODULE:ATTR [--json]`: every environment with its
  branch, namespace, commit, build and deploy state, health (from the
  workloads' ready replicas), age and last push, plus the GitOps
  controller's view of each branch when one runs (see {doc}`gitops`).
- `piceli logs wp-login api [--previous] [-f]`: one workload's logs in that
  environment (`kubectl logs` with the explicit context).
- `piceli access wp-login --pipeline MODULE:ATTR`: the declared forwards of
  that environment, each on a free local port (main keeps its declared ports).

`--pipeline` can be set once with `PICELI_PIPELINE`.

## API reference

| Task | Types |
| --- | --- |
| Declare and apply | {py:class}`~piceli.app.environment.Environment`, {py:class}`~piceli.app.environment.Scaling`, {py:meth}`App.environment <piceli.app.app.App.environment>`, {py:meth}`App.for_environment <piceli.app.app.App.for_environment>`, `App.environments`, `App.selected_environment` |
| Pipelines | {py:class}`~piceli.pipeline.model.Pipeline` (`target=` mapping, `for_environment`, `environment`, `needs_environment`) |
| Compare | {py:func}`~piceli.app.environment.environment_diff`, {py:func}`~piceli.app.environment.field_changes` |
| Per-branch environments | `piceli.envs`: `EnvConfig`, `namespace_for`, `env_up`, `env_down`, `env_stop`, `seed_env`, `list_envs`, `EnvStatus` |
| Named environments | `piceli.envs`: `Environment`, `Branch`, `Tag`, `Promote`, `Stack` |
