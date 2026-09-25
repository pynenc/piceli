# Describe an app in typed Python

This page shows how to describe an application's Deployments, StatefulSets,
DaemonSets, Jobs and CronJobs, Services, configuration, secrets, service
accounts with their permissions, pod security defaults, network policies,
autoscalers, disruption budgets, Ingresses and Gateway API HTTPRoutes with
typed Python objects, and
how to render them to manifests or release them, with no manifest dicts or YAML.
Objects of any other kind, custom resources included, are declared with
`app.resource(...)` and a spec generated from their CRD (see {doc}`crds`);
one module can describe dev, staging and prod with typed overrides (see
{doc}`environments`).

```{admonition} Maturity: preview
:class: note

`piceli.app` and `piceli render` are **preview**: they are tested and used by
the release example, but names and defaults may still change before 1.0.
The manifests they render are plain Kubernetes objects and do not depend on
Piceli at runtime.
```

## Prerequisites

- Piceli installed (`pip install piceli`) and Python 3.12 or later.
- To release the app, a `release.toml` as described in {doc}`release_cli`.
  Rendering needs no cluster and no spec.

## Steps

1. **Declare the app.** Create a module with a function of the release
   context. `App(name)` collects declarations, and each declaration is
   validated as soon as you make it. This is `examples/typed_app/app.py`:

   ```{literalinclude} ../examples/typed_app/app.py
   :language: python
   :lines: 8-
   ```

   `ctx` is the release context: `ctx.image(name)` is the image pinned by
   digest in `release.toml`, `ctx.secret(name)` is an opaque reference to a
   generated secret, `ctx.values` holds the public `[values]` table and
   `ctx.nodes` holds the verified nodes.

2. **Render it without a cluster.**

   ```text
   piceli render --spec examples/typed_app/release.toml
   piceli render examples/typed_app/app.py:build --spec examples/typed_app/release.toml --format json
   ```

   The spec supplies the namespace, images, secret inputs (as placeholders),
   values and nodes, and names the composition (`[release] composition =
   "app.py:build"`). Without `--spec`, `piceli render` accepts an `App` object
   (`path/to/app.py:app`) or a composition function that needs no images or
   secrets, and uses `--namespace` (default `default`). An `App` alone knows
   no nodes, so a `node="alias"` pin is refused there; render the `Pipeline`
   instead (`path/to/app.py:pipeline`, see {doc}`deploy`), which uses its
   target's namespace and declared nodes.

3. **Release it.** Run `piceli release plan --spec
   examples/typed_app/release.toml` (after pointing `[target]` at your
   cluster) and apply the plan hash it prints, as described in
   {doc}`release_cli`.

### Expected output

`piceli render` prints one YAML document per object, grouped by component.
Secret data is shown as `<redacted>`:

```yaml
---
# component: api (depends on: cache, cache-password)
apiVersion: apps/v1
kind: Deployment
metadata:
  labels:
    app.kubernetes.io/name: api
    app.kubernetes.io/part-of: shop
  name: api
  namespace: shop
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: api
  …
```

With `--format json`, it prints one object:
`{"state": "rendered", "namespace": …, "components": [{"name", "dependencies",
"resources": [{"manifest", "secret_bindings": [{"pointer", "input"}]}]}]}`.

### If it fails

- **A `ValidationError` at declaration.** The message names the model and
  field, for example `replicas: Input should be greater than or equal to 0`.
  Fix the argument. Unknown keyword arguments are rejected, so a typo
  fails here too.
- **`unknown image 'x'` or `unknown secret input 'x'`.** The function reads an
  image or secret that the spec does not declare. Pass `--spec`, or declare it
  under `[images]` or `[secrets]`.
- **`pinned to node 'gpu', which the target does not declare`.** Add
  `[target.nodes.gpu]` to the spec.
- **`depends() names unknown components`.** A `depends` argument names a
  component that nothing declares.
