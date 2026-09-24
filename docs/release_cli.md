# Releases from a spec (`piceli release`)

`piceli release` deploys a composition to one namespace as a sequence of
immutable, catalogued **releases**. It is a thin command layer over
`ReleaseWorkflow`: every write goes through the durable deployment session,
the plan executor and its journal, so each release can be previewed, applied,
resumed, stopped and rolled back.

```text
piceli release plan     --spec release.toml [--rotate NAME] [--adopt Kind/name] [--out plan.json]
piceli release preview  --spec release.toml          # alias of plan
piceli release apply    --spec release.toml --approve <plan-hash> | --auto-approve [--adopt Kind/name]
piceli release rollback <release|previous> --spec release.toml [--approve <hash> | --auto-approve [--adopt Kind/name]]
piceli release resume   --spec release.toml [--release NAME]
piceli release stop     --spec release.toml [--release NAME]
piceli release status   --spec release.toml
```

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

[execution]
max_seconds = 300
readiness_seconds = 240

[images]                            # pinned by digest
web = "docker.io/library/nginx@sha256:…"
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
a resource the composition declares; entries for objects that are absent or
already managed are reported as `adopt_not_needed` and ignored, so a standing
list keeps working after the first release. Adoption flags are planning
flags: `apply --approve HASH` runs exactly the approved plan and refuses them.

The plan shows how each object is adopted, and the mode, previous owner and
displaced field managers are part of the plan hash you approve:

```console
$ piceli release plan --spec release.toml \
    --adopt Deployment/web --adopt PersistentVolumeClaim/web-data --adopt Secret/web-token
release web-06c982ec7fe9 (create, apply): 3 adopt
    adopt Secret/web-token  [metadata-only: owner annotation only; previous owner: none]
    adopt PersistentVolumeClaim/web-data  [metadata-only: owner annotation only; previous owner: none]
    adopt Deployment/web  [takeover: forced apply; displaces field managers: kubectl-client-side-apply, kubectl-set; previous owner: none]
plan hash: b9b282dd…c67a07 (valid until 2026-09-24T19:26:34+00:00)
$ piceli release apply --spec release.toml --approve b9b282dd…c67a07
  adopted Secret/web-token (metadata-only)
  adopted PersistentVolumeClaim/web-data (metadata-only)
  adopted Deployment/web (takeover; removed field managers: kubectl-client-side-apply, kubectl-set)
apply web-06c982ec7fe9: ready
```

* **Retained objects** (PVC, Secret, or `piceli.io/retained: "true"`) are
  adopted **metadata-only**: Piceli writes only its owner annotation, never
  spec or data, and only when the live object already contains everything the
  composition declares for it (labels and annotations included). Declare a
  PVC with the same spec as the live claim; declare an existing Secret without
  `data` to adopt it and keep its value. A different declared value is refused
  before any write. A retained object of an `inherited_owners` id can be
  adopted the same way; its owner annotation is re-stamped.
* **Everything else** is adopted by a **takeover**: one server-side apply with
  `force=true` (the only forced write Piceli sends), then the displaced
  managers' `managedFields` entries are removed. Afterwards Piceli's field
  manager is the only owner of the fields the composition declares, so a
  later `kubectl set image` shows up as drift in the next plan:

  ```console
    drift   Deployment/web: desired fields also managed by kubectl-set
  ```

  Managers of undeclared fields (such as `kubectl rollout restart`'s
  `restartedAt` annotation) are left alone. Ordinary creates and updates
  never force.

The `apply` JSON lists `adopted` objects with `mode`, `previous_owner`,
`displaced_managers` and `removed_managers`; the journal records the same.
Rolling back to an earlier release re-applies its archived composition as
usual; adopted retained objects and their data are never touched by a
rollback. If the managedFields cleanup of a takeover is interrupted, the apply
ends `blocked` and `resume` finishes it.

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
  unmanaged; planning them fails until they are adopted explicitly
  (`--adopt` / `[release] adopt`). Only an approved takeover adoption sends a
  forced server-side apply; retained objects are adopted by an owner-annotation
  change only.
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
   by plain `kubectl` are unmanaged and planning refuses them until you adopt
   them with `--adopt Kind/name` (see
   [Adopting existing objects](#adopting-existing-objects)).
4. Replace `kubectl apply`, hand-written rollout waits and state files with
   `plan`/`apply`, and ad-hoc rollback with `rollback previous`. Keep the
   state directory: it is the release history, and the secret store holds
   the only copy of generated values.
