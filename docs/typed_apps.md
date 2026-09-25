# Describe an app in typed Python

This page shows how to describe an application's Deployments, Services,
configuration, secrets, service accounts with their permissions, pod security
defaults and network policies with typed Python objects, and
how to render them to manifests or release them, with no manifest dicts or YAML.

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

`piceli render` exits with `2` for every rejection. With `--format json` it
also prints `{"state": "rejected", "reason": …, "message": …}` to stdout. The
reason is `render-target-invalid` (the target or the spec could not be loaded)
or `render-model-invalid` (the model failed to render).

## A complete example

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
| `app.deployment(name, …)` | `name` |
| `app.config(name, …)`, `app.secret(name, …)` | `name` |
| `app.service(workload, …)`, `app.network_policy(workload, …)` | the workload's component |
| `app.service_account(name, …)` | `name` (with its Role and ClusterRole objects) |
| `app.network_policy(selector=…, name=…)` | `component=`, else `name` |

Pass `component=` to group objects, as the example does with `config`. A
Deployment automatically depends on the component of every config and secret
of the app that it reads through env or volumes, and of the service account it
is bound to. `app.depends(a, on=b)` adds
other edges. It accepts declared objects or component names.

### Selector labels are chosen once

A Deployment's `spec.selector` is immutable in Kubernetes. Piceli derives it
from the Deployment's own name only:

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

Every Deployment declared on the app renders:

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
- **Only RBAC.** A release refuses other cluster-scoped kinds (Namespace,
  PersistentVolume, CustomResourceDefinition, …) with `invalid-composition`.
  A hand-built ClusterRole or ClusterRoleBinding in a composition is stamped
  with `piceli.io/namespace`; one that names another namespace is refused.
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
| Collect and render an app | {py:class}`~piceli.app.app.App` (`deployment`, `service`, `config`, `secret`, `service_account`, `network_policy`, `release_selector`, `depends`, `add`, `override`, `composition`, `render`) |
| Pod settings | {py:class}`~piceli.app.model.PodDefaults`, {py:class}`~piceli.app.model.Security` |
| Permissions | {py:class}`~piceli.app.model.Rule`, {py:class}`~piceli.app.model.ServiceAccount` |
| Containers | {py:class}`~piceli.app.model.Container`, {py:class}`~piceli.app.model.ContainerPort`, {py:class}`~piceli.app.model.Resources` |
| Health checks | {py:class}`~piceli.app.model.Probe` (`http`, `tcp`, `exec`; also `app.probe`) |
| Environment | `str`, {py:class}`~piceli.app.model.SecretKey`, {py:class}`~piceli.app.model.ConfigKey`, {py:class}`~piceli.app.model.FieldRef` |
| Volumes | {py:class}`~piceli.app.model.ConfigVolume`, {py:class}`~piceli.app.model.SecretVolume`, {py:class}`~piceli.app.model.MemoryVolume`, {py:class}`~piceli.app.model.ExistingClaim`, {py:class}`~piceli.app.model.Mount` |
| Declared objects (returned handles) | {py:class}`~piceli.app.model.Deployment`, {py:class}`~piceli.app.model.Service`, {py:class}`~piceli.app.model.ServicePort`, {py:class}`~piceli.app.model.Config`, {py:class}`~piceli.app.model.Secret`, {py:class}`~piceli.app.model.ServiceAccount`, {py:class}`~piceli.app.model.NetworkPolicy` |

Everything above is importable from `piceli` directly (`from piceli import
App, ExistingClaim`). The exports are lazy, so `import piceli` stays cheap and
has no side effects.

## `piceli render` contract

```text
piceli render [TARGET] [--spec release.toml] [--namespace NS] [--format yaml|json]
```

| | |
| --- | --- |
| `TARGET` | `module:attr` or `path/to/file.py:attr` (dotted `attr` allowed). The attribute can be an `App`, a `DeploymentComposition`, a function of the release context that returns one, or a `Pipeline` (new in 0.5.0). It defaults to the spec's `[release] composition`. |
| `--spec` | A `release.toml` that supplies the namespace, images (receipts are read locally), secret inputs as placeholders, `[values]` and declared nodes. Not accepted with a `Pipeline`. |
| `--namespace` | Overrides the namespace (default: the spec's namespace, otherwise `default`). A `Pipeline` renders into its target's namespace; another value is refused. |
| A `Pipeline` | Renders the app as `piceli deploy` would release it: the target's namespace and declared nodes (`node="alias"` pins resolve, unverified), secret inputs as placeholders, build images as `pipeline.piceli.invalid/<image>:unresolved`, pinned images as they are, and the delivery node's `kubernetes.io/hostname` pin on workloads that use a built image. It reads no kubeconfig, build spec or pipeline state. |
| `--format` | `yaml` (default, multi-document) or `json` (one object). |
| Side effects | Imports the target module and reads the spec and the receipts it names. It never contacts a cluster or reads a kubeconfig, never reads or generates secret values and never writes files. |
| Retry | Always safe. |
| Approval | None. |
| Exit codes | `0` rendered, `2` rejected (`render-target-invalid`, `render-model-invalid`). |

## Not typed yet

StatefulSets, Jobs, Ingress, PodDisruptionBudgets, tolerations and affinity are
not part of `App` yet. In the meantime:

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

- for a whole object, build a `DeploymentComponent` from `ResourceIntent`
  objects (or from the {doc}`templates <kubernetes_model/index>`) and include
  it with `app.add(...)`.

`container=` names the main container when it must not be named after the
Deployment. `piceli import` ({doc}`migrate_from_kubectl`) generates both
forms from live objects.