- **`mounted as an ExistingClaim and must never be managed`.** The same claim
  is also created by the composition, for example by an added template. Remove
  one of them.

`piceli render` exits with `2` for every rejection and prints
`{"state": "rejected", "reason": …, "message": …}` to stdout, whatever
`--format` says (the explanation goes to stderr). The reason is
`render-target-invalid` (the target or the spec could not be loaded) or
`render-model-invalid` (the model failed to render).

## A complete example

{doc}`reference_app` walks through `examples/reference/app.py`: every kind on
this page, a custom resource and a SOPS secret in one module, deployed to
dev, staging and prod with typed environment overrides.

`examples/release/composition.py` is the release example as a typed model.
It renders byte for byte the same resource intents as the dict-based
composition it replaced (a unit test compares the canonical JSON):

```{literalinclude} ../examples/release/composition.py
:language: python
:lines: 14-
```

## Rules the model enforces

### Components and ordering

A release applies components in dependency order and waits for each one to
become ready before the next. Every declaration belongs to a component:

| Declaration | Default component |
| --- | --- |
| `app.deployment(name, …)`, `app.stateful_set`, `app.daemon_set`, `app.job`, `app.cron_job` | `name` (a StatefulSet's headless Service joins it) |
| `app.config(name, …)`, `app.secret(name, …)` | `name` |
| `app.service(workload, …)`, `app.network_policy(workload, …)`, `app.autoscaler(workload, …)`, `app.disruption_budget(workload, …)` | the workload's component |
| `app.ingress(name, …)`, `app.http_route(name, …)` | the first route's Service component, else `name` |
| `app.service_account(name, …)` | `name` (with its Role and ClusterRole objects) |
| `app.network_policy(selector=…, name=…)` | `component=`, else `name` |

Pass `component=` to group objects, as the example does with `config`. A
workload automatically depends on the component of every config and secret
of the app that it reads through env or volumes, and of the service account it
is bound to. `app.depends(a, on=b)` adds
other edges. It accepts declared objects or component names.

Within a release, kinds are also applied in a fixed order unless a dependency
says otherwise: Deployments, StatefulSets and DaemonSets, then Services, then
Jobs and CronJobs, then Ingresses, HTTPRoutes, NetworkPolicies, disruption
budgets and autoscalers. `app.depends(api, on=migrate)` makes a Job run (to
completion) before a Deployment instead.

### Selector labels are chosen once

A workload's `spec.selector` is immutable in Kubernetes. Piceli derives it
from the workload's own name only (a Job's selector is chosen by Kubernetes;
its pods still carry the label):

```text
selector = {"app.kubernetes.io/name": <deployment name>}   # unless selector= is given
```

Renaming the app, moving the Deployment to another component, or changing app
or Deployment labels never changes the selector of an existing Deployment. The
pod labels always include the selector. To change a selector, create a
Deployment with a new name. If an existing Deployment's selector changes
anyway (through an explicit `selector=`), the API server rejects the apply
instead of orphaning pods.

Object labels default to `{"app.kubernetes.io/part-of": <app name>}` on every
object. `App(name, labels={…})` replaces them.

### Existing claims are never managed

`ExistingClaim("name")` mounts a PersistentVolumeClaim that already exists. It
produces **no** resource intent, so no plan can create, change, adopt or delete
the claim, and rendering refuses a composition that also manages a claim with
that name. The claim must exist before the pod starts; otherwise the pod stays
`Pending`. PersistentVolumeClaims that a release does manage are a retained
kind: a release never prunes them either.

### Secret values never reach the model

`app.secret(name, {key: ctx.secret(...)})` accepts only opaque secret
references. The executor resolves them at apply time. Env values reference a
Secret key (`secret.key("password")`). Passing a secret reference or a plain
string as a Secret value is a validation error. Put public values in
`app.config(...)`.

### Nodes come from the target

