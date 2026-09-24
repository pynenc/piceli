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
Existing unmanaged objects require exact adoption grants. Explicit desired values
always participate in comparison, even when the API supplies a default.

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
conditional POST; existing objects use server-side apply with `force=false` and
UID/resourceVersion preconditions. A dry-run admission check precedes each write.
Deletes carry UID/version preconditions and `propagationPolicy=Orphan`.
No retained kind is deleted. Target identity is rechecked for mutations and each
readiness poll; resource identity, desired state and field ownership are checked
against receipts. Controllers may update status while readiness is pending.

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
Adopted objects, deleted objects and retained resources are kept. Interrupted
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
make local-test-env
make test-local-executor
```

Bootstrap uses available Python 3.12, the hash-locked
`tests/local-requirements.lock` and an editable install of this checkout. It fixes
relocated entrypoints; no exact `python3.11.7` interpreter is required. To review
and regenerate the lock explicitly:

```sh
uv pip compile pyproject.toml tests/local-requirements.txt --generate-hashes --output-file tests/local-requirements.lock
```

Set `VENV=/absolute/path/to/a/new/venv` on both make commands to validate an
isolated environment. The entire acceptance suite also passes from a fresh
Python 3.12.7 environment using that override.

The test command runs unit tests plus the actual SDK transport against a
fault-injecting loopback API. It exercises no-op reapply, partial discovery,
RBAC/conflict, byte/deadline bounds, private versions, lost replies, object/namespace
recreation, SIGKILL after response before receipt, readiness timeout, cancellation,
resume and conservative compensation. Reports and source pins are retained in
`target/local-executor/{results.xml,evidence.json}`. Imports/planning remain
client-free. The fake server checks request semantics; it does not reproduce all
Kubernetes admission, SSA ownership/defaulting or controller behavior. No live
cluster or homelab readiness is qualified by these results.
