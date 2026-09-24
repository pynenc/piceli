# Overview and Architecture

Piceli manages Kubernetes infrastructure as Python code. This page covers the
mental model, the main building blocks and the vocabulary used in the rest of the
documentation.

## The mental model

Every Piceli workflow goes through four stages:

```text
  MODEL                 PLAN                    EXECUTE                 OBSERVE
  ─────                 ────                    ───────                 ───────
  templates,            desired state vs.       ordered, authorized     live inventory,
  k8s client objects,   observed cluster  ──►   writes with a      ──►  logs, forwards,
  YAML / JSON           state → actions         durable journal         releases
```

1. **Model**: declare the resources you want. Use Piceli templates (typed Pydantic
   models with sensible defaults), official `kubernetes.client` objects, or YAML/JSON
   manifests. All three can be mixed in one project.
2. **Plan**: compare the model with what the cluster reports. The result is a
   list of `create`, `adopt`, `apply`, `no-op` and `delete` actions, grouped in
   dependency order (for example ServiceAccounts and Secrets before the Deployments
   that use them).
3. **Execute**: apply the plan level by level and wait for readiness between levels.
4. **Observe**: compare what was declared with what is running, read logs, and
   reach services through local port forwards.

## Two execution engines

Piceli currently ships two implementations of *plan* and *execute*. Knowing which
one you are using avoids surprises.

| | CLI engine | Recoverable engine |
| --- | --- | --- |
| Entry point | `piceli deploy detail` / `piceli deploy run` | `DeploymentSession`, `PlanExecutor` (Python API) |
| Input | Templates, client objects, YAML/JSON loaded from a module or folder | `DeploymentComposition` of `ResourceIntent` manifests |
| Cluster access | The current kubeconfig context | An explicit `ApiClient` and explicit target identity |
| Update strategy | Deletes and recreates existing objects | Server-side apply with UID/resourceVersion preconditions |
| Ordering | Fixed level per kind; kinds outside the table are skipped | Kind levels plus explicit component dependencies |
| Failure handling | Rolls back to the previous objects | Durable SQLite journal: cancel, resume and conservative compensation |
| Pruning | No | Opt-in, and only with complete discovery coverage |
| Secrets | Inline in the manifest | Private versioned store; plans and journals hold only references |

The CLI engine is the quickest way to try Piceli against a disposable cluster.
The recoverable engine is the long-term foundation. The {doc}`roadmap` describes
how the CLI will be moved onto it.

```{warning}
Because the CLI engine replaces existing objects, running `piceli deploy run`
against a namespace with live traffic causes a brief outage for every changed
object. Use `piceli deploy detail` first, and prefer the recoverable engine for
anything long-lived.
```

## Building blocks

### Model

- **Templates** (`piceli.k8s.templates`): `Deployment`, `StatefulSet`, `Job`,
  `CronJob`, `Service`, `ConfigMap`, `Secret`, `ServiceAccount`, `Role`,
  `RoleBinding`, autoscalers and volumes, plus helpers such as `Container`,
  `Port`, `Resources` and `crontab`. See {doc}`kubernetes_model/piceli_templates/index`.
- **Loader**: finds every template, `kubernetes.client` model and YAML/JSON document
  under a module or folder (`--module-name`, `--module-path`, `--folder-path`).

### Plan (recoverable engine)

- **`ResourceIntent`**: one immutable desired manifest. `with_secret()` binds a
  JSON pointer inside it to a private secret version.
- **`DeploymentComponent`**: a named group of intents plus the names of the
  components it depends on.
- **`DeploymentComposition`**: the full desired state, i.e. all components.
- **Discovery** (`capture_discovery`): reads the target namespace within strict
  time and size limits and records coverage. Incomplete coverage can never
  justify deleting anything.
- **`ObservedSnapshot`**: the discovered cluster state, bound to cluster and
  namespace UIDs.
- **`build_plan`**: a pure function from composition + snapshot + authorization
  to a deterministic, hashable plan. It has no side effects and makes no network
  calls.

### Execute (recoverable engine)

- **`KubernetesProvider`**: the only component that talks to the API server. It is
  constructed explicitly from a caller-supplied `ApiClient`.
- **`ExecutionAuthorization`**: an explicit, expiring grant that ties a plan to
  one target, owner and field manager, and lists exactly which actions may run.
- **`PlanExecutor`**: runs a plan one action at a time. Before each write it runs
  a dry-run admission check and verifies the target identity.
- **`ExecutionJournal`**: a private SQLite log. It records intent before each
  network call and a receipt after it, which makes cancel and resume safe.
- **`SecretVersionStore`**: an owner-only local store for private values. Plans,
  journals and reports only contain opaque references.

### Durable workflow

- **`DeploymentSession`**: the recommended entry point. It combines composition,
  snapshot, authorization, journal and secret store into one recoverable unit
  with `preview()`, `apply()`, `resume()` and `stop()`.
- **`DeploymentRevision` / `ExecutionBundle`**: canonical JSON identities used for
  exact resume after a crash.
- **`ReleaseCatalog` / `ReleaseWorkflow`**: named releases, each bound to an
  immutable source (Git commit, source digest or OCI digest), with select and
  revert.

### Observe and operate

- **`piceli observe`**: reconciles a session archive with a live namespace and
  reports declared, missing and undeclared objects. Also bounded logs and saved
  loopback port forwards.
- **`piceli operator`**: inventory classification, release promotion, approvals,
  and state backup/restore, with a local web UI.
- **`piceli artifacts`**: deterministic OCI image layouts built from pinned
  sources, with explicit local import. It never pushes images.

## Design principles

1. **No side effects on import.** Importing Piceli never reads kubeconfig, creates
   clients or contacts a cluster.
2. **Planning is pure.** A plan depends only on its inputs, and its hash can be
   reviewed before anything runs.
3. **Authority is explicit.** Writes need a grant that names the target, owner
   and the exact actions. Existing unmanaged objects are only changed after an
   explicit adoption.
4. **Fail closed.** Incomplete discovery, ambiguous replies, identity changes and
   ownership conflicts stop execution instead of guessing.
5. **Data is retained by default.** Namespaces, PersistentVolumes,
   PersistentVolumeClaims and Secrets are never pruned.
6. **The same capability everywhere.** The aim is for each operation to be
   available from the Python library, the CLI, the JSON/REST API and the web UI.

## Glossary

| Term | Meaning |
| --- | --- |
| Adopt | Take ownership of an existing object that Piceli did not create. Requires an explicit grant. |
| Compensation | Reversing Piceli's own changes after a failed run. Only objects Piceli still owns are affected. |
| Coverage | Which resource types discovery listed completely. It determines what the plan may treat as absent. |
| Field manager | The server-side apply identity Piceli writes with. |
| Owner | The Piceli annotation that marks an object as managed by a given deployment. |
| Retained | A resource that is never deleted, either because of its kind or the `piceli.io/retained` annotation. |
| Session archive | Canonical JSON describing a deployment session. It is private operational metadata. |