`node="primary"` pins the pods to the verified node declared under
`[target.nodes.primary]` (`nodeSelector: kubernetes.io/hostname`), or under
`nodes=` of a pipeline's `Target`. `piceli release` and `piceli deploy`
verify the node against the cluster. `piceli render` uses the name declared
in the spec, or in the `Target` when the target is a `Pipeline`.

### Access is declared, never rendered

`app.service(web, port=3000, access=app.access.forward(local=18080,
path="/login"))` declares how to reach the Service from a laptop. It adds
nothing to the manifests; `piceli status` and `piceli access` use it (see
{doc}`access`). For them to see it, the composition function returns the App
itself, as `examples/typed_app/app.py` does; a release renders a returned App.

## Apply pod defaults to every workload

Settings that every pod of an app shares (a non-root user, a seccomp profile,
an extra node selector, a grace period) are declared once on the App:

```python
from piceli import App, PodDefaults, Security

app = App(
    "shop",
    pod_defaults=PodDefaults(
        security=Security.restricted(user=10001, fs_group=10001),
        node_selector={"kubernetes.io/arch": "amd64"},
        termination_grace_seconds=30,
    ),
)
api = app.deployment("api", image=ctx.image("api"))
web = app.deployment(
    "web",
    image=ctx.image("web"),
    node="primary",
    security=Security(read_only_root_filesystem=True),
)
```

Every workload declared on the app (Deployment, StatefulSet, DaemonSet, Job,
CronJob) renders:

| Setting | Rendered as |
| --- | --- |
| `Security` pod fields (`run_as_non_root`, `run_as_user`, `run_as_group`, `fs_group`, `seccomp`) | the pod's `securityContext` |
| `Security` container fields (`allow_privilege_escalation`, `read_only_root_filesystem`, `drop_capabilities`, `add_capabilities`) | the `securityContext` of **every** container, init containers and sidecars included |
| `node_selector` | `nodeSelector`, merged with a `node=` pin (`kubernetes.io/hostname`) |
| `termination_grace_seconds` | `terminationGracePeriodSeconds` |
| `automount_token` | `automountServiceAccountToken` of pods not bound to a declared service account |

`Security.restricted(...)` passes the Kubernetes `restricted` Pod Security
Standard: non-root user and group, the runtime's default seccomp profile, no
privilege escalation and every capability dropped.

Layering, from lowest to highest:

1. `pod_defaults`;
2. the workload's own `security=`, `node_selector=`,
   `termination_grace_seconds=` and `automount_token=`. `security` wins field
   by field (a workload's `Security(run_as_user=2000)` keeps the default
   `fs_group`), `node_selector` key by key, the others as a whole;
3. `app.override(workload, patch)`, applied to the rendered manifest last (a
   `None` in the patch removes a default).

Rules:

- A `node_selector` (from the defaults or the workload) that sets
  `kubernetes.io/hostname` is refused on a workload with `node=`: pin with
  `node=` or select the host by label, not both. The error names the
  workload and where the key came from.
- `Security(run_as_non_root=True, run_as_user=0)` is refused, and a
  `Localhost` seccomp profile needs `seccomp_localhost_profile`.
- Without `pod_defaults` and the new workload arguments, rendering is exactly
  what it was.
- Components added with `app.add(...)` are not changed.

## Give a workload API permissions

A workload that reads the Kubernetes API (a watcher, an operator) needs a
ServiceAccount and RBAC objects. Declare them with typed rules and bind the
account to the workload:

```python
from piceli import Rule

watcher = app.service_account(
    "watcher",
    rules=[Rule(resources=["pods", "pods/log"], verbs=["get", "list", "watch"])],
    cluster_rules=[Rule(resources=["nodes"], verbs=["get", "list"])],
)
app.deployment("watcher", image=ctx.image("watcher"), service_account=watcher)
```

This renders:

| Object | Name | When |
| --- | --- | --- |
| ServiceAccount | `watcher` | always |
| Role and RoleBinding | `watcher` | with `rules` |
| ClusterRole and ClusterRoleBinding | `<namespace>:<app>:watcher`, annotated `piceli.io/namespace: <namespace>` | with `cluster_rules` |

