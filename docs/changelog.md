# Changelog

The changelog documents the history of changes and version releases for Piceli.

For detailed information on each version, please visit the [Piceli GitHub Releases page](https://github.com/pynenc/piceli/releases).

## Version 0.5.0

- **Build smoke checks match output and take an environment**
  (experimental): a smoke table (`build.toml`, and the new `Smoke` for
  `Build.dockerfile(..., smoke={...})`) adds `env` (plain, non-secret
  values), `entrypoint` (overrides the image's entrypoint) and
  `expect_stdout`/`expect_stderr` (regular expressions searched in the first
  256 KiB of each stream). A pattern that is not found fails the build with
  `smoke-output-mismatch` (exit `1`); the step lists the streams under
  `unmatched`, and stderr and the build log show a short escaped excerpt.
  Smoke env values that look like secret references are refused
  (`smoke-env-secret`). The smoke table is part of the plan hash and is
  recorded in the receipt under `smoke`. Existing smoke checks keep their
  argv, isolation flags and spec digest.

## Version 0.4.1

- **Fix (safety):** Piceli signals a child's process group only when the id
  is a real child group: never `1`, `0` or its own group. A test double's
  `pid` (which converts to `1`) made the forward supervisor send SIGTERM to
  process group 1, which on Linux CI stopped the job itself.
- **Release commands for pipeline releases:** every `piceli release`
  subcommand (`plan`, `preview`, `diff`, `apply`, `rollback`, `resume`,
  `stop`, `check`, `status`, `secret show`) now takes `--spec MODULE:ATTR`
  naming a `Pipeline`, as `piceli status` and `piceli access` do, and
  operates the release `piceli deploy` manages: same state directory,
  release name, target and composition. Nothing is built: a rollback
  re-applies the recorded image digests (`oci-set`), and `plan`/`diff`/`apply`
  use the images delivered from the current sources or refuse with the new
  code `pipeline-not-delivered`. Commands that change the cluster hold the
  pipeline's run lock (`pipeline-locked`).
- **Exec credential plugins for pipelines:** `Target.kubeconfig(...)` takes
  `allow_exec`, `exec_sha256`, `exec_pass_env` and `exec_timeout_seconds`
  with the semantics of `[target]` in `release.toml`. They apply to
  `piceli deploy`, `piceli status`, `piceli access` and the release commands.
  `piceli status` and `piceli access` now also honour `[target] allow_exec`
  of a `release.toml`, and refuse an exec user without it
  (`exec-auth-not-allowed`); post-deploy checks use the target's policy.
- **Fix:** checks declared on a `Pipeline` (`Checks.http`, `exec`, `metric`,
  `python`) now run with a `piceli.checks.CheckContext` built from the
  target; in 0.4.0 the default runner got the pipeline's context and every
  check failed with `check-raised`.
- **Fix:** `piceli deploy --resume` of a run that failed at its checks stage
  with `rollback_on_failed_checks` no longer fails with
  `pipeline-stage-error`; it rolls back to the previous release.
- **Fix:** `piceli deploy` prints the build log as a path relative to the
  working directory or `<state_dir>/…`, never an absolute local path.
- **Fix:** `piceli status` and `piceli access --dashboard` with a pipeline
  target now read the pipeline's release state: `status` shows the release
  line and the dashboard its catalog (active release, managed workloads).
  The human `status` workload columns line up.
- **Fix (output contract):** a pipeline's automatic rollback reports
  `{"state": "rejected", "reason": "<code>", "message": …}` or
  `{"state": "unavailable", "reason": "checks-rollback-unavailable",
  "message": …}` instead of `"state": "refused"` or a free-text `reason`. A
  release execution refused by the executor is recorded in the history as
  `"state": "rejected"` with `"reason": "execution-refused"` (was
  `"refused"`).
- **`piceli --version`** prints `piceli <version>`; the top-level help has a
  real description.
- **Saved forwards never start implicitly (safety):** `piceli operator serve`
  and `piceli observe serve` no longer start a user's saved port forwards at
  launch. `--restore-forwards` starts them, and only those saved for the same
  cluster (a digest of the context's API server URL), context and namespace.
  Forwards saved without a scope (older files, or `forward-save` without the
  new `--kubeconfig/--context`) are never restored. Forwards added from the
  dashboard are saved with their scope, and `GET /v1/preferences` lists only
  the current user's forwards for this cluster, context and namespace.
- **`piceli status` only reports forwards Piceli owns:** a forward is `up`
  only when the listener is Piceli's `kubectl port-forward` for that
  declaration. Any other process on the port is `occupied`
  (`status-port-occupied`) with its pid only; its command line is never
  printed. The dashboard's supervisor also refuses to call a forward healthy
  when another process answers on its port (`conflict`).
- **`operator serve --access` starts the model's forwards** at launch, like
  `piceli access --dashboard`, refusing with `access-port-conflict` when a
  required port is taken. `--no-start-access` leaves them stopped.
- **Operator dashboard:** Pods and ReplicaSets owned (through
  `ownerReferences`) by a managed workload are listed as managed with
  `derived_from` instead of unmanaged. Image references are shortened with the
  full reference in a tooltip, and the tables keep their columns inside the
  card.
- **Readable plans and progress:** `release plan` shortens long values in the
  middle (keeping the digest tail and the closing quote) and puts long changes
  on their own lines; `piceli deploy --plan` wraps long change lists and
  prints the approve command on its own line. `release apply` and the deploy
  apply stage print progress on stderr while applying and waiting for
  readiness (`applying 3/7: Deployment/web`, `waiting for Deployment/web to
  be ready (12s)`); stdout is unchanged.
- Human output shortens delivered image digests (`@sha256:<12 hex>…`) and
  drops microseconds from the `piceli status` release time; JSON output keeps
  the full values.

## Version 0.4.0

- **Removed:** the legacy delete-and-recreate engine and its CLI
  (`piceli model …`, `piceli deploy plan/detail/run`). There is one engine:
  `piceli release`, driven by typed apps or a `release.toml`. `piceli deploy`
  is now the pipeline command below.
- **`piceli deploy` (preview):** a `piceli.pipeline.Pipeline` declares the
  app, its image builds, how the images reach the cluster (registry,
  node-loopback registry or node import) and the target. `piceli deploy
  module.py:pipeline` builds, delivers by digest, plans, applies and checks in
  one journaled run. Unchanged stages are skipped, an interrupted run resumes,
  approval covers the combined hash of every stage, and `--json` streams one
  event per stage. A multi-image release records its source as an `oci-set`
  image map.
- **Post-deploy checks and automatic rollback (preview):** `[[checks]]` in
  `release.toml` (or `piceli.checks` in Python) declares `http`, `exec`,
  `metric` and `python` checks. A release is `ready` only when they pass;
  with `rollback_on_failed_checks = true` a failed check re-applies the
  previous ready release. New `piceli release check` and `--skip-checks`.
  A failed check ends `apply` with `reason: check-failed` and exit 3.
- **Field-level diffs and true no-op plans:** `release plan` compares the
  desired object with a server-side dry run of it instead of with the raw
  live object, so API-server defaults no longer show up as changes. Unchanged
  objects plan `no-op`, and every `apply` shows the fields it changes.
  New `piceli release diff`.
- **Secret no-op plans and three-way removal:** an unchanged Secret (or
  other secret-bound object) now plans `no-op`; its resolved values are
  compared in-process with keyed digests and are never shown or stored. An
  `apply` now removes labels, annotations and map keys (such as ConfigMap
  keys) that an earlier release declared and the composition dropped, unless
  another field manager owns them (`removes` in the plan, `remove` changes in
  the diff).
- **Access and status from the model (preview):** `app.access.forward(…)`
  declares how each service is reached from your laptop (it renders no
  Kubernetes object). `piceli access TARGET` runs supervised loopback port
  forwards, and `piceli status TARGET [--json]` (`piceli.status.v1`) says
  whether the app is up and lists its URLs.
- **Import existing apps (preview):** `piceli import live` (a namespace) and
  `piceli import yaml` (manifest files) generate a typed app module that
  adopts the existing objects on its first release.
  `App.override(obj, patch)` sets fields the typed model does not cover.
- **`piceli.testing` (public):** the in-process fake Kubernetes API used by
  Piceli's own acceptance suite, with a `fake_cluster()` context manager and a
  pytest fixture, for testing your deployment code without a cluster.
- **Managed clusters (preview):** kubeconfigs that authenticate through an
  exec credential plugin (GKE, EKS, AKS, OIDC) are refused
  (`exec-auth-not-allowed`) unless `[target] allow_exec = true`. The plugin is
  resolved to an absolute path, hashed before every run (optional
  `exec_sha256` pin) and run with a minimal environment
  (`exec_pass_env`) and a timeout; Piceli refreshes the credential itself.
- **Changed (machine output):** every preview command now follows the output
  contract: a refusal is `{"state": "rejected", "reason": "<code>",
  "message": "<sentence>"}` on stdout, and exit codes are unchanged. See
  {ref}`the contract changes <agents-contract-changes>` for each command.
  Release refusals keep `code` as an alias of `reason` for 0.4.x only; it
  will be removed in 0.5.0. Results of `release apply/rollback/resume/stop`
  add `state` (`succeeded`, or `failed` with `reason`).
- **Changed (safety):** `observe` and `operator` commands need an explicit
  `--context`, build their clients through the same checks as `release`
  (a refused target is `target-refused`) and keep static client certificates
  in memory instead of temporary files.
- **Fixed:** the `rust-hello` build example could ship a stale binary after
  a source edit: staged sources carry the fixed `source_date_epoch` mtime, so
  Cargo treated the cached `target/` artifact as fresh. The example now runs
  `cargo clean --release --package rust-hello` before `cargo build`, keeping
  dependencies cached and builds reproducible. {doc}`containerized_builds`
  documents the pitfall.
- **Fixed:** `operator status`, `observe` and release plan observation no
  longer fail on RBAC objects whose names contain `:` (such as
  `system:controller:*` in `kube-system`). RBAC names are validated as
  Kubernetes path segments, and live objects Piceli cannot model are skipped
  with a scan warning instead of crashing.
- **Fixed:** `exec` checks failed TLS verification (`check-exec-unavailable`)
  with clients built from an explicit kubeconfig; they now use the client's
  own verified TLS context and credentials.
- `Checks` is exported from the top-level package
  (`from piceli import Checks, Pipeline`).
- Help text shows `[[…]]` TOML names literally instead of dropping them as
  terminal markup.
- Two flaky tests fixed (git identity under redirected `GIT_DIR`, process
  limit timing).

## Version 0.3.0

- **Safety fix:** the dry-run admission check before a delete really deleted
  the object, because the API server ignores a `dryRun` query parameter when a
  DeleteOptions body is sent. `dryRun` is now sent in the body. This affected
  opt-in pruning in 0.1.0 and 0.2.0: objects that were about to be deleted
  anyway lost the check-before-write guarantee.
- **Ownership transitions (preview):**
  - `piceli release --replace KIND/NAME` / `[release] replace` deletes and
    recreates an unmanaged, non-retained object after writing a restorable
    backup (`kubectl create -f`). It always needs a per-object flag.
  - `--adopt-all-desired` adopts every unmanaged object the composition
    declares.
  - Plan refusals list every blocking object with suggested flags and codes.
    The human text of each blocking entry is in its `message` field.
  - Retained objects, including those with inherited owners, whose only
    difference is labels or annotations get a metadata-only write.
- **Immutable image references (preview):**
  - A build receipt without a registry digest is refused with
    `image-not-immutable` instead of falling back to its movable tag.
  - `[images.<name>] receipt` and `images_from` accept
    `piceli.node-delivery.v1` receipts, which need a content tag
    `repo:sha256-<12hex>`.
  - `images_from` takes a list of build and delivery receipts, merged by config
    digest.
  - New `examples/two-images`, with an opt-in kind acceptance test.

- **Typed apps (preview):** `from piceli import App, ExistingClaim, …`
  describes Deployments (sidecars, init containers, probes, resources,
  memory/config/secret volumes, node pinning), Services, ConfigMaps, Secrets
  and NetworkPolicies as typed Python that renders to release intents.
  - `ExistingClaim` mounts a claim that the release never creates, changes or
    deletes.
  - Selectors derive only from the Deployment name.
  - `piceli render` prints the manifests without a cluster.
  - `examples/release` is now typed and renders identically.
- **Secrets (preview):** new `tls-ca`, `template`, `import` (file, env, or a
  live Secret; rotatable with `--rotate`) and `static` generators, and
  `piceli release secret show NAME [--key] --reveal`. A refused
  `release plan` no longer generates, imports or stores any secret version.
- **Builds:**
  - Drift is checked over the staged files and the spec. Whole-source identity
    is kept as provenance (`sources_changed_during_build`), so edits to
    unrelated files no longer reject a build.
  - `--log` streams during the build, and `--progress steps|plain|quiet`
    shows step progress.
  - `tag = "{image_id:N}"` content tags.
  - Optional per-image `smoke` checks run in an isolated container.
  - `piceli inputs record|verify --only NAME`.
- **Agent and contract foundations (preview):**
  - a registry of error codes with `piceli explain <code> [--json]`;
  - `piceli help-json` / `--help-json` (the CLI tree with side effects,
    approval and retry metadata);
  - generated `reference/errors` and `reference/cli` pages;
  - `AGENTS.md`, `llms.txt` and `docs/agents.md`;
  - maturity labels on every feature page;
  - a clear error for a misplaced `images_from`.

## Version 0.2.0

- `release.toml` images can point at a registry delivery receipt:
  `web = { receipt = "web.delivery.json" }` releases the receipt's
  digest-pinned `pull_ref`. This chains build → delivery → release without
  copying digests by hand.

- Adopt existing objects by ownership transfer. `piceli release --adopt
  Kind/name` (or `[release] adopt = [...]`, or
  `PlanAuthorization.adopt_resources`) adopts objects in one of two ways:
  - retained objects (PVC, Secret, Namespace, PV) with an owner-annotation-only
    write; their spec and data are never touched;
  - other objects with an explicitly authorized takeover. Field ownership of
    every client manager (for example `kubectl-create`, `kubectl-set`) is
    transferred to Piceli and the manifest is applied without force, so fields
    the release does not declare are removed. Subresource and control-plane
    managers are kept, and `force=true` is only used for the admission dry run.

  Plans show the adoption mode and the displaced managers, and report field
  drift. Inherited owners are now part of the execution grant.
- Receipts only compare declared fields, which fixes the false drift on the
  first apply of a WaitForFirstConsumer PVC.
- Boolean `*Token` fields such as `automountServiceAccountToken` are no longer
  redacted as secrets.

- `templates.NodeLocalRegistry` is a digest-pinned OCI registry bound to the
  node loopback, so the node pulls from it without any registry configuration
  and it is not exposed on the network. It has:
  - retained storage, delete support and loopback probes;
  - a garbage-collection Job that is safe to run in stopped or read-only mode;
  - a `pull_reference()` helper, and a `component()` for `piceli release`
    compositions.

  See {doc}`node_local_registry`.
- `piceli artifacts deliver --to oci://host[:port]/repo[:tag]` is now the
  default delivery mode:
  - it pushes an image approved by config digest and uploads only the missing
    blobs, chunked where needed;
  - it is idempotent, and it re-verifies the pushed manifest;
  - its receipt (`piceli.registry-delivery.v1`) records the manifest digest and
    a node-side `pull_ref`;
  - `--via-forward` pushes through a supervised loopback port-forward.

  Plain HTTP is only allowed for loopback registries, and credentials come from
  a private file. Node import over `ssh://` / `docker://` remains as the
  fallback.

## Version 0.1.0

- `piceli release {plan,preview,apply,rollback,resume,stop,status} --spec release.toml`
  is the first CLI on the recoverable engine. The typed release spec takes an
  explicit kubeconfig file and context, optional cluster/namespace/node UID
  pins, and images by digest or from a `piceli.build-receipt.v1` receipt. A
  `module:function` composition builds the resources, and `random` and
  `tls-self-signed` secret generators are available. Plans need a one-shot
  approval by plan hash, and `rollback` re-applies the earlier release.
  `piceli.k8s.ops.provider_factory` builds an identity-checked
  `KubernetesProvider` from an explicit kubeconfig. See {doc}`release_cli`.
- Declarative containerized builds: `piceli artifacts build-spec preview|run`
  and `piceli.artifacts.build_spec`. Builder images are pinned by digest,
  build contexts are staged minimally with byte budgets, and BuildKit cache
  mounts are used. Outputs can be files or images. A `piceli.build-receipt.v1`
  receipt binds the source identities, builder digest and output digests. Image
  IDs are reproducible across cold builds. Includes a `rust-hello` linux/arm64
  example. See {doc}`containerized_builds`.
- `piceli artifacts deliver` streams an image (`docker save`, or a Docker/OCI
  archive) straight into a node's containerd over `ssh://` (k3s or plain
  containerd) or `docker://` (e.g. kind nodes), with no registry and no
  temporary archive. The approved config digest is re-verified from the stream
  before the image index is released, and again on the node. The command is
  idempotent and writes a JSON receipt and journal. See {doc}`node_delivery`.
- Access profiles with connection health. Shortcuts in the `--ui-config` TOML
  can declare a TCP or HTTP health probe and a bounded restart policy. The
  port-forward supervisor restarts a forward after consecutive probe failures,
  with exponential backoff. It reports `health`, `last_error` and
  `last_probe_at` in `/v1/forwards`, `/v1/shortcuts` and the dashboard. New
  commands: `piceli observe forwards apply|status`, with a port-conflict
  preflight, and `observe serve --start-shortcuts`.
- Added `piceli inputs record|verify` and the source-identity API in
  `piceli.artifacts` (`SourceIdentity`, `InputsLock`, `pinned_sources`). Build
  sources are identified by git commit, dirty flag and a working-tree digest,
  not by unversioned local receipt files. A checkout that changes during a
  build fails. See {doc}`source_identity`.
- **Tooling:** migrated from Poetry to [uv](https://docs.astral.sh/uv/) with
  PEP 621 metadata and the hatchling build backend. Python 3.12+ is required.
  ruff replaces black and isort. CI runs on Python 3.12–3.14 including the
  acceptance suite, runs integration tests on kind, and builds the docs strictly.
  Releases use PyPI trusted publishing.
- **Packaging:** the Google Cloud dependencies moved to the `gcp` extra
  (`pip install "piceli[gcp]"`) and OpenTelemetry to the `telemetry` extra.
  PyYAML is now a declared dependency. The unused `textual` dependency was
  removed. `typer` ≥ 0.12 is required.

- **Safety:** resource ownership is now an exact match on the owner annotation.
  Previously, any owner id sharing the prefix before the first `-` was treated
  as the same owner, so pruning could remove another owner's objects. To take
  over objects from an earlier owner id, pass
  `KubernetesProvider(..., inherited_owner_ids=("old-id",))` or adopt them
  explicitly.
- **Fixed:** `Deployment` templates are now found by the loader, and
  `--module-path` / `PICELI__MODULE_PATH` now executes the module (before, it
  always loaded nothing). `StatefulSet`, `HorizontalPodAutoscaler` and
  `VerticalPodAutoscaler` manifests now carry `apiVersion`/`kind`. `Service`,
  `PersistentVolume` and `PersistentVolumeClaim` return lists like every other
  template. Workloads without `template_labels` default to `{"app": <name>}`.
  The ineffective 15-character name limit was replaced by the real Kubernetes
  rules.
- **Fixed:** the CLI engine no longer silently skips kinds it doesn't know. They
  are deployed last, with a warning.
- **Security:** GKE service-account credentials are built in memory. No
  `sa.json` file is written and `GOOGLE_APPLICATION_CREDENTIALS` is left
  untouched.
- **Security (local web UI):**
  - loopback `Host` and same-origin checks;
  - a token is required on every `/v1/*` request, GET included;
  - the `viewer` role is read-only;
  - Content-Security-Policy with a nonce, and no inline event handlers;
  - a 1 MiB request limit;
  - error bodies contain fixed error codes only.
- **Changed:** the dashboard ships no application-specific shortcuts or topology.
  Configure them with `--ui-config` / `PICELI__UI_CONFIG` (TOML).
  `observe serve` gains `--namespace`.
- **Fixed:** `piceli operator serve` no longer crashes on start, and handles an
  occupied port and signals like `observe serve`.
- Reworked the documentation: new landing page, overview and architecture guide
  (mental model, the two execution engines, glossary), public roadmap, a real FAQ,
  an open contributing guide, full CLI reference for `observe`, `operator` and
  `artifacts`, and a warning-free Sphinx build. Examples no longer reference a
  specific environment.

- Added the read-only local operations lens: Python inventory API, JSON CLI,
  loopback REST endpoints, and owner-only non-secret port-forward preferences.
  It reconciles an explicit deployment-session archive with an explicit
  kubeconfig and distinguishes declared, missing, unknown, and undeclared
  objects without acquiring deployment authority.

- Added `DeploymentSession`, a Python-first provider-free boundary for one-time
  private input materialization, canonical opaque-reference interchange, exact
  preview/apply/resume, explicit rotation and owner-scoped stop.
- Added canonical `DeploymentRevision` and `ExecutionBundle` interchange for
  exact, secret-safe resume. Retained owned resources reconcile without a new
  SSA operation annotation; PVC first-consumer execution defers only readiness,
  never treats Pending claims as ready.
- Added public, deterministic build/OCI APIs and a thin `piceli artifacts` CLI
  with source/tool pins, bounded process groups, cancellation, secret-safe
  receipts and explicit local Docker import without tags or push.
- Added bounded OTLP deployment-operation spans/logs with exact journal states
  and signal-level drop/rejection/unknown-delivery accounting. Joined local
  acceptance covers the real recoverable executor, fault API and telemetry consumer restart.

- Replaced discovery authority v1 with validated v2 coverage, scope, timestamps,
  provenance and capture-time byte/deadline limits; retained historical v1 files.
- Added an explicit-client Kubernetes provider and scoped executor with private
  versioned secret bindings, durable intent/receipts, readiness, cancellation,
  resume and ownership-limited compensation. Qualified the provider against a
  fault-injecting local API, including process death and ambiguous replies.
- Added `make local-test-env` and `make test-local-executor`, hash-locked test
  dependencies, retained acceptance evidence and an executable composition example.
  Removed the unavailable exact Python interpreter pin from pre-commit bootstrap.

- Added immutable deployment components and target-bound observed snapshots,
  deterministic offline create/adopt/apply/no-op/delete plans, UID/version and
  absence preconditions, secret-safe hashes, retained-resource protection and
  child-first pruning.
- Switched `piceli deploy plan` to the pure planner and added tests proving that
  import and planning neither create Kubernetes clients nor mutate input intent.
- Required explicit cluster binding and resource adoption, and reject stale
  snapshots, wrong namespaces and unsafe unmanaged descendants.
- Published bounded portable discovery schema v1 and a pure provider protocol
  covering continuation, API scope, coverage failures, SSA conflicts and readiness.
- Bound snapshots to discovery/defaulting identity, preserved explicit desired
  defaults, blocked absence/pruning under incomplete coverage, and made built-in
  Namespace/PV/PVC/Secret retention non-overridable.

## Version 0.0.4

- First version of the docs

## Version 0.0.3

- CLI
- Integration tests
- Github pipelines

## Version 0.0.2

- MVP
- automatic deployment
- deploymnet graph and automatic dependencies
- automatic rollback
- comparison functionalities, adding ignoring/default paths

## Version 0.0.1

- Refactor legacy libs
- Adding support for kubernetes official python library
- Adding support for yaml and json
- Unit tests for piceli templates
