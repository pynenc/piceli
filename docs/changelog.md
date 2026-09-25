# Changelog

The changelog documents the history of changes and version releases for Piceli.

For detailed information on each version, please visit the [Piceli GitHub Releases page](https://github.com/pynenc/piceli/releases).

## Unreleased

- **GitOps handoff (preview):** `piceli publish MODULE:ATTR [--env E] --to
  oci://registry/repo[:tag]` packages the rendered manifests as an OCI
  artifact in the `flux push artifact` layout (config
  `application/vnd.cncf.flux.config.v1+json`, one
  `application/vnd.cncf.flux.content.v1.tar+gzip` layer), with a
  deterministic digest; it prints the digest and exits 3, and pushes by
  digest (then the tag) only with `--approve <digest>`. `piceli render
  --out DIR` writes the same files for a Git directory. A Secret is refused
  unless `--secrets external` leaves Secrets out; redacted values and
  placeholder images are always refused. New codes `gitops-*` and
  `render-out-refused`. Flux and Argo CD examples in `docs/gitops.md`; a kind
  test has Flux reconcile a published artifact.

## Version 0.7.0

- **Reference app:** `examples/reference/app.py` deploys a realistic app to
  dev, staging and prod from one typed module (StatefulSet with claim
  templates, Job and CronJob, HPA and PDB, HTTPRoute, a cert-manager
  `Certificate` typed by `piceli codegen crd`, a release-wide NetworkPolicy,
  RBAC, restricted pods, a SOPS-encrypted password and checks), with render,
  fake-API and kind tests; walkthrough in `docs/reference_app.md`.
- Environments: `autoscalers={name: Scaling(min_replicas=…, max_replicas=…,
  cpu=…, memory=…)}` changes an autoscaler per environment (the `replicas=`
  refusal pointed at a fix no override could express), and a `resources=`
  override that drops a request an autoscaler's utilization target needs is
  refused (`environment-invalid`).
- `--diff-env` no longer reports the namespace inside RBAC objects (binding
  subjects, `<namespace>:<app>:<name>` ClusterRole names) as a difference.
- `Checks.exec(...)` with a StatefulSet or DaemonSet handle targets
  `statefulset/<name>` or `daemonset/<name>` instead of `deployment/<name>`;
  a Job or CronJob handle is refused.
- An environment's `replicas=` on a workload an autoscaler targets is refused
  (`environment-invalid`) instead of being ignored.

- **External secret sources (preview):** three new `[secrets.*]` types, and
  `Sops`, `Vault` and `AwsSecret` for `piceli.pipeline.Secrets`: `sops` (one
  value of a SOPS-encrypted file, or the whole file, decrypted by the `sops`
  binary with an explicit argv, a minimal environment plus `pass_env`, a
  timeout and an optional `sops_sha256` pin), `vault` (one key of a HashiCorp
  Vault KV v2 secret over verified TLS, token from `token_file` or
  `token_env`, `namespace`, `version`, `ca_file`) and `aws-secrets-manager`
  (a secret or one JSON key, through botocore; new extra `piceli[aws]`). They
  are read at every `plan` and `diff`; each value is reduced to an HMAC-SHA256
  under a private key (`state_dir/secret-sources.key`) that is part of the
  release fingerprint, so an unchanged value re-plans the same release and a
  changed one creates a new release with a new private version (new plan
  origin `fetched`; the others are `carried:<release>`). A refused plan stores
  nothing, and values never reach plans, journals, receipts, logs or errors.
  `--rotate` refuses an external source (rotate it at the source). New codes
  `secret-source-auth-failed`, `secret-source-not-found`,
  `secret-source-tool-missing`, `secret-source-timeout` and
  `secret-source-failed`. Specs without external sources keep their release
  names; `secret show --json` adds `source` for them.
- **Custom resources and any other kind (preview):** `app.resource(api_version,
  kind, name, spec, *, fields=, scope=, public=, labels=, annotations=,
  component=)` declares one object of any kind with a typed spec (a pydantic
  model, validated at declaration) or a JSON mapping; it renders by alias with
  only the fields that were set, joins components, `depends` and `override`
  like other declarations. Cluster-scoped resources follow the per-namespace
  ownership rules of ClusterRoles (no namespace, `piceli.io/namespace`
  annotation, never another namespace's objects); a release now manages any
  such annotated cluster-scoped kind except Namespace,
  CustomResourceDefinition and PersistentVolume. A declared scope that the
  server's discovery contradicts is refused at plan time
  (`resource-scope-mismatch`). See `docs/crds.md`.
- **`piceli codegen crd` (preview):** generates a deterministic module of
  frozen pydantic models (`<Kind>Spec` with `API_VERSION`, `KIND`, `SCOPE`)
  from a CRD's structural OpenAPI v3 schema, from a file or with
  `--from-cluster --kubeconfig F --context C --crd NAME`; `--out` replaces
  only files it generated. A small in-house generator (no new dependency; it
  understands `x-kubernetes-int-or-string` and preserve-unknown-fields). New
  codes: `crd-invalid`, `crd-not-found`, `codegen-flags-conflict`,
  `codegen-output-refused`, `codegen-cluster-read-failed`. Pinned
  cert-manager `Certificate` and Prometheus-operator `ServiceMonitor` CRDs are
  vendored in `tests/fixtures/crds/` with their source and licence.
- **Environments (preview):** `app.environment(name, ...)` declares typed
  overrides (replicas, images, container resources, config values, hosts,
  node selectors, resource specs, enabled components) checked against the
  declared objects; `app.for_environment(name)` returns the derived App.
  `piceli render --env NAME` renders one environment and `--diff-env OTHER`
  prints their typed difference (text, or JSON with `--format json`).
  `Pipeline(app, {"dev": Target…, "prod": Target…})` deploys each environment
  to its own target with its own state (`state_dir/environments/NAME`);
  `piceli deploy --env` and `piceli release … --spec MODULE:ATTR --env` select
  one, and the combined hash covers the environment's name and resolved
  values. JSON adds `environment` (render, deploy result) only with `--env`.
  New codes: `environment-unknown`, `environment-required`,
  `environment-invalid`, `environment-unsupported`. See `docs/environments.md`.
- Plans show Secret *references* in any kind (`secretName`, `*File`/`*Path`
  strings, `{name, key}` selectors, `secretTemplate`) instead of redacting
  them, so custom resources that reference Secrets can be applied; the
  `piceli.io/public-fields` annotation (`app.resource(..., public=[...])`)
  declares other sensitive-looking fields public. Values stay redacted.
- A file target (`piceli render path/app.py:app`, a pipeline, a release
  composition file) can import the modules next to it (such as generated CRD
  models); its directory is appended to `sys.path`.
- `piceli` and `piceli.app` export `Environment` and `Resource`.
- **Typed App kinds (preview):** `app.stateful_set(...)` (per-pod
  `ClaimTemplate` volumes, a governing headless Service by default,
  `pod_management`, `update_strategy`), `app.daemon_set(...)`, `app.job(...)`,
  `app.cron_job(...)`, `app.autoscaler(workload, ...)` (HPA `autoscaling/v2`),
  `app.disruption_budget(workload, ...)` (PDB `policy/v1`), `app.ingress(...)`
  and Gateway API `app.http_route(...)` with typed `Route` and `GatewayRef`.
  Every pod kind shares the Deployment's pod model (`piceli.app.Workload`):
  `pod_defaults`, `service_account=`, `node=` pins, images, secrets, config
  dependencies and `override` work the same. Workload names are now unique
  across kinds. `Service(headless=True)` renders `clusterIP: None`.
- **Autoscaled replicas, one rule:** a workload targeted by `app.autoscaler`
  refuses `replicas=` and renders the autoscaler's `min_replicas` as its
  initial `spec.replicas`; the plan's autoscaled-replicas rule of 0.6.0
  (`initial`, `held`, `yielded`) decides what is written for typed apps and
  plain manifests alike, and a plan never removes `/spec/replicas` from a
  workload an autoscaler targets, whether the autoscaler is in the release
  or only live. See {doc}`compatibility`.
- **Claims are never pruned:** StatefulSets render
  `persistentVolumeClaimRetentionPolicy` `Retain`/`Retain`, and prune and
  replace delete them with `Orphan` propagation; the claims their templates
  create are never part of a plan.
- **Immutable fields:** a plan that would change a managed Job's pod template
  or `completions`, or a StatefulSet's `serviceName`, `podManagementPolicy`,
  selector or claim templates, is refused with the new code
  `immutable-field-changed` (`blocking[].suggest` names the flag).
  `--replace Kind/name` now also accepts a **managed** Job or StatefulSet and
  recreates it (Jobs with `Background`, StatefulSets with `Orphan`
  propagation); every other managed object is still refused.
- **Readiness, one rule:** HorizontalPodAutoscalers, PodDisruptionBudgets,
  Ingresses and HTTPRoutes are ready once they exist (they need metrics or
  another controller; in 0.6.0 they followed the status conventions); every
  other kind without a specific rule, custom or built in, follows the status
  conventions of 0.6.0 (`observedGeneration`, `Ready`, `Reconciling`,
  `Stalled`; ready once applied when it reports none). See
  {ref}`readiness-rules`. HTTPRoutes are applied with Ingresses, after
  Services.
- Portable plan files (`deploy --plan --out`) record `--env`, and `--apply`
  deploys that environment (`deploy-plan-file-mismatch` for another `--env`).
- `piceli.testing`: the fake API serves StatefulSets, DaemonSets, Jobs,
  CronJobs, HorizontalPodAutoscalers, PodDisruptionBudgets, Ingresses and
  HTTPRoutes (added to `TYPES`), reports their readiness, and refuses updates
  to immutable Job and StatefulSet fields with `422`.
## Version 0.6.0

- **Fix:** reads the API server throttles with 429 (for example while the
  watch cache of a just-installed CRD initializes) are retried after
  `Retry-After` (bounded, within the deadline); writes are never retried.

- **Shared deployment state (preview):** `Pipeline(state="cluster")` and
  `[release] state = "cluster"` keep the run journal, receipts, release
  catalog, execution journal and secret store in the release namespace
  (gzip snapshot in `piceli.io/state` Secrets, chunked, never in ConfigMaps),
  and `state_dir` becomes a working copy. Every command holds a
  release-scoped lock, a `Lease piceli-lock-<release>` renewed by the holder,
  taken over when its holder stopped renewing (`state_lease_seconds`,
  default 60), and fenced: each state write proves the holder and
  `leaseTransitions` first. State is written at every journaled stage change
  and after every execution journal commit, before the change it records, so
  a deploy interrupted or killed on one runner resumes on another. The first
  `state="cluster"` run moves an existing local state to the cluster. The
  default stays `local`. See {doc}`state`.
- **Portable approved plans:** `piceli deploy … --plan --out FILE` writes a
  `piceli.deploy-plan-file.v1` document (combined hash, stages, `--ref`
  commits, pipeline and observed target identity, build/delivery/mirror
  receipts); `piceli deploy --apply FILE --approve HASH` applies it on any
  runner: it refuses another hash, pipeline or cluster, plans again against
  live state and runs only when the combined hash is unchanged. A build whose
  images are already delivered by digest is not rebuilt, so no build cache is
  needed; a resumed run whose build happened on another runner rebuilds
  before delivering.
- **`piceli state show|pull|export|import`:** where a release's state lives,
  who holds its lock, refresh the working copy, export it to one file (secret
  material excluded, or AES-256-GCM encrypted with `--include-secrets
  --key-file`; new extra `piceli[crypto]`) and import it back after
  approving the import digest (exit `3` without `--approve`).
- `pipeline-locked` now covers the release lock and adds `lock` (`holder`,
  `expires_in`) to the rejection; the deploy result adds `plan_file`. New
  codes: `release-locked`, `state-lock-lost`, `state-unavailable`,
  `state-access-denied`, `state-corrupt`, `state-layout-mismatch`,
  `state-too-large`, `state-export-invalid`, `state-key-required`,
  `state-crypto-unavailable`, `state-import-partial`, `state-import-changed`,
  `state-output-exists`, `deploy-plan-file-invalid`,
  `deploy-plan-file-mismatch`, `deploy-plan-target-mismatch`.
- `piceli release plan|check --spec MODULE:ATTR` now hold the pipeline's run
  lock like `apply` (refused with `pipeline-locked` while a deploy runs);
  `release status|diff|secret show` and `piceli status` refresh a shared
  state's working copy first (reads only).
- Discovery lists with the label selector `!piceli.io/state`, so plans,
  pruning and `piceli import live` never see the state objects.
  `piceli.testing` serves Leases and equality/existence label selectors.
- The CI recipe (`examples/ci/github-actions-deploy.yml`, {doc}`ci`) runs
  plan, apply and resume on any runner: no persistent state directory, the
  plan file travels as the `deploy-plan` artifact.
- **Supported Kubernetes versions:** the four most recent minors, 1.34 to
  1.37, each tested on kind with node images pinned by digest
  (`.github/kind-nodes.json`, kind v0.33.0). Pull requests run the
  integration suite on the newest; a nightly job runs it on all four and the
  unit and acceptance tests with the lowest allowed `kubernetes` client
  (`>=29.0.0`). CI now passes the cluster to the integration tests
  explicitly, so they run instead of being skipped. See {doc}`compatibility`.
- **Autoscalers own `spec.replicas`:** for a workload a
  HorizontalPodAutoscaler targets (declared in the release, or live and
  discovered), plans no longer declare `spec.replicas` once the autoscaler
  owns it, and declare the live value while Piceli still does. Before, a
  re-plan showed a perpetual `replicas` diff and drift, and applying it reset
  the count and failed with `applied-resource-drift`. `release plan --json`
  and `release diff` (JSON on stdout) add `autoscaled` (mode `initial`, `held` or
  `yielded` per workload).
- **Controllers writing status during a plan or an apply** (found by the
  kind matrix, most often on 1.37): a status update between discovery and a
  server dry run made the dry run conflict, and an unchanged release planned
  `apply` from a literal comparison; `plan`, `diff` and `rollback` now
  capture discovery and the dry runs again (up to three times) when a dry
  run conflicts. A status update between the executor's read and its merge
  patch failed the apply with `conflict`; the patch is now sent again at the
  new `resourceVersion` when only the status or bookkeeping changed (content
  and field ownership unchanged). An autoscaler scaling a workload through
  the `scale` subresource while Piceli waits for readiness is no longer
  `applied-resource-drift`.
- **Readiness of other kinds:** HorizontalPodAutoscalers, custom resources,
  PodDisruptionBudgets and other kinds without a dedicated rule follow the
  common status conventions (`observedGeneration`, `Ready`, `Reconciling`,
  `Stalled`) and are ready once written when they have none. Before, a
  release containing any of them failed with `readiness-unsupported`, which
  now only means a malformed `status`.
- **Interrupted executions:** `release resume` after a kill before a write
  reached the cluster (a create whose object is absent, a write or delete
  whose object still has the recorded version) sends the write again, once
  the new `[execution] write_settle_seconds` (default 60) have passed since
  it was sent, instead of stopping with `ambiguous-write-blocked`,
  `ambiguous-content-blocked` or `ambiguous-delete-blocked` for good.
  Kill tests stop `release apply` and `release rollback` at every write
  (before it, after the server applied it, at the next request) and on kind
  mid-rollout; {doc}`plans_and_diffs` documents the recovery and the rollback
  boundary (what a rollback restores, and that it cannot restore data,
  external side effects or what others own).
- **Ownership tests on kind:** an autoscaler owning `replicas`, an
  operator-like writer sharing a custom resource, a mutating admission
  webhook, and adoption and pruning with a third field manager.
- `piceli.testing.FakeAPI` adds `scale()` (an autoscaler's `scale`
  subresource write) and `intercept` (stop a client at an exact request
  phase).

## Version 0.5.1

- **Fix:** stopping a finished build step no longer fails intermittently on
  macOS with `PermissionError` when only the exited process-group leader is
  left.

- **Every command follows the output contract:** `render`, `release diff` and
  `release check` are now `conforms` (none is `partial`). `piceli render`
  prints its rejection object on stdout for every `--format` (it did only with
  `--format json`), and a missing `--spec` file is a `render-target-invalid`
  rejection instead of a usage error. `release check` adds `"state":
  "succeeded"`, or `"state": "failed"` with `"reason": "check-failed"` when it
  exits `1`. `release diff --exit-code` adds `"reason":
  "release-changes-pending"` (new code) when it exits `1`. Existing fields are
  unchanged.
- **Retry-safe releases:** the release workflow asks PyPI whether the version
  is released instead of trusting the git tag, uploads only what is missing
  (retrying transient index failures), verifies every file on PyPI and only
  then pushes the tag, which is never moved. An interrupted release finishes
  on re-run. TestPyPI pre-releases from pull requests are retried and
  verified the same way.
- **Provenance:** wheels and sdists are published with PEP 740 attestations
  (Sigstore, trusted publishing); see `SECURITY.md`.

## Version 0.5.0

- **Mirror third-party images (preview):** `NodeLoopbackRegistry(mirror=[…])`
  and `Registry(url, mirror=[…])` copy digest-pinned images the app does not
  build (`docker.io/library/redis@sha256:…`) into the delivery registry in the
  deliver stage, over the OCI distribution API on the machine running Piceli
  (anonymous, or `mirror_credentials={"registry": "file.json"}`). The app's
  references are rewritten to the copy with the same digest, and workloads
  using it are pinned to the registry node. For a multi-arch index the index
  keeps its digest and the node registry holds only the node's platform
  (`NodeLocalRegistry(index_platforms=…)`); `Registry` copies every platform.
  Copies are verified by digest, skipped when present, recorded in
  `state_dir/mirrors/` (`piceli.mirror-delivery.v1`) and part of the combined
  plan hash. New codes: `pipeline-mirror-not-pinned`, `pipeline-mirror-failed`,
  `mirror-digest-mismatch`, `mirror-manifest-invalid`,
  `mirror-platform-unavailable`, `blob-not-found`, `invalid-blob-redirect`,
  `too-many-redirects`. `piceli release plan|diff|apply --spec` refuse with
  `pipeline-not-delivered` until the mirrors are copied.
- **Take over a live node-loopback registry (preview):**
  `NodeLoopbackRegistry(adopt="NAME")` adopts a registry Deployment that
  already runs on the node (and its ConfigMap and claim) by ownership transfer,
  keeping its selector and its data, after checking that it is compatible
  (host network, port, node, storage), else `pipeline-registry-incompatible`.
  `replace="NAME"` backs it up, deletes and recreates it; storage is never
  deleted, and the plan says whether the data carries over. A registry that
  holds the port without either flag is refused at plan time with
  `pipeline-registry-takeover-required` instead of failing later with
  `pipeline-registry-not-ready`. New options `host_path=`, `existing_claim=`
  and `inherited_owners=` on `NodeLoopbackRegistry`, and `existing_claim=`,
  `selector=` on `NodeLocalRegistry`; new code `pipeline-registry-unreadable`.
- `StreamedOciRegistryClient` gains `open_blob()` (streamed blob downloads
  that follow a redirect to another origin without credentials) and
  `actions="pull"` for pull-only token scopes. `piceli.testing.FakeAPI` serves
  Nodes (`add_node`).
- The deploy plan's `deliver` stage adds `mirrors` and `registry.existing` /
  `registry.index_platforms` only when used, so earlier pipelines keep their
  combined hashes.
- **Deploy a commit, not the working tree (preview):**
  `piceli deploy TARGET --ref [SOURCE=]REV` (repeatable; a bare `REV` when
  every source is one repository) resolves each revision to its commit SHA,
  checks it out in a temporary `git worktree` and runs the `inputs` and
  `build` stages from there; the worktrees are removed on success, failure,
  `Ctrl-C` and `SIGTERM`. The combined hash covers the SHAs, so an approval
  of commit X never applies commit Y, and the printed approval command pins
  them. The pipeline module still runs from the working tree and must match
  the commit (`deploy-ref-model-differs`). `--resume` reuses the run's
  commits. New error codes `deploy-ref-invalid`, `deploy-ref-source-unknown`,
  `deploy-ref-ambiguous`, `deploy-ref-unknown`, `deploy-ref-checkout-failed`
  and `deploy-ref-model-differs`. Additive output: `refs` in the deploy
  result, `stages.inputs.refs`/`model`, the run journal and the build
  receipt; a release created by a deploy records `provenance.sources`
  (commit, dirty, ref), shown by `piceli release status`.
- **CI recipe (preview):** {doc}`ci` and `examples/ci/github-actions-deploy.yml`:
  plan the pushed commit, publish the plan and its combined hash, approve
  through a protected GitHub environment, apply with the same `--ref` and
  hash, resume by hand. Piceli's tests run the workflow's commands against
  the fake API.
- **App-level pod defaults (preview):** `App(..., pod_defaults=PodDefaults(...))`
  applies typed settings to every Deployment of the app: `security=Security(...)`
  (pod `securityContext`: `run_as_non_root`, `run_as_user`, `run_as_group`,
  `fs_group`, `seccomp`; container `securityContext` on every container:
  `allow_privilege_escalation`, `read_only_root_filesystem`,
  `drop_capabilities`, `add_capabilities`; `Security.restricted(...)` for the
  `restricted` Pod Security Standard), an extra `node_selector` merged with
  `node=` pins, `termination_grace_seconds` and `automount_token`. Workloads
  take the same typed arguments, which win (`security` field by field,
  `node_selector` key by key); `app.override` still patches last. A
  `kubernetes.io/hostname` selector together with `node=` is refused.
  Rendering is unchanged when none of this is used.
- **Typed RBAC (preview):** `app.service_account(name, rules=[Rule(...)],
  cluster_rules=[Rule(...)])` renders a ServiceAccount, a Role and
  RoleBinding, and a ClusterRole and ClusterRoleBinding named
  `<namespace>:<app>:<name>`; bind it with `app.deployment(...,
  service_account=sa)`. `Rule` refuses empty lists, malformed verbs, resources
  and groups, `resource_names` with `create`/`deletecollection`, and `"*"`
  unless `allow_wildcard=True`. The ServiceAccount renders
  `automountServiceAccountToken: false` and only pods bound to it get `true`.
- **Cluster-scoped RBAC in releases:** a release may now manage ClusterRole
  and ClusterRoleBinding objects (other cluster-scoped kinds are still
  refused with `invalid-composition`). They carry `piceli.io/namespace`, and
  a provider treats a cluster-scoped object as managed only when it names the
  release's namespace as well as its owner, so one owner's releases in two
  namespaces never change, adopt or prune each other's. Plans flag them with
  `"cluster_scoped": true` (additive) and `[cluster-scoped]`; prune and
  rollback create and delete them like namespaced objects.
- **Label-selected network policies:** `app.network_policy(selector=...,
  allow_from_selector=..., name=...)` and `app.release_selector` (the labels
  every pod of the app carries) express "only this app's pods may connect".
- `--adopt`/`--replace` and `[release] adopt`/`replace` accept RBAC names with
  `:` (`ClusterRole/staging:shop:watcher`).
- **Fix:** a pruning delete no longer fails with
  `deleted-resource-reappeared` on a real cluster. An `Orphan` delete keeps
  the object (with a `deletionTimestamp`) until the garbage collector removes
  its finalizer; the executor now waits for the same object to disappear, and
  still refuses an object recreated with another UID.
- `piceli.testing`: the fake API also serves ServiceAccount, Role,
  RoleBinding, ClusterRole and ClusterRoleBinding, decodes percent-encoded
  names, and `FakeAPI.terminating_reads` keeps an `Orphan`-deleted object
  terminating for a few reads, like a real API server.
- **Release preview before the images exist (`piceli deploy --plan`,
  preview):** while build images are not built or delivered, `--plan` now
  computes the release plan with placeholder images
  (`pending-build.piceli.invalid/<image>@sha256:000…` or `pending-delivery…`)
  instead of printing only "after delivery". It shows what the release would
  create, adopt, replace, apply or delete, and refuses with the release
  engine's `blocking` list and suggested flags (before any build or registry
  write) when existing objects need adoption or replacement. The preview is
  never approvable or persisted, generates or reads no secret, and objects
  carrying a placeholder are never sent to the cluster, not even as a server
  dry run (`dry-run-placeholder-image`). JSON adds `stages.plan.preview`, and
  a refused preview adds `stage` and `preview`; existing fields keep their
  meaning. The combined hash of a pending plan now covers the preview's
  adopt/replace/delete set: after delivery the real release plan may not go
  beyond it (`pipeline-preview-changed`, nothing applied; plan again).
  `--approve <preview_hash>` is refused with
  `pipeline-preview-not-approvable`. An image that is neither a build handle
  nor pinned by digest is now refused at `--plan` time.
- **`piceli render MODULE:pipeline`:** a `Pipeline` target renders offline
  with its target's namespace and declared nodes (`node="alias"` pins
  resolve), build images as placeholders, pinned images as they are, the
  delivery node's pin, and secret placeholders. It reads no kubeconfig,
  build spec or pipeline state. `piceli render MODULE:app` is unchanged.
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