`Rule(resources=…, verbs=…, api_groups=("",), resource_names=())` is checked
when it is declared: `resources`, `verbs` and `api_groups` must not be empty,
verbs and resources must be well-formed, and `resource_names` cannot be
combined with `create` or `deletecollection` (Kubernetes cannot restrict those
by name). A `"*"` anywhere is refused unless the rule says
`allow_wildcard=True`, because a wildcard also grants whatever Kubernetes adds
later.

Tokens: the ServiceAccount renders `automountServiceAccountToken: false`, and
a pod bound to it with `service_account=` renders `true`. Only the pods you
bind get API credentials, even if another pod names the account. A pod bound
to an account the app does not declare (`service_account="name"`) keeps the
Kubernetes default. Set `PodDefaults(automount_token=False)` to keep tokens out
of every pod that is not bound to a declared account, and
`automount_token=` on a workload to decide for that workload alone.

`app.override(watcher, patch)` patches the ServiceAccount; the Role and
binding objects follow from the rules.

### Cluster-scoped objects in a release

A ClusterRole and a ClusterRoleBinding are not in any namespace: every
release in the cluster sees them. `piceli release` handles them like this:

- **Names are unique per namespace.** `<namespace>:<app>:<account>` cannot
  collide between two namespaces (RBAC names accept `:`).
- **Ownership is per namespace.** An object counts as this release's only
  when it carries this release's owner (`piceli.io/owner`) **and**
  `piceli.io/namespace: <release namespace>`. The same owner released in
  another namespace is someone else: its objects are never changed or pruned,
  and one with a conflicting name must be adopted explicitly
  (`--adopt ClusterRole/<name>`, which a takeover restamps).
- **Plans show them.** Each such action carries `"cluster_scoped": true` in
  the JSON plan and `[cluster-scoped]` in the text; approving the plan hash
  approves them like any other action.
- **Prune and rollback clean them up.** With `[release] prune = true`,
  dropping `cluster_rules` (or the account) deletes exactly this release's
  ClusterRole and ClusterRoleBinding; a rollback to a release that had them
  recreates them.
- **RBAC and declared resources.** Besides ClusterRoles and
  ClusterRoleBindings, a release manages other cluster-scoped objects only
  when they carry `piceli.io/namespace` (as `app.resource(...,
  scope="cluster")` renders them, see {doc}`crds`); Namespace,
  PersistentVolume and CustomResourceDefinition are always refused with
  `invalid-composition`. A hand-built ClusterRole or ClusterRoleBinding in a
  composition is stamped with `piceli.io/namespace`; one that names another
  namespace is refused.
- **The deployer needs cluster rights.** Planning lists ClusterRoles and
  ClusterRoleBindings cluster-wide, and applying writes them, so the
  kubeconfig user needs those permissions (and Kubernetes only lets it grant
  permissions it holds). Without them, discovery is incomplete and the plan
  is refused.

## Restrict traffic with network policies

`app.network_policy(workload, allow_from=[…], ports=[…])` protects one
workload's pods. To select pods by label instead, pass `selector=` and a
`name=`; `allow_from_selector=` adds sources by label (one mapping or a list):

```python
app.network_policy(
    selector=app.release_selector,
    allow_from_selector=app.release_selector,
    name="shop-internal",
)
app.network_policy(api, allow_from_selector={"role": "gateway"}, ports=[8080])
```

