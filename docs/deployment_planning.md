# Deployment planning and recoverable execution

```{admonition} In short
:class: tip

1. **Discover**: `capture_discovery` reads the target namespace within strict limits
   and produces an `ObservedSnapshot`.
2. **Plan**: `build_plan(composition, snapshot, authorization)` is a pure,
   deterministic function that returns ordered create/adopt/apply/no-op/delete actions.
3. **Execute**: `PlanExecutor.run(...)` applies the plan through a
   `KubernetesProvider`, writing every step to an `ExecutionJournal` so it can
   be cancelled, resumed or compensated.
4. **Wrap it**: `DeploymentSession` and `ReleaseWorkflow` combine the above into a
   recoverable, named release.

New to these terms? Start with the {doc}`overview`.
```

Piceli separates pure intent and preview from an explicitly constructed provider
and authorized execution. Importing the planner, discovery contract or executor
does not load kubeconfig, construct clients or contact infrastructure.

## Portable discovery v2

The current contract is `piceli.discovery.v2`:

- [Schema](schemas/piceli-discovery-v2.schema.json)
- [Portable fixture](https://github.com/pynenc/piceli/blob/main/tests/fixtures/discovery-v2/complete.json)
- [SHA-256 manifest](schemas/piceli-discovery-v2.manifest.json)

V1 remains historical; the runtime rejects its weaker authority format. V2
validates RFC3339 capture time, GVK, scope, requested/completed/API coverage,
UID/resourceVersion, retention, default pointers and source provenance at both
constructor and decoding boundaries. JSON Schema describes the wire structure;
the decoder additionally enforces relationships, chronology and private-content
completeness. Consumers must run both validations.

`capture_discovery` enforces requested-type, page, count, byte, per-call and total
wall-time budgets. Continuation loops, duplicate resources, changing list versions,
RBAC denial, malformed responses and oversized private content fail closed.
Incomplete evidence cannot prove absence or authorize pruning. Outstanding
uncooperative provider calls are bounded to four, including timed-out calls.
Cluster and namespace UIDs are checked before and after non-synthetic captures.

Portable provenance is an assertion, never an execution grant. Synthetic evidence
is preview-only. Public redacted snapshots are content-incomplete and cannot
authorize execution. The real provider requires the caller's `ApiClient`, explicit
origin/target, field manager, owner, kube-system Namespace UID (cluster identity)
and target Namespace UID. No ambient credential refresh, proxy, redirect or
automatic HTTP retry is used. Live transport requires verified HTTPS; local
acceptance requires a literal loopback HTTP origin.

## Pure composition

`ResourceIntent`, `DeploymentComponent` and `DeploymentComposition` own immutable
manifest copies and dependency order. `ObservedSnapshot` binds coverage, provenance,
defaults and resource identities. `build_plan(composition, snapshot, authorization)`
produces deterministic create/adopt/apply/no-op/delete actions and a public hash.
Existing unmanaged objects require exact adoption grants
(`PlanAuthorization(adopt_resources=...)`); see "Adoption by ownership
transfer" below. Explicit
desired values always participate in comparison, even when the API supplies a
default.

Namespace, PV, PVC and Secret retention cannot be disabled. Pruning protects
retained/unmanaged descendants and orders allowed deletion child-first. Public
summaries omit secret values and their digests. Standard Kubernetes Secret
references remain visible; sensitive inline values are redacted by the same
implementation in planning and discovery.

[The composition example](https://github.com/pynenc/piceli/blob/main/examples/local_cluster_composition.py) builds a
credential Secret followed by a worker Deployment. Its caller supplies an actual
image and a private version reference. Acceptance executes
it only against the fake API and verifies dependency order.

`piceli deploy plan --cluster-id ID` remains an offline preview CLI using empty
observed state. It neither discovers nor executes a live plan. The recoverable
executor is an explicit Python library API; legacy deploy CLI commands are not
automatically routed through it.

## Execution API

Construct `KubernetesProvider`, `ExecutionJournal`, `SecretVersionStore` and
`PlanExecutor` explicitly. Capture fresh private evidence from that provider,
build an immutable plan, then supply an `ExecutionAuthorization` binding:

- target, endpoint provenance, cluster/namespace UIDs and evidence age;
- plan/snapshot hashes, owner and field manager;
- exact action/precondition/private-version grants and expiry;
- individual cluster-scoped and compensation resource permissions.

`executor.preview(plan)` is pure. `executor.run(id, plan, snapshot, grant)`
checks those bindings and executes one ordered action at a time. New objects use
conditional POST; existing objects use server-side apply with `force=false` (or,
for objects this owner already manages, a merge patch) and UID/resourceVersion
preconditions; nothing is persisted with `force=true`. A dry-run admission
check precedes each write (for a takeover adoption, described below, it is
the one request sent with `force=true`).
Deletes carry UID/version preconditions and `propagationPolicy=Orphan`.
No retained kind is deleted. Target identity is rechecked for mutations and each
readiness poll; resource identity, the declared fields' values and their field
owners are checked against receipts. Fields the plan does not declare are not
compared, so controllers may update status, a Deployment's revision annotation
or a WaitForFirstConsumer claim's `spec.volumeName` and binding annotations
while readiness is pending.

The private SQLite journal commits intent before network I/O, then commits the
receipt. A process lock excludes competing executors. Cancellation is durable:
use a separately opened journal's `cancel(id)` from another thread/process, and
`run(..., resume=True)` to explicitly resume. Limits bound actions, polls,
concurrency and elapsed time. A timed-out HTTP mutation may still finish at the
server; it remains ambiguous and is never blindly retried. Resume observes its
operation annotation, UID, ownership and content. Absence alone cannot prove that
an ambiguous create will not finish later, so it remains blocked.

`executor.compensate(...)` refreshes complete discovery and reverses only still
owned, explicitly authorized changes under current UID/version/field-owner
preconditions. Created objects may be removed; owned updates may be restored.
A takeover adoption is reversed like an update: the pre-adoption content is
re-applied with `force=false` and the object stays owned (the transferred
field managers are not restored). Metadata-only adoptions, deleted objects and
retained resources are kept. Interrupted
compensation is also journaled and reconciled by observation. This is not a
Kubernetes-wide rollback transaction. Unknown scope, object recreation, drift,
descendants or conflicting ownership prevent unsafe reversal.

Private values use immutable random store/version IDs through
`intent.with_secret(json_pointer, reference)`. Rotation changes exact authorization
even when a public plan hash is unchanged. Values and before/after manifests live
only in an owner-only private store; the journal contains opaque references.
Directories/files require modes 0700/0600. This local store is plaintext: callers
own encryption, backup and access policy. Public reports contain neither private
values nor private reference IDs. The durable public interchange below is a
different machine-readable artifact: it carries opaque reference IDs solely to
bind an exact resume to the already-private versions.

### Adoption by ownership transfer

An ADOPT action moves an existing object under this owner. Only objects the
caller lists in `PlanAuthorization(adopt_resources=...)` are adopted; any
other unmanaged desired object fails planning, and the executor refuses an
ADOPT that its `ExecutionAuthorization` does not grant exactly, before any
write. `build_plan` picks the mode from the observed object and records it in
the action (`PlanAction.adoption`), so it is part of the plan hash:

| Mode | Objects | Write |
| --- | --- | --- |
| `metadata-only` | retained: Namespace, PV, PVC, Secret, or `piceli.io/retained: "true"` | one merge patch with only `piceli.io/owner`, `piceli.io/operation` and the observed UID/resourceVersion |
| `takeover` | everything else | transfer of client field managers to this manager (a `managedFields` merge patch), then a server-side apply with `force=false` that removes undeclared transferred fields |

**Metadata-only.** Planning refuses unless the live object already contains
the desired manifest (labels and annotations included). Private values are
compared by the executor after resolution and before any write; the refusal
names the resource, never the value. Spec and data are never part of the
request, and the response must differ from the observed object only in those
two annotations. Compensation never touches the object. A retained object
owned by an earlier owner id can be adopted the same way when that id is in
`PlanAuthorization.inherited_owner_ids`; the owner annotation is re-stamped.

**Takeover.** It makes the desired manifest the object's full desired state,
as Flux does for objects written by `kubectl`. `transferred_managers` in the
plan are every *transferable* field manager of the live object, not only
those owning declared fields (`plan.transferable_managers`):

* transferred: entries on the main resource (no subresource) with operation
  `Apply` or `Update`, for example `kubectl-client-side-apply`,
  `kubectl-create`, `kubectl-set`, `kubectl-edit`, `kubectl-rollout`,
  `helm` or another tool;
* kept: every subresource entry (`status`; `scale`, as an autoscaler writes
  it) and control-plane managers (`CONTROL_PLANE_MANAGERS`:
  `kube-apiserver`, `kube-controller-manager`, `kube-scheduler`, `kubelet`,
  `cloud-controller-manager`; plus names starting with `k3s` or ending in
  `-controller` or `-controller-manager`).

`PlanAuthorization.field_manager` keeps the executing manager out of the
list. The executor then:

1. re-checks that the live `managedFields`, generation and content match the
   planned evidence (`field-owner-precondition-failed` otherwise), and that
   the recorded transfer list matches that evidence;
2. sends the admission check: `KubernetesProvider.take_over(dry_run=True)`,
   a `dryRun=All` server-side apply with `force=true`. It is the only
   `force=true` request Piceli sends and persists nothing. It refuses
   retained kinds, a manifest that does not claim this owner, and a stale
   UID/resourceVersion;
3. journals intent with `adoption.transferred_managers`;
4. `KubernetesProvider.converge_takeover`: a merge patch, guarded by the
   observed resourceVersion, replaces `managedFields` so that the transferred
   entries (and this manager's own Update entries) become one Apply entry of
   this manager holding the union of their fields; kept entries are
   unchanged. The API rejects `managedFields` in an apply body, so this is
   its own write;
5. applies the manifest with `force=false`. This manager now owns every
   client-written field, so the API server removes each one the manifest
   does not declare: a container with another name, extra labels, a
   `restartedAt` annotation and `kubectl.kubernetes.io/last-applied-configuration`
   (an unowned leftover of that annotation is removed too, so a client-side
   `kubectl apply` cannot resurrect old fields). A conflict at this point is
   with a kept manager (for example an autoscaler owning `replicas` with
   another value) and fails as `conflict` instead of being forced;
6. verifies the object contains the manifest and that no transferable foreign
   manager is left, then records `adoption.completed_transfer`.

A concurrent write makes steps 4–5 fail their precondition; the object is
re-read and the steps repeat. A transferable manager that the plan did not
list stops the takeover. The steps are idempotent: an interrupted takeover
stays in `intent` (execution `blocked`) and resume converges it. A plan may
also ADOPT an object this owner already manages (non-retained): the takeover
then reclaims fields other clients wrote since. Ordinary creates and updates
are unchanged, so real conflicts still fail with `error:conflict`. An ADOPT
restored from a plan without an adoption mode (before this feature) never
transfers anything.

`field_drift(composition, snapshot, field_manager)` reports managed,
non-retained objects whose declared fields are also owned by another manager,
for example after a `kubectl set image`. It is informational and not part of
the plan hash.

**Inherited owners as a grant.** `ExecutionAuthorization.inherited_owner_ids`
lets the executor treat retained objects of those earlier owner ids as its own
(an unchanged object is reconciled without a write). Only ids the provider
also lists in `KubernetesProvider(inherited_owner_ids=...)` are honoured, and
a grant listing others is refused. The grant is part of the revision identity
when it is not empty.

**Rotating a retained Secret.** A retained Secret is never rewritten, so a new
value under the same name fails with `retained-content-precondition-failed`.
Rotate into a new Secret instead: give the Secret a name with a generation
suffix (for example `api-token-2`), point the workloads at it, and apply.
The old Secret stays until you delete it.

### Durable revisions

`DeploymentRevision.create(plan, snapshot, grant)` produces a canonical,
redacted identity for the desired state, target snapshot, authorization and exact
action grants. `ExecutionBundle.create(revision)` adds one durable execution ID
and one random operation ID per action. `bundle.to_json()` is canonical JSON for
caller-owned durable storage: it includes only opaque `SecretVersionRef` IDs so a
resume can prove it is using the same private versions, never the secret values.
It also deliberately excludes receipts and resolved manifests.

On restart, load the original private plan/snapshot/grant and validate them with
`DeploymentRevision.from_json(...)`, then rebuild the bundle with
`ExecutionBundle.from_json(...)` and call `executor.run_bundle(bundle,
resume=True)`. This reuses the revision, private references and operation IDs.
A renewed authorization must explicitly set `resume_revision_id` to the revision
ID and retain the exact target, ownership and action scope. Retained Piceli-owned
resources that already match are reconciled without an SSA write or replacement
operation annotation; ownership, content, UID/version or generation drift blocks.

PVC readiness is always `Bound`. For a PVC explicitly consumed by a workload in
the same plan, execution submits both resources before waiting, then verifies PVC
binding followed by normal workload readiness. A Pending PVC is never globally
reported as ready. See `examples/deployment_revision.py` for the Python-first
canonical JSON interchange boundary.

### Deployment sessions

`DeploymentSession` is the Python-first boundary above revisions and bundles.
It accepts a pure composition factory, immutable snapshot, plan authorization and
authorization factory, alongside caller-owned `ExecutionJournal` and
`SecretVersionStore`. Session construction never constructs a Kubernetes provider
or performs Kubernetes I/O. Each named private input is materialized once through
`SecretVersionStore.put_once`; retries reuse its random opaque reference without
storing a secret value or value-derived hash.

`session.preview()` is fully offline. `session.apply(executor)`,
`session.resume(executor)` and `session.stop(executor)` require an executor with
the exact journal, private store, target and owner. Recovery rebuilds the
composition from the persisted opaque references and rejects changed composition,
snapshot/target drift, missing or wrong-store references, replaced/expired grants,
or incomplete journal action identity. Rotation means a *new* session ID and
therefore a new revision; it never changes a previous session.
`DeploymentSession.create()` refuses an existing session ID because it cannot
compare caller values safely; use `open()` for exact recovery or `rotate()` for
deliberately new private material.

`session.archive.to_json()` is canonical machine interchange described by
[the v1 schema](schemas/piceli-deployment-session-v1.schema.json). It contains
opaque `SecretVersionRef` IDs needed to prove exact resume, but never values or
value hashes. It must be treated as private operational metadata. In contrast,
`session.report()` is safe for status endpoints and omits those IDs. A concise
factory is in `examples/deployment_session.py`.

### Legacy execution import

`LegacyExecutionArchive` is the only supported bridge from a legacy
`ExecutionJournal` binding to a durable `DeploymentRevision` and
`ExecutionBundle`. Create and retain the canonical JSON archive while the
original plan, discovery evidence and authorization still exist:

```python
archive_json = LegacyExecutionArchive.from_revision(revision).to_json()
```

The archive has schema version `1` and exactly these fields:

```json
{
  "schema_version": 1,
  "revision": "redacted DeploymentRevision interchange",
  "plan": "original rendered redacted plan",
  "snapshot": "immutable target, coverage, defaults and resource identities",
  "discovery": "immutable redacted DiscoveryArtifact",
  "authorization": "exact grant material and expiry",
  "private_references": "opaque resource/pointer/SecretVersionRef bindings"
}
```

Import needs the legacy journal, a *separate* destination journal, the original
private version store and that canonical archive. It makes no provider calls and
does not resolve secret values. Each archive redaction must correspond to an
existing opaque `SecretVersionRef`; source action IDs and safe prior receipts are
copied verbatim. Migration records source-binding and archive SHA-256 lineage in
the destination journal, never in the historical journal.

`import_legacy_execution(...)` raises `LegacyEvidenceInsufficient` for a missing
or altered plan, target, ownership, action scope, snapshot coverage/default,
manifest, private reference, grant, receipt or legacy state. Legacy `intent` and
compensation states are deliberately ambiguous and cannot be imported. The
result's `report()` is safe for humans and omits opaque reference IDs; the archive
itself retains those IDs only for exact machine resume. See
`examples/legacy_execution_import.py` for the compact flow.

### Release workflow

`ReleaseWorkflow` is the small public layer for an everyday release loop. It
does not add a planner, executor, database, or credential format: it records an
immutable `ReleaseSource` and a canonical `DeploymentSessionArchive` in a
locked, atomic local `ReleaseCatalog`, then reopens the same session for preview,
apply, resume, selection/revert, or exact-owner stop. Python callers supply the
same frozen composition and discovery snapshot already required by
`DeploymentSession`; safe JSON/YAML inputs are parsed by `load_release_input()`
before validation and never execute source text.

`ReleaseSource` accepts an immutable Git commit, an explicit dirty source-closure
SHA-256, or a prebuilt OCI digest/archive. Digest delivery requires neither Git
nor a registry. Preview only reconstructs durable local evidence; import,
discovery and apply remain separately explicit operations. A preview namespace
may carry a positive TTL, but retained PVCs are always `retain`: expiry is never
permission to delete data. The compact catalog contains the private archive and
is therefore operator metadata (`0600`), not a public report.

The built-in `piceli observe` CLI and loopback operations page remain the human
and REST-facing observer for an archived session: they show declared versus
undeclared resources, bounded logs, and saved loopback forwards. They do not
gain deploy authority from the page. `ReleaseWorkflow` supplies the same durable
identity to Python, CLI and future REST adapters rather than creating separate
release state.

Named `ReleaseWorkflow.reopen(name)` and `preview(name)` do not change the
selected release. Use `ReleaseCatalog.select(name)` explicitly for that local
preference. Catalog reads do not create directories or lock files. The workflow
namespace must match its immutable snapshot target, and duplicate `create`
rejects before private materialization; recover using `reopen`. Git and OCI
selectors can share an identical session/artifact identity, but this equivalence
does not itself verify an image build/import or a live rollback.

## Local acceptance

```sh
make install
make test-acceptance   # or `make test` for unit + acceptance
```

The acceptance suite runs the real Kubernetes client SDK against a
fault-injecting loopback API. It exercises:

- no-op reapply and partial discovery;
- RBAC and conflict errors, byte and deadline bounds;
- private secret versions and lost replies;
- object and namespace recreation;
- SIGKILL after a response but before the receipt is written;
- readiness timeout, cancellation, resume and conservative compensation.

Imports and planning remain client-free. The fake server checks request
semantics, but it does not reproduce all of Kubernetes admission, SSA
ownership/defaulting or controller behaviour, so these results do not qualify a
live cluster. `make test-integration` covers that part against a disposable
cluster (CI uses kind).
