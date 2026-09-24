# Releases from a spec (`piceli release`)

```{admonition} Maturity: preview
:class: note

`piceli release` is tested end to end against kind; options and JSON fields may still change in a minor release, with a changelog entry. See the {doc}`roadmap` for every feature's status.
```

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
piceli release secret show NAME --spec release.toml [--key KEY] [--release NAME] [--reveal] [--json]
```

JSON goes to stdout and a short summary to stderr. Exit codes: `0` success,
`1` the execution did not become ready, `2` refused (invalid spec, identity
mismatch, unknown or expired plan), `3` approval required. A refusal with a
stable cause also carries a `code` (for example `secret-import-unavailable`).

## The spec

```toml
# Top-level keys come before the first [table].
# images_from = "build.receipt.json"   # optional: images from a piceli.build-receipt.v1 receipt

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
# api = { receipt = "api.delivery.json" }  # a piceli.registry-delivery.v1 receipt

[secrets.api-token]
type = "random"
bytes = 32

[secrets.web-tls]
type = "tls-self-signed"
dns_names = ["web.release-demo.svc", "web"]
days = 365
openssl = "/usr/bin/openssl"        # absolute path; add openssl_sha256 to pin the binary
# more types: tls-ca, template, import, static (see "Secret generators")

[values]                            # free-form, passed to the composition
greeting = "hello"
```

Unknown keys are rejected everywhere except `[values]`. Relative paths
resolve from the spec's directory. A complete example lives in
`examples/release/`.

### Images

An image is `repository@sha256:…`, a bare `sha256:…` digest, or a table
`{ ref = "registry/app:tag", digest = "sha256:…" }`. Tags alone are refused.
With the **top-level** key `images_from` (written before the first `[table]`,
never inside `[images]`), images come from `outputs.images.<name>` of a build
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
bound, except outputs that a `template` generator uses. Resources must be
namespaced and in the target namespace.

### Secret generators

```{toctree}
:hidden:

secrets
```

The how-to {doc}`secrets` covers every generator with examples, `secret
show`, rotation and the error codes. In short:

| `type` | Settings | Outputs |
| --- | --- | --- |
| `random` | `bytes` (16–512, default 32) | `<name>` |
| `tls-self-signed` | `dns_names`, `ip_addresses`, `days`, `rsa_bits`, `openssl`, `openssl_sha256` | `<name>.crt`, `<name>.key` |
| `tls-ca` | `leaves = {leaf = {dns_names, ip_addresses}}` (or `leaf = [dns names]`), `common_name`, `ca_days`, `days`, `rsa_bits`, `openssl`, `openssl_sha256` | `<name>.ca.crt`, `<name>.<leaf>.crt`, `<name>.<leaf>.key`; `<name>.ca.key` is internal |
| `template` | `template` with `{output}` / `{secret:output}` placeholders, `{{`/`}}` for braces | `<name>` |
| `import` | exactly one of `file`, `env`, `secret = {name, key}`; `trim_newline` (default true, file/env), `rotate` (`random` or `reimport`), `bytes` | `<name>` |
| `static` | `value` (public) | `<name>` |

All take `encoding = "base64"` (default, fits `Secret.data`) or `"raw"`
(fits `stringData`; UTF-8 text only). Certificates come from the pinned
`openssl` called with an explicit argv, never a shell.

Values are produced only after the plan and its grant validate, so a refused
plan generates, imports and stores nothing. They are stored once per release
in the private `SecretVersionStore`. A new release **carries over** the
previous release's values unless the generator's settings changed or you pass
`--rotate NAME` (rotation always creates a new release; templates are
re-rendered from their inputs). An imported value is the first version, is
carried over like the others and is rotatable with `--rotate`.

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
  adopted **metadata-only**: Piceli writes only its owner annotation, never
  spec or data, and only when the live object already contains everything the
  composition declares for it (labels and annotations included). Declare a
  PVC with the same spec as the live claim; declare an existing Secret without
  `data` to adopt it and keep its value. A different declared value is refused
  before any write. A retained object of an `inherited_owners` id can be
  adopted the same way; its owner annotation is re-stamped.
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
`transferred_managers` and `completed_transfer`; the journal records the same.
Rolling back to an earlier release re-applies its archived composition as
usual; adopted retained objects and their data are never touched by a
rollback. If a takeover is interrupted, the apply ends `blocked` and `resume`
converges it. A takeover that was approved with a different transfer list
(for example by an older Piceli) cannot be resumed; plan again with `--adopt`.

## If `plan` refuses

A refused `plan` changes nothing: no object is written and no plan is stored.
It exits with code `2`, prints `{"state": "refused", "reason": "…"}` on
stdout and repeats the reason on stderr after `refused:`. Find the reason in
the table and take the next step, then run `plan` again.

| The reason says | What to do |
| --- | --- |
| `existing objects are not managed by this release's owner: Kind/name, …` | Adopt each listed object: `piceli release plan --spec release.toml --adopt Kind/name` (repeat the flag, or list them in `[release] adopt`). Review the adoption mode in the plan before approving (see [Adopting existing objects](#adopting-existing-objects)). If an object should not be kept, delete it yourself and plan again. A per-object `--replace` (delete and recreate) is planned for a later release. |
| `adopt '…' does not name exactly one resource declared by the composition` | Fix the `--adopt` entry: it must be `Kind/name` (or `apiVersion/Kind/name`) of an object the composition declares. |
| `discovery is incomplete, refusing to plan (…)` | Each item names a resource type and a code: `rbac-denied` (grant list/get on it to the spec's identity), `limit-exceeded` (raise `[discovery]` limits), `api-unavailable` or `deadline-exceeded` (retry). |
| `cluster identity differs from the one recorded in this state directory` | The kubeconfig/context points at another cluster than the one this `state_dir` deployed to. Fix `[target]`; never reuse a `state_dir` across clusters. |
| `cannot rotate undeclared secrets: …` | `--rotate` names must be `[secrets.<name>]` entries of the spec. |
| `invalid release spec: …` | Fix the named key. `images_from` is a top-level key (before the first `[table]`), not part of `[images]`. |
| A single code such as `rbac-denied` or `server-target-identity-mismatch` | Run `piceli explain <code>`, or see {doc}`reference/errors`. |

`apply --approve HASH` refuses a hash that is unknown, expired or already
applied (`no pending plan with this hash`): run `plan` again and approve the
new hash.

```{note}
Release refusals still carry a sentence in `reason`. They will move to the
fixed `{"state": "rejected", "reason": "<code>"}` form of
{doc}`reference/errors` in a later release; match on the exit code (`2`) until
then.
```

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
  (`--adopt` / `[release] adopt`). A takeover transfers field ownership and
  applies without force; retained objects are adopted by an owner-annotation
  change only.
* Plans, discovery evidence and secrets stay in the private state directory
  (owner-only). Reports and the catalog contain opaque references only.

## Migrating from a custom deploy script

1. Move the manifest-building code into a pure `build(ctx)` function. Replace
   hard-coded images with `ctx.image(...)` and inline secret values with
   `ResourceIntent.with_secret(pointer, ctx.secret(name))`.
2. Declare the target, identities, images (or `images_from`) and generators in
   `release.toml`, and delete the script's password, certificate and
   config-file code: `tls-ca` replaces hand-made certificates and `template`
   replaces init-container scripts that write DSNs or config files. To keep a
   service with existing data working, `import` the values it already uses
   (from a live Secret, a file or an environment variable); see
   {doc}`secrets`.
3. Ownership: objects that already carry `piceli.io/owner` are managed if you
   keep that owner or list it in `release.inherited_owners`. Objects created
   by plain `kubectl` are unmanaged and planning refuses them until you adopt
   them with `--adopt Kind/name` (see
   [Adopting existing objects](#adopting-existing-objects)).
4. Replace `kubectl apply`, hand-written rollout waits and state files with
   `plan`/`apply`, and ad-hoc rollback with `rollback previous`. Keep the
   state directory: it is the release history, and the secret store holds
   the only copy of generated values.
