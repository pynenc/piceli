# Releases from a spec (`piceli release`)

`piceli release` deploys a composition to one namespace as a sequence of
immutable, catalogued **releases**. It is a thin command layer over
`ReleaseWorkflow`: every write goes through the durable deployment session,
the plan executor and its journal, so each release can be previewed, applied,
resumed, stopped and rolled back.

```text
piceli release plan     --spec release.toml [--rotate NAME] [OWNERSHIP…] [--out plan.json]
piceli release preview  --spec release.toml          # alias of plan
piceli release apply    --spec release.toml --approve <plan-hash> | --auto-approve [OWNERSHIP…]
piceli release rollback <release|previous> --spec release.toml [--approve <hash> | --auto-approve [OWNERSHIP…]]
piceli release resume   --spec release.toml [--release NAME]
piceli release stop     --spec release.toml [--release NAME]
piceli release status   --spec release.toml
```

`OWNERSHIP…` are the planning flags that authorize taking over existing
objects: `--adopt Kind/name`, `--adopt-all-desired` and `--replace Kind/name`
(see [If `plan` refuses](#if-plan-refuses)).

**Maturity:** `piceli release` is `preview`. Ownership transitions
(`--adopt`, `--adopt-all-desired`, `--replace`) are `preview`: flags and JSON
fields may still change before 1.0.

JSON goes to stdout and a short summary to stderr. Exit codes: `0` success,
`1` the execution did not become ready, `2` refused (invalid spec, identity
mismatch, unknown or expired plan), `3` approval required.

## The spec

```toml
[target]
kubeconfig = "release.kubeconfig"   # explicit file; KUBECONFIG and ~/.kube/config are never read
context = "kind-release-demo"       # explicit context; current-context is never used
namespace = "release-demo"          # must exist
cluster_uid = "…"                   # optional: expected kube-system Namespace UID
namespace_uid = "…"                 # optional: expected namespace UID
# transport = "loopback-http"       # only for a literal loopback test API
[target.nodes.primary]              # optional node pins, exposed as ctx.nodes
name = "node-a"
uid = "…"

[release]
name = "web"                        # releases are named web-<fingerprint>
owner = "release-demo"              # piceli.io/owner of managed objects
field_manager = "release-demo"
composition = "composition.py:build"   # or "package.module:function"
state_dir = ".piceli-release"       # catalog, journal, secret store, plans (0700)
approval_window_seconds = 900       # plan validity and evidence age limit
prune = false                       # delete managed objects a release drops
# inherited_owners = ["old-owner"]  # earlier owner ids whose objects count as ours
# adopt = ["Deployment/web", "PersistentVolumeClaim/data"]   # see "Adopting existing objects"
# replace = ["Deployment/legacy"]   # one-off delete-and-recreate, see "Replacing an object"

[execution]
max_seconds = 300
readiness_seconds = 240

[images]                            # pinned by digest
web = "docker.io/library/nginx@sha256:…"
# api = { receipt = "api.delivery.json" }  # a piceli.registry-delivery.v1 receipt
# images_from = "build.receipt.json"   # a piceli.build-receipt.v1 receipt

[secrets.api-token]
type = "random"
bytes = 32

[secrets.web-tls]
type = "tls-self-signed"
dns_names = ["web.release-demo.svc", "web"]
days = 365
openssl = "/usr/bin/openssl"        # absolute path; add openssl_sha256 to pin the binary

[values]                            # free-form, passed to the composition
greeting = "hello"
```

Unknown keys are rejected everywhere except `[values]`. Relative paths
resolve from the spec's directory. A complete example lives in
`examples/release/`.

### Images

An image is `repository@sha256:…`, a bare `sha256:…` digest, or a table
`{ ref = "registry/app:tag", digest = "sha256:…" }`. Tags alone are refused.
With `images_from`, images come from `outputs.images.<name>` of a build
receipt whose `revision` is `piceli.build-receipt.v1`; each entry has
`image_id`, `digest` (may be null), `platform` and `ref`. The release records
the registry digest, or the image ID when no digest exists.

To release an image pushed with `piceli artifacts deliver --to oci://…`, point
the image at its delivery receipt: `api = { receipt = "api.delivery.json" }`.
The receipt's `pull_ref` (the node-side registry address pinned to the
manifest digest) becomes the image reference, and the manifest digest becomes
the release identity. Only receipts with result `pushed` or
`already-present` are accepted. Add `digest = "sha256:…"` to pin the expected
manifest digest. This chains build → delivery → release without copying
digests by hand:

```bash
piceli artifacts build-spec run --spec build.toml --approve-builder sha256:… --out build.receipt.json
piceli artifacts deliver --image <image_id> --approve-digest <image_id> \
  --to oci://127.0.0.1:15000/app/api:r1 --node-registry 127.0.0.1:5000 \
  --via-forward deployment/registry --namespace my-app --kubeconfig kc --context ctx \
  --kubectl /usr/local/bin/kubectl --kubectl-sha256 sha256:… \
  --receipt api.delivery.json
piceli release plan --spec release.toml
```

### The composition function

```python
from piceli.k8s.ops.plan import (
    DeploymentComponent,
    DeploymentComposition,
    ResourceIntent,
)
from piceli.k8s.release_spec import ReleaseContext


def build(ctx: ReleaseContext) -> DeploymentComposition:
    secret = ResourceIntent.from_manifest(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "web-token", "namespace": ctx.namespace},
            "data": {"token": "<private>"},
        }
    ).with_secret("/data/token", ctx.secret("api-token"))
    ...
    return DeploymentComposition((DeploymentComponent("web", (secret, ...)),))
```

`ctx` carries `namespace`, `images` (use `ctx.image(name)` for the pull
reference), `values`, verified `nodes` and opaque `secrets` references.
Secret values never reach the function. Every declared secret input must be
bound. Resources must be namespaced and in the target namespace.

### Secret generators

Generators run when a release is created. `random` produces
`token_urlsafe(bytes)`; `tls-self-signed` produces an RSA key and a
self-signed certificate for the given DNS names and IPs by calling the pinned
`openssl` with an explicit argv. Inputs are named `<name>` (random) and
`<name>.crt` / `<name>.key` (TLS). `encoding = "base64"` (default) fits
`Secret.data`; `"raw"` fits `stringData`.

Values are stored once per release in the private `SecretVersionStore`. A new
release **carries over** the previous release's values unless the generator's
settings changed or you pass `--rotate NAME`. Rotation always creates a new
release.

Secrets are retained objects: Piceli never rewrites or deletes an existing
Secret, so a rotated value must go into a **new** Secret. Put a generation in
the Secret's name, bump it together with `--rotate`, and reference the new
name from the workloads:

```python
generation = ctx.values["token_generation"]  # [values] token_generation = 2
token = ResourceIntent.from_manifest(
    {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": f"web-token-{generation}", "namespace": ctx.namespace},
        "data": {"token": "<private>"},
    }
).with_secret("/data/token", ctx.secret("api-token"))
```

Rotating under the same name fails at apply with
`retained-content-precondition-failed` and writes nothing to that Secret. The
old Secret stays until you delete it.

## Plan and approval

`plan` identifies the cluster (kube-system and namespace UIDs, optional node
UIDs), captures bounded live discovery and computes a release name from a
fingerprint of the images, the composition output and the generator settings.

* A new fingerprint creates a release (`mode: create`): secret inputs are
  materialized, and the discovery snapshot, plan and grant are frozen in an
  immutable session in the catalog.
* An existing fingerprint re-plans that release against fresh discovery
  (`mode: reapply`).

The plan and its evidence are stored privately under the plan hash. `apply
--approve <hash>` executes exactly that plan; the approval is one-shot and
expires after `approval_window_seconds`. Without `--approve`, `apply` plans,
prints the hash and asks you to type its first 12 characters on a terminal;
without a terminal it exits with code `3`. `--auto-approve` is for CI.

```console
$ piceli release plan --spec release.toml
release web-716dfe62698b (create, apply): 4 create
   create ConfigMap/web-config
   create Secret/web-tls
   create Secret/web-token
   create Deployment/web
  secret api-token: generated
  secret web-tls: generated
plan hash: 4123ff6e…29b8e4 (valid until 2026-09-24T17:24:59+00:00)
$ piceli release apply --spec release.toml --approve 4123ff6e…29b8e4
apply web-716dfe62698b: ready
```

## Adopting existing objects

Objects that already exist in the namespace without this release's
`piceli.io/owner` (for example created by `kubectl`) are refused at plan
time, with the list of objects to adopt. Authorize each one with
`--adopt Kind/name` (or `apiVersion/Kind/name`; repeatable) on `plan`,
`apply` or `rollback`, or list them in `[release] adopt`. An entry must name
a resource the composition declares; entries for objects that are absent, or
already managed with nothing to reclaim, are reported as `adopt_not_needed`
and ignored, so a standing list keeps working after the first release. An
entry for a managed object that other clients have written to since (for
example with `kubectl edit`) takes it over again, which reclaims those fields. Adoption flags are planning
flags: `apply --approve HASH` runs exactly the approved plan and refuses them.

The plan shows how each object is adopted, and the mode, previous owner and
transferred field managers are part of the plan hash you approve:

```console
$ piceli release plan --spec release.toml \
    --adopt Deployment/web --adopt PersistentVolumeClaim/web-data --adopt Secret/web-token
release web-06c982ec7fe9 (create, apply): 3 adopt
    adopt Secret/web-token  [metadata-only: owner annotation only; previous owner: none]
    adopt PersistentVolumeClaim/web-data  [metadata-only: owner annotation only; previous owner: none]
    adopt Deployment/web  [takeover: transfers field managers: kubectl-client-side-apply, kubectl-rollout, kubectl-set; fields they own that the release does not declare will be REMOVED; previous owner: none]
plan hash: b9b282dd…c67a07 (valid until 2026-09-24T19:26:34+00:00)
$ piceli release apply --spec release.toml --approve b9b282dd…c67a07
  adopted Secret/web-token (metadata-only)
  adopted PersistentVolumeClaim/web-data (metadata-only)
  adopted Deployment/web (takeover; transferred field managers: kubectl-client-side-apply, kubectl-rollout, kubectl-set)
apply web-06c982ec7fe9: ready
```

* **Retained objects** (PVC, Secret, or `piceli.io/retained: "true"`) are
  adopted **metadata-only**: one merge patch writes Piceli's owner annotation
  and the labels and annotations the composition declares that differ from
  the live object (listed as `metadata_changes`), never spec or data, and
  only when the live object already contains everything else the composition
  declares for it. Keys are set, never removed. Declare a PVC with the same
  spec as the live claim; declare an existing Secret without `data` to adopt
  it and keep its value. A different declared spec or value is refused before
  any write. A retained object of an `inherited_owners` id can be adopted the
  same way; its owner annotation is re-stamped.
* **Everything else** is adopted by a **takeover**, which makes the
  composition the object's full desired state. Every field written by a
  client (`kubectl-client-side-apply`, `kubectl-create`, `kubectl-set`,
  `kubectl-edit`, `kubectl-rollout`, other tools) is transferred to Piceli's
  field manager, then the manifest is applied without force. **Fields those
  clients wrote that the composition does not declare are removed**: a
  container with another name, a `restartedAt` annotation, extra labels and
  the `kubectl.kubernetes.io/last-applied-configuration` annotation (so a
  later client-side `kubectl apply` cannot bring old fields back). Fields
  defaulted by the API server come back with their defaults. Kept as they
  are: subresource entries (`status`, and `scale` written by an autoscaler)
  and control-plane managers (`kube-controller-manager`, `kube-scheduler`,
  `kube-apiserver`, `kubelet`, `cloud-controller-manager`, names starting
  with `k3s` or ending in `-controller`/`-controller-manager`). A value
  that a kept manager owns and the composition sets differently (for
  example `replicas` under an autoscaler) fails as a conflict instead of
  being forced.

  Afterwards a later `kubectl set image` shows up as drift in the next plan:

  ```console
    drift   Deployment/web: desired fields also managed by kubectl-set
  ```

  Ordinary creates and updates never force. The only request Piceli sends
  with `force=true` is a takeover's `dryRun=All` admission check, which
  persists nothing.

The `apply` JSON lists `adopted` objects with `mode`, `previous_owner`,
`transferred_managers`, `completed_transfer` and `metadata_changes`; the
journal records the same. Replaced objects appear there with `mode: replace`
and their backup file.
Rolling back to an earlier release re-applies its archived composition as
usual; adopted retained objects and their data are never touched by a
rollback. If a takeover is interrupted, the apply ends `blocked` and `resume`
converges it. A takeover that was approved with a different transfer list
(for example by an older Piceli) cannot be resumed; plan again with `--adopt`.

### Adopting everything the composition declares

`--adopt-all-desired` authorizes adopting **every** existing unmanaged object
the composition declares, and nothing else: objects that are not in the
composition are never touched, and objects named by `--replace` are replaced
instead. The plan still lists each adoption with its mode, and the
`authorized` field of the plan JSON names every adopted and replaced object,
all bound to the plan hash you approve. Retained objects are still adopted
metadata-only. It is a planning flag only; there is no spec equivalent, so a
later plan never adopts new objects silently.

```console
$ piceli release plan --spec release.toml --adopt-all-desired --replace Deployment/web
release shop-8eebc4a5b6fb (create, apply): 2 adopt, 1 apply, 1 replace
    adopt PersistentVolumeClaim/cache  [metadata-only: owner annotation and labels/app.kubernetes.io/part-of; previous owner: none]
    apply PersistentVolumeClaim/state  [retained, metadata-only: sets labels/app.kubernetes.io/part-of; spec and data untouched]
  replace Deployment/web  [DELETES uid 86d938d2-… and recreates it from the release; backup written first; dependents: deleted]
    adopt Service/web  [takeover: transfers field managers: kubectl-client-side-apply; fields they own that the release does not declare will be REMOVED; previous owner: none]
plan hash: c354b73f…1e280c (valid until 2026-09-24T20:47:52+00:00)
```

### Metadata changes on retained objects

A retained object that this release already manages (its own owner id, or an
id in `inherited_owners`) is never rewritten. When the only difference from
the composition is in labels or annotations, the plan shows an `apply` marked
`metadata_only` and the executor writes exactly those keys, plus Piceli's
owner and operation annotations, with one merge patch guarded by the observed
UID and resourceVersion. Spec and data are never sent, and the object is
re-stamped with this release's owner. This covers the common migration case of
a volume claim created by a retired tool: list the tool's owner id in
`inherited_owners` and add your labels in the composition. A difference in
spec or data refuses the plan with `retained-content-differs`.

### Replacing an object (delete and recreate)

Some objects cannot be adopted into the desired state: a Deployment whose
`spec.selector` (immutable) must change, or a Service whose type change the
API rejects. `--replace Kind/name` (repeatable; or `[release] replace`)
authorizes deleting such an object and creating it from the release. Every
replace needs its own explicit entry; there is no "replace all".

Replace is allowed only for objects that are **unmanaged**, **not retained**
and **not owned by another object** (`ownerReferences`). It is refused, before
any write, for:

* retained kinds and objects (Namespace, PersistentVolume, PVC, Secret,
  `piceli.io/retained: "true"`): adopt them instead;
* objects already managed by this release (including inherited owners): the
  release already updates them;
* objects owned by another object, and workloads whose retained dependents a
  background delete would remove.

What `apply` does for each replaced object:

1. re-checks the live object against the planned evidence (UID,
   resourceVersion, field managers, content) and sends a `dryRun` delete;
2. writes a **backup**: the live object as JSON, cleaned so that
   `kubectl create -f` accepts it (no `status`, `uid`, `resourceVersion`,
   `managedFields`, `creationTimestamp`, `generation`), to
   `<state_dir>/backups/<execution-id>/<n>-<Kind>-<name>.json` with mode
   `0600` in `0700` directories. The journal records its path and SHA-256
   together with the intent, before the delete;
3. deletes the object with UID and resourceVersion preconditions. Workload
   controllers (Deployment, ReplicaSet, DaemonSet, Job, CronJob) are deleted
   with `propagationPolicy=Background`, so their old pods go too: expect a
   short downtime. Everything else, including StatefulSets (so a PVC
   retention policy can never delete claims), uses `Orphan`;
4. waits until the object is gone and creates it from the release with
   Piceli's owner annotation.

The apply output names the backup:

```console
  replaced Deployment/web (deleted uid 86d938d2-…; backup: .piceli-release/backups/9669…/0002-Deployment-web.json; to restore the previous object: kubectl delete Deployment/web, then kubectl create -f .piceli-release/backups/9669…/0002-Deployment-web.json)
```

**Recovery and rollback.** A replace is not undone automatically:
compensation and `rollback` never delete a replaced object or restore its
backup. `rollback previous` re-applies an earlier release as usual, which
updates the recreated object like any managed object. To go back to the
object as it was before Piceli, restore the backup by hand:

```bash
kubectl delete deployment web
kubectl create -f .piceli-release/backups/<execution>/<n>-Deployment-web.json
```

If the apply stops after the delete (the create failed, the process was
interrupted, the object took too long to disappear), the execution is
`blocked`, the backup is on disk and the journal records the step reached.
`piceli release resume` finishes the create for a created release; for a
re-apply, plan and apply again (the object is now absent and is simply
created). If someone else created an object with the same name in between,
the resume stops with `replace-recreated-by-another-writer` instead of
touching it.

## If `plan` refuses

A plan that meets objects it may not change refuses with exit code `2` and
lists **every** blocking object at once, each with the flags that would
unblock it, in the JSON `blocking` array and on stderr:

```console
$ piceli release plan --spec release.toml
refused: existing objects are not managed by this release's owner: Deployment/web (--adopt Deployment/web or --replace Deployment/web), PersistentVolumeClaim/cache (--adopt PersistentVolumeClaim/cache), Service/web (--adopt Service/web or --replace Service/web); …
  blocking Deployment/web: exists and is not managed by this release's owner -> --adopt Deployment/web or --replace Deployment/web
  blocking PersistentVolumeClaim/cache: exists and is not managed by this release's owner; retained: replace is never allowed -> --adopt PersistentVolumeClaim/cache
  blocking Service/web: exists and is not managed by this release's owner -> --adopt Service/web or --replace Service/web
```

Choosing between adopt and replace:

| Situation | Use | Effect |
| --- | --- | --- |
| The live object can become the composition's object by an update | `--adopt Kind/name` | Takeover: field ownership moves to Piceli and fields the composition does not declare are removed. No downtime, same UID. |
| Adopt everything the composition declares, after reviewing the list | `--adopt-all-desired` | The same takeover for each unmanaged declared object; retained ones metadata-only. |
| A volume claim, Secret or other retained object | `--adopt Kind/name` (never replace) | Metadata-only: owner annotation and declared labels/annotations; the spec/data must already match. |
| An immutable field must change (selector, Service `clusterIP`…), or an adoption fails with `invalid-request` | `--replace Kind/name` | Backup, delete, create. New UID; workload pods restart. |
| The object is not yours to change | neither | Rename the object in the composition, or remove it from the composition. |

Refusal codes (`code` in the JSON, also per object in `blocking[].code`):

| Code | Cause | Fix |
| --- | --- | --- |
| `resource-requires-adoption` | An object the composition declares exists without this release's owner. | `--adopt` or `--replace` it (see `suggest`), `--adopt-all-desired`, or delete it. |
| `replace-refused` | `--replace` names a retained object, an object already managed, or one owned by another object. | Adopt a retained object instead; remove managed objects from the replace list (`[release] replace` is for a one-off migration). |
| `retained-content-differs` | A retained object's spec or data differ from the composition; only labels and annotations may change. | Make the composition match the live object (or create a new object under a new name). |
| `adopt-and-replace` | The same object is named by an adopt and a replace entry. | Keep one. |
| `adopt-entry-not-declared`, `replace-entry-not-declared` | An entry does not name exactly one resource the composition declares. | Fix the `Kind/name` (or use `apiVersion/Kind/name`). |
| `plan-blocked` | Several of the above at once. | See each `blocking[].code`. |

Execution failures of these paths (`failure_category` of `apply`):

| Category | Cause | Fix |
| --- | --- | --- |
| `replace-precondition-failed` | At apply time the object is managed, retained or owned by another object, or no backup directory is available. Nothing was deleted. | Plan again. |
| `replace-backup-failed` | The backup could not be written (permissions, disk). Nothing was deleted. | Fix the state directory, plan again. |
| `replace-delete-timeout` | The deleted object was still present after `readiness_seconds` (finalizers). Execution `blocked`. | Inspect the finalizers, then `resume` or plan again. |
| `replace-recreated-by-another-writer` | Another client created an object with the same name after the delete. Execution `blocked`; the object is not touched. | Decide which object to keep; the backup holds the deleted one. |
| `replace-delete-not-observed` | On resume, the original object is still there although the journal recorded its delete. Execution `blocked`. | Inspect the object, then plan again. |
| `invalid-metadata-change` | A metadata-only write was asked to set a non-string value or Piceli's own annotations. | Fix the composition's labels/annotations. |
| `retained-content-precondition-failed` | A retained object's content (for example a private Secret value) differs from the composition at apply time. Nothing was written. | Use a new object name (see "Secret generators"). |

## Rollback

`rollback <release>` re-plans the named release's archived composition
against current discovery and, once approved, executes it as a new execution
(`ReleaseWorkflow.rollback`). The release is selected only when the rollback
becomes ready. It reuses the release's own secret versions; nothing is
regenerated. `previous` means what was running before the latest change: the
last ready release other than the current one, or, when the latest execution
did not become ready, the last ready release itself.

## Resume, stop and status

* `resume` continues an interrupted apply of a created release with the same
  grant and operation IDs, while the grant is valid. Re-apply and rollback
  executions are not resumable: plan and apply again.
* `stop` cancels the latest unfinished execution after checking the owner
  and target. A stopped execution is not resumed.
* `status` reads the catalog, journal and history without contacting the
  cluster: releases with their image identities and executions, the deployed
  and previous release, pending plans and recent history.

## Safety model

* The kubeconfig and context are explicit. Exec plugins and auth providers
  are refused (token refresh is not supported yet), as are proxies,
  `insecure-skip-tls-verify` and basic auth. `https` requires verified TLS.
* A state directory serves one cluster and namespace: releases record the
  kube-system UID, and a later run against another cluster is refused.
* Objects are owned by exact `owner` match. Objects created by other tools are
  unmanaged; planning them fails until they are adopted or replaced
  explicitly (`--adopt`, `--adopt-all-desired`, `--replace`). A takeover
  transfers field ownership and applies without force; retained objects are
  only ever changed in metadata (owner annotation, declared labels and
  annotations) and are never deleted or replaced. A replace writes a
  restorable backup before a delete guarded by UID and resourceVersion.
* Plans, discovery evidence and secrets stay in the private state directory
  (owner-only). Reports and the catalog contain opaque references only.

## Migrating from a custom deploy script

1. Move the manifest-building code into a pure `build(ctx)` function. Replace
   hard-coded images with `ctx.image(...)` and inline secret values with
   `ResourceIntent.with_secret(pointer, ctx.secret(name))`.
2. Declare the target, identities, images (or `images_from`) and generators in
   `release.toml`, and delete the script's password and certificate code. The
   first release generates fresh values; importing existing values is not
   supported yet, so plan a rotation window for anything clients cache.
3. Ownership: objects that already carry `piceli.io/owner` are managed if you
   keep that owner or list it in `release.inherited_owners`. Objects created
   by plain `kubectl` are unmanaged and planning refuses them, listing each
   one, until you adopt them (`--adopt Kind/name` or `--adopt-all-desired`)
   or replace them (`--replace Kind/name`); see
   [If `plan` refuses](#if-plan-refuses).
4. Replace `kubectl apply`, hand-written rollout waits and state files with
   `plan`/`apply`, and ad-hoc rollback with `rollback previous`. Keep the
   state directory: it is the release history, and the secret store holds
   the only copy of generated values.
