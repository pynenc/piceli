# Describe an app in typed Python

This page shows how to describe an application's Deployments, Services,
configuration, secrets and network policies with typed Python objects, and
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
   secrets, and uses `--namespace` (default `default`).

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

Pass `component=` to group objects, as the example does with `config`. A
Deployment automatically depends on the component of every config and secret
of the app that it reads through env or volumes. `app.depends(a, on=b)` adds
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
`[target.nodes.primary]` (`nodeSelector: kubernetes.io/hostname`). `piceli
release` verifies the node against the cluster. `piceli render` uses the name
declared in the spec.

### Access is declared, never rendered

`app.service(web, port=3000, access=app.access.forward(local=18080,
path="/login"))` declares how to reach the Service from a laptop. It adds
nothing to the manifests; `piceli status` and `piceli access` use it (see
{doc}`access`). For them to see it, the composition function returns the App
itself, as `examples/typed_app/app.py` does; a release renders a returned App.

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
| Collect and render an app | {py:class}`~piceli.app.app.App` (`deployment`, `service`, `config`, `secret`, `network_policy`, `depends`, `add`, `composition`, `render`) |
| Containers | {py:class}`~piceli.app.model.Container`, {py:class}`~piceli.app.model.ContainerPort`, {py:class}`~piceli.app.model.Resources` |
| Health checks | {py:class}`~piceli.app.model.Probe` (`http`, `tcp`, `exec`; also `app.probe`) |
| Environment | `str`, {py:class}`~piceli.app.model.SecretKey`, {py:class}`~piceli.app.model.ConfigKey`, {py:class}`~piceli.app.model.FieldRef` |
| Volumes | {py:class}`~piceli.app.model.ConfigVolume`, {py:class}`~piceli.app.model.SecretVolume`, {py:class}`~piceli.app.model.MemoryVolume`, {py:class}`~piceli.app.model.ExistingClaim`, {py:class}`~piceli.app.model.Mount` |
| Declared objects (returned handles) | {py:class}`~piceli.app.model.Deployment`, {py:class}`~piceli.app.model.Service`, {py:class}`~piceli.app.model.ServicePort`, {py:class}`~piceli.app.model.Config`, {py:class}`~piceli.app.model.Secret`, {py:class}`~piceli.app.model.NetworkPolicy` |

Everything above is importable from `piceli` directly (`from piceli import
App, ExistingClaim`). The exports are lazy, so `import piceli` stays cheap and
has no side effects.

## `piceli render` contract

```text
piceli render [TARGET] [--spec release.toml] [--namespace NS] [--format yaml|json]
```

| | |
| --- | --- |
| `TARGET` | `module:attr` or `path/to/file.py:attr` (dotted `attr` allowed). The attribute can be an `App`, a `DeploymentComposition`, or a function of the release context that returns one. It defaults to the spec's `[release] composition`. |
| `--spec` | A `release.toml` that supplies the namespace, images (receipts are read locally), secret inputs as placeholders, `[values]` and declared nodes. |
| `--namespace` | Overrides the namespace (default: the spec's namespace, otherwise `default`). |
| `--format` | `yaml` (default, multi-document) or `json` (one object). |
| Side effects | Imports the target module and reads the spec and the receipts it names. It never contacts a cluster, never reads or generates secret values and never writes files. |
| Retry | Always safe. |
| Approval | None. |
| Exit codes | `0` rendered, `2` rejected (`render-target-invalid`, `render-model-invalid`). |

## Not typed yet

Security contexts, ServiceAccounts and RBAC, StatefulSets, Jobs, Ingress and
PodDisruptionBudgets are not part of `App` yet. In the meantime, build a
`DeploymentComponent` from `ResourceIntent` objects (or from the
{doc}`templates <kubernetes_model/index>`) and include it with `app.add(...)`.