`app.release_selector` is the set of labels every pod of the app carries (the
app's object labels, `{"app.kubernetes.io/part-of": <app name>}` by default),
so the first policy lets only this app's pods reach its pods. An empty
selector is refused: it would match every pod in the namespace.

## Stateful workloads

`app.stateful_set(...)` takes the same pod and container arguments as
`app.deployment(...)`. Mount a `ClaimTemplate` to give each pod its own
PersistentVolumeClaim:

```python
from piceli import ClaimTemplate

db = app.stateful_set(
    "db",
    image=ctx.image("db"),
    ports=[5432],
    replicas=3,
    volumes={"/var/lib/db": ClaimTemplate("data", size="1Gi")},
    pod_management="Parallel",  # or "OrderedReady" (default)
    update_strategy="RollingUpdate",  # or "OnDelete"
)
```

- **Headless Service.** By default the app also declares a headless Service
  (`clusterIP: None`) named after the StatefulSet (or `service_name=`) that
  selects its pods and exposes the main container's ports (named
  `port-<number>` when there are several); `serviceName` points at it, so
  each pod is reachable as `<pod>.<service>`. Pass `headless=False` to point
  `service_name=` at a Service declared elsewhere, or to have none. Declare a
  regular Service in front of it with `app.service(db, port=…, name=…)`.
- **Claims are never pruned.** The claims come from the StatefulSet
  controller (`<template>-<name>-<ordinal>`), not from the release: no plan
  creates, changes, adopts or deletes them. The StatefulSet renders
  `persistentVolumeClaimRetentionPolicy: {whenDeleted: Retain, whenScaled:
  Retain}`, and a release deletes (prune) or replaces a StatefulSet with
  `Orphan` propagation, so the claims and their data outlive it; a later
  release (or a rollback) that declares the StatefulSet again reattaches its
  pods to the same claims. Delete claims yourself when the data is no longer
  needed.
