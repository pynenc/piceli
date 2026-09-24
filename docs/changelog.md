# Changelog

The changelog documents the history of changes and version releases for Piceli.

For detailed information on each version, please visit the [Piceli GitHub Releases page](https://github.com/pynenc/piceli/releases).

## Unreleased

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
