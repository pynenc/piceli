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

## API reference

| Task | Types |
| --- | --- |
| Declare and apply | {py:class}`~piceli.app.environment.Environment`, {py:class}`~piceli.app.environment.Scaling`, {py:meth}`App.environment <piceli.app.app.App.environment>`, {py:meth}`App.for_environment <piceli.app.app.App.for_environment>`, `App.environments`, `App.selected_environment` |
| Pipelines | {py:class}`~piceli.pipeline.model.Pipeline` (`target=` mapping, `for_environment`, `environment`, `needs_environment`) |
| Compare | {py:func}`~piceli.app.environment.environment_diff`, {py:func}`~piceli.app.environment.field_changes` |