- **Immutable fields.** `service_name`, `pod_management`, the selector and
  the claim templates cannot change on an existing StatefulSet (see
  [Immutable fields](#immutable-fields-and-replace)). A `ClaimTemplate` is
  only accepted on a StatefulSet.

## Jobs, CronJobs and DaemonSets

```python
migrate = app.job(
    "migrate",
    image=ctx.image("api"),
    command=["migrate"],
    backoff_limit=2,
    active_deadline_seconds=600,
)
api = app.deployment("api", image=ctx.image("api"), ports=[8080])
app.depends(api, on=migrate)  # run the migration before the API rolls out

app.cron_job(
    "report",
    schedule="0 3 * * *",
    time_zone="Etc/UTC",
    image=ctx.image("api"),
    command=["report"],
    concurrency="Forbid",
)
app.daemon_set(
    "agent", image=ctx.image("agent"), node_selector={"kubernetes.io/os": "linux"}
)
```

- A release waits until a **Job** completes (a failing Job times out the
  release). `restart_policy` is `Never` (default) or `OnFailure`. A Job has no
  `selector=`: Kubernetes chooses it. With `ttl_seconds_after_finished`, the
  Job is deleted after it finishes and the next release creates (and runs) it
  again.
- A **CronJob** is ready as soon as it exists. The Jobs it creates belong to
  it and are never managed or pruned by a release. Every field may change;
  new Jobs use the new template.
- A **DaemonSet** runs one pod on every node that matches its node selector
  (and the app's `pod_defaults.node_selector`); a release waits until every
  scheduled pod is ready and updated.

## Settings shared by every pod kind

Deployments, StatefulSets, DaemonSets, Jobs and CronJobs share one pod model
({py:class}`~piceli.app.model.Workload`), so these work the same for all of
them:

- `pod_defaults` (security, extra node selector, grace period, token
  automounting), layered under the workload's own arguments;
- `service_account=` (a declared account gives the pods a token and its
  permissions);
- `node=` pins, images from `ctx.image(...)` or a pipeline's build handles
  (the pipeline pins workloads that use a node-delivered image to that node,
  whatever their kind);
- env and volumes from configs and secrets, with the automatic component
  dependencies;
- `app.override(workload, patch)`, `app.service(workload, …)` and
  `app.network_policy(workload, …)`.

Workload names are unique across kinds: a Job and a Deployment with the same
name would share the `app.kubernetes.io/name` pod label.

## Autoscale a workload

```python
from piceli import Resources

api = app.deployment(
    "api",
    image=ctx.image("api"),
    ports=[8080],
    resources=Resources(cpu="100m", memory="128Mi"),
)
app.autoscaler(api, min_replicas=2, max_replicas=10, cpu=70)
```

`app.autoscaler(workload, …)` declares a HorizontalPodAutoscaler
(`autoscaling/v2`) for a Deployment or StatefulSet. `cpu` and `memory` are
target average utilizations in percent of the requests, so every container
of the workload must request that resource (checked when declared).

**Replicas rule: the autoscaler owns the replica count.**

- The workload must not set `replicas=` (refused when declared). It renders
  the autoscaler's `min_replicas` as `spec.replicas`, which is only its
  initial size: a new workload starts at the minimum.
- Once the workload exists, the plan declares the live count while Piceli
  still owns the field (`held`), and leaves the field out once the HPA has
  scaled it (`yielded`); it never removes `/spec/replicas`. A release
  therefore never resets the HPA's count, and a workload that had `replicas`
  in an earlier release keeps its live count.
- The HPA's changes are not a difference: the next plan is a no-op.
  `release plan --json` reports the mode under `autoscaled`.

The same rule applies to an HPA in plain manifests or created by another
tool; see {doc}`compatibility`.

Only one autoscaler per workload. Metrics need a metrics server in the
cluster; without one the HPA still enforces `min_replicas`. An environment
changes the bounds with `autoscalers={"api": Scaling(min_replicas=3,
max_replicas=20)}` (see {doc}`environments`).

## Limit disruptions

```python
app.disruption_budget(api, max_unavailable=1)
app.disruption_budget(db, min_available="50%", name="db-budget")
```

A PodDisruptionBudget (`policy/v1`) selects the workload's pods (a
Deployment, StatefulSet or DaemonSet). Pass exactly one of `min_available`
and `max_unavailable`, as a pod count or a percentage;
`unhealthy_pod_eviction=` sets `unhealthyPodEvictionPolicy`.

## Route HTTP traffic

A `Route(service, path)` is one path to a Service port. With a declared
Service handle the port is filled in (and checked); a Service the app does not
declare needs `port=`.

```python
from piceli import GatewayRef, Route

web_service = app.service(web, port=80, target_port=3000)
app.ingress(
    "shop",
    hosts=["shop.example.com"],
    class_name="nginx",
    tls_secret="shop-tls",
    routes=[Route(web_service, "/"), Route(api_service, "/api", port="http")],
)
app.http_route(
    "shop",
    gateway=GatewayRef("public", namespace="gateways", section="https"),
    hosts=["shop.example.com"],
    routes=[Route(web_service, "/"), Route(api_service, "/api", port=8080)],
)
```

- `app.ingress` renders a `networking.k8s.io/v1` Ingress; every host serves
  every route, and no hosts means any host. It needs an ingress controller to
  carry traffic, not to be released.
- `app.http_route` renders a Gateway API `HTTPRoute`
  (`gateway.networking.k8s.io/v1`), one rule per route, attached to one or
  more Gateways (a name in the release namespace, or a `GatewayRef`).
  Backend ports are numbers. **The Gateway API CRDs must be installed in the
  cluster**; they are not part of Kubernetes. Rendering needs nothing, but
  without the CRDs a plan is refused because the kind cannot be discovered.
  Install them from a pinned release, for example
  `kubectl apply --server-side -f
  https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.2.1/standard-install.yaml`.
- A release waits only until an Ingress or HTTPRoute exists, not until a
  controller accepts it.

(immutable-fields-and-replace)=
## Immutable fields and replace

Some fields never change on an existing object: a Job's pod template and
`completions`, and a StatefulSet's `serviceName`, `podManagementPolicy`,
selector and claim templates. Piceli never works around this implicitly.
When a composition changes one of them, `piceli release plan` refuses with
`immutable-field-changed` and names the object:

```text
rejected: Job/migrate: immutable fields would change (spec.template); the API server refuses the update [immutable-field-changed]
  blocking Job/migrate: … -> --replace Job/migrate
```

Plan again with `--replace Job/migrate` (or `[release] replace`) to delete the
object and create it from the release, after a restorable backup (see
{doc}`release_cli`). A replaced Job's pods are deleted with it and the new Job
runs; a replaced StatefulSet is deleted with `Orphan` propagation, so its
pods keep running until the new StatefulSet adopts (and, for a changed
template, rolls) them, and its claims are kept. An existing claim keeps its
size: a changed claim template applies only to new claims.

## Reusing templates

`app.add(...)` includes a component built elsewhere: a `DeploymentComponent`,
or a template with a `component(namespace)` method such as
{doc}`NodeLocalRegistry <node_local_registry>`:

```python
app.add(templates.NodeLocalRegistry(node_name=ctx.nodes["primary"].name))
app.depends(web, on="registry")
```

## API reference

Grouped by task. Every class is a frozen pydantic model with its parameters
documented in the API docs.

| Task | Types |
| --- | --- |
| Collect and render an app | {py:class}`~piceli.app.app.App` (`deployment`, `stateful_set`, `daemon_set`, `job`, `cron_job`, `service`, `config`, `secret`, `service_account`, `network_policy`, `autoscaler`, `disruption_budget`, `ingress`, `http_route`, `resource`, `release_selector`, `depends`, `add`, `override`, `environment`, `for_environment`, `composition`, `render`) |
| Any other kind, custom resources ({doc}`crds`) | {py:class}`~piceli.app.resource.Resource`, `piceli codegen crd` |
| Environments ({doc}`environments`) | {py:class}`~piceli.app.environment.Environment` |
| Pod settings | {py:class}`~piceli.app.model.PodDefaults`, {py:class}`~piceli.app.model.Security` |
| Permissions | {py:class}`~piceli.app.model.Rule`, {py:class}`~piceli.app.model.ServiceAccount` |
| Containers | {py:class}`~piceli.app.model.Container`, {py:class}`~piceli.app.model.ContainerPort`, {py:class}`~piceli.app.model.Resources` |
| Health checks | {py:class}`~piceli.app.model.Probe` (`http`, `tcp`, `exec`; also `app.probe`) |
| Environment | `str`, {py:class}`~piceli.app.model.SecretKey`, {py:class}`~piceli.app.model.ConfigKey`, {py:class}`~piceli.app.model.FieldRef` |
| Volumes | {py:class}`~piceli.app.model.ConfigVolume`, {py:class}`~piceli.app.model.SecretVolume`, {py:class}`~piceli.app.model.MemoryVolume`, {py:class}`~piceli.app.model.ExistingClaim`, {py:class}`~piceli.app.model.ClaimTemplate`, {py:class}`~piceli.app.model.Mount` |
| HTTP routing | {py:class}`~piceli.app.kinds.Route`, {py:class}`~piceli.app.kinds.GatewayRef` |
| Declared objects (returned handles) | {py:class}`~piceli.app.model.Deployment`, {py:class}`~piceli.app.kinds.StatefulSet`, {py:class}`~piceli.app.kinds.DaemonSet`, {py:class}`~piceli.app.kinds.Job`, {py:class}`~piceli.app.kinds.CronJob` (all {py:class}`~piceli.app.model.Workload`), {py:class}`~piceli.app.model.Service`, {py:class}`~piceli.app.model.ServicePort`, {py:class}`~piceli.app.model.Config`, {py:class}`~piceli.app.model.Secret`, {py:class}`~piceli.app.model.ServiceAccount`, {py:class}`~piceli.app.model.NetworkPolicy`, {py:class}`~piceli.app.kinds.Autoscaler`, {py:class}`~piceli.app.kinds.DisruptionBudget`, {py:class}`~piceli.app.kinds.Ingress`, {py:class}`~piceli.app.kinds.HttpRoute` |

Everything above is importable from `piceli` directly (`from piceli import
App, ExistingClaim`). The exports are lazy, so `import piceli` stays cheap and
has no side effects.

## `piceli render` contract

```text
piceli render [TARGET] [--spec release.toml] [--namespace NS] [--format yaml|json]
              [--env NAME [--diff-env OTHER]] [--out DIR [--secrets refuse|external]]
```

| | |
| --- | --- |
| `TARGET` | `module:attr` or `path/to/file.py:attr` (dotted `attr` allowed). The attribute can be an `App`, a `DeploymentComposition`, a function of the release context that returns one, or a `Pipeline` (new in 0.5.0). It defaults to the spec's `[release] composition`. |
| `--spec` | A `release.toml` that supplies the namespace, images (receipts are read locally), secret inputs as placeholders, `[values]` and declared nodes. Not accepted with a `Pipeline`. |
| `--namespace` | Overrides the namespace (default: the spec's namespace, otherwise `default`). A `Pipeline` renders into its target's namespace; another value is refused. |
| A `Pipeline` | Renders the app as `piceli deploy` would release it: the target's namespace and declared nodes (`node="alias"` pins resolve, unverified), secret inputs as placeholders, build images as `pipeline.piceli.invalid/<image>:unresolved`, pinned images as they are, and the delivery node's `kubernetes.io/hostname` pin on workloads that use a built image. It reads no kubeconfig, build spec or pipeline state. |
| `--format` | `yaml` (default, multi-document) or `json` (one object). |
| `--env` | Renders one environment of the App (new in 0.7.0, see {doc}`environments`); a `Pipeline` also uses its target for it. JSON output adds `"environment"`. |
| `--diff-env` | With `--env`: prints the typed difference between the two environments instead of manifests (text, or one `{"state": "diffed", …}` object with `--format json`). |
| `--out DIR` | Writes one YAML file per object into `DIR` instead of printing, and prints one `{"state": "written", …}` object (new in the next release, see {doc}`gitops`). |
| `--secrets` | With `--out`: `refuse` (default) a Secret object, or `external` to leave Secrets out because they are provided outside the files. |
| Side effects | Imports the target module and reads the spec and the receipts it names. It never contacts a cluster or reads a kubeconfig, never reads or generates secret values and writes files only with `--out DIR` (one YAML file per object for a Git directory; see {doc}`gitops`). |
| Retry | Always safe. |
| Approval | None. |
| Exit codes | `0` rendered, `2` rejected (`render-target-invalid`, `render-model-invalid`, `environment-unknown`, `environment-required`, `environment-invalid`, `environment-unsupported`, and with `--out` `render-out-refused` or a `gitops-*` code). |

## Not typed yet

Tolerations, affinity and topology spread constraints are not part of `App`
yet, nor are other kinds (custom resources, other Gateway API routes). In the
meantime:

- declare a whole object of any kind with `app.resource(api_version, kind,
  name, spec)` ({doc}`crds`): a typed spec generated from a CRD, or a JSON
  mapping;

- set a field of a declared object with `app.override(obj, patch)`: the patch
  is merged into the rendered manifest (mappings key by key, `None` removes a
  key, lists of objects with a unique `name` merge on `name`, other values
  replace). It cannot change the object's identity or a Secret's data;

  ```python
  api = app.deployment("api", image=ctx.image("api"), container="server")
  app.override(
      api,
      {
          "spec": {
              "template": {
                  "spec": {
                      "tolerations": [{"key": "dedicated", "operator": "Exists"}],
                  }
              }
          }
      },
  )
  ```

- for a whole component built elsewhere, build a `DeploymentComponent` from
  `ResourceIntent` objects (or from the {doc}`templates <kubernetes_model/index>`)
  and include it with `app.add(...)`.

`container=` names the main container when it must not be named after the
Deployment. `piceli import` ({doc}`migrate_from_kubectl`) generates both
forms from live objects.
