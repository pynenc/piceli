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
piceli release plan     --spec release.toml [--rotate NAME] [OWNERSHIP…] [--out plan.json]
piceli release preview  --spec release.toml          # alias of plan
piceli release diff     --spec release.toml [OWNERSHIP…] [--exit-code]   # read-only
piceli release apply    --spec release.toml --approve <plan-hash> | --auto-approve [OWNERSHIP…] [--skip-checks]
piceli release rollback <release|previous> --spec release.toml [--approve <hash> | --auto-approve [OWNERSHIP…]] [--skip-checks]
piceli release resume   --spec release.toml [--release NAME] [--skip-checks]
piceli release stop     --spec release.toml [--release NAME]
piceli release check    --spec release.toml [--release NAME]
piceli release status   --spec release.toml
piceli release secret show NAME --spec release.toml [--key KEY] [--release NAME] [--reveal] [--json]
```

`OWNERSHIP…` are the planning flags that authorize taking over existing
objects: `--adopt Kind/name`, `--adopt-all-desired` and `--replace Kind/name`
(see [If `plan` refuses](#if-plan-refuses)).

**Maturity:** `piceli release` is `preview`. Ownership transitions
(`--adopt`, `--adopt-all-desired`, `--replace`) are `preview`: flags and JSON
fields may still change before 1.0.

JSON goes to stdout and a short summary to stderr. Exit codes: `0` success,
`1` the execution did not become ready or its `[[checks]]` failed (reason
`check-failed`), `2` rejected (invalid spec, identity mismatch, unknown or
expired plan), `3` approval required. See
[Output and exit codes](#output-and-exit-codes) for the objects each case
prints.

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
# allow_exec = true                 # GKE/EKS/AKS exec plugins, see "Target and credentials"
# exec_sha256 = "sha256:…"          # optional pin of the resolved plugin file
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
# rollback_on_failed_checks = true  # re-apply the previous ready release when [[checks]] fail

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

[[checks]]                          # optional post-deploy checks, run after readiness
type = "http"                       # http | exec | metric | python (see docs/checks.md)
target = "service/web"
path = "/"
expect = 200
```

Unknown keys are rejected everywhere except `[values]`. Relative paths
resolve from the spec's directory. A complete example lives in
`examples/release/`.

### Target and credentials

`[target]` names exactly one namespace of one cluster: an explicit kubeconfig
file (`KUBECONFIG`, `~/.kube/config` and in-cluster files are never read), an
explicit context (never `current-context`) and a namespace that must exist.
`cluster_uid` and `namespace_uid` pin the kube-system and namespace UIDs, and
`[target.nodes.<alias>]` pins nodes.

Credentials may be a client certificate or a bearer token. Managed clusters
(GKE, EKS, AKS, OIDC) use an **exec credential plugin** instead; Piceli runs
it only with `allow_exec = true`, pins the resolved file (`exec_sha256`),
passes it a minimal environment (`PATH`, `HOME`, `exec_pass_env` and the
kubeconfig's `env`) and refreshes expiring credentials itself. See
{doc}`managed_clusters` for the security model, the `exec_*` keys and the
`exec-*` error codes. Legacy `auth-provider` users, proxies,
`insecure-skip-tls-verify` and basic auth are always refused.

| Key | Default | Meaning |
| --- | --- | --- |
| `kubeconfig`, `context`, `namespace` | required | The explicit target. |
| `cluster_uid`, `namespace_uid` | none | Expected identities. |
| `transport` | `https` | `loopback-http` only for a literal loopback test API. |
| `request_seconds` | `10` | Limit for one API request, in (0, 60]. |
| `allow_exec` | `false` | Run the context's exec plugin (preview). |
| `exec_sha256`, `exec_pass_env`, `exec_timeout_seconds` | none, `[]`, `60` | Plugin pin, extra variables, run limit. |

### Images

`images_from` is a **top-level** key: write it before the first `[table]`,
never inside `[images]`.

```{admonition} Status: preview
:class: note

The immutability rule and the receipt formats below are stable. The merge
rules for `images_from` lists and the refusal codes are new in this release
and may still gain cases.
```

**Rule: a release only references an image by a name that no other build can
move.** Every `ctx.image(name)` is one of:

- `repository@sha256:<manifest digest>`: declared by hand, from a build
  receipt that recorded a manifest digest, or the `pull_ref` of a
  registry-delivery receipt; or
- `repository:sha256-<hex>`: a **content tag**, accepted only from a
  node-delivery receipt that proves the node holds that exact config digest
  under that name. `<hex>` is a prefix (12 to 64 characters) of the config
  digest, so the name can only ever point at this image.

A plain tag (`app:dev`, `app:1.4.2`, `latest`) is never used. When nothing
immutable is known, `ctx.image(name)` refuses with `image-not-immutable`; the
image's `identity` is still available.

Image sources:

| Declared as | Reference | Release identity |
| --- | --- | --- |
| `web = "repo@sha256:…"`, or `{ ref = "repo:tag", digest = "sha256:…" }` | `repo@digest` | the digest |
| a bare `"sha256:…"` | none (`identity` only) | the digest |
| `images_from`: build receipt entry with a `digest` | `repo@digest` | the digest |
| `images_from`: build receipt entry with `digest: null` | **refused** (`image-not-immutable`) until a delivery receipt replaces it | `image_id` |
| `{ receipt = "…" }` or `images_from`: `piceli.registry-delivery.v1` | its `pull_ref` (`<node registry>/<repo>@<manifest digest>`) | the manifest digest |
| `{ receipt = "…" }` or `images_from`: `piceli.node-delivery.v1` | its node `image.reference`, which must be a content tag | the config digest |

The release records its images as its source identity: with one image,
`{"kind": "oci", "identity": <digest>}`; with several, the whole set,
`{"kind": "oci-set", "images": {<name>: <digest>, …}, "identity": <digest of
that map>}` (in `release status` and the catalog). The Python API builds the
same with `ReleaseSource.image_set({...})` for `ReleaseWorkflow.create`.

A build receipt (`revision = "piceli.build-receipt.v1"`, from
`piceli artifacts build-spec run`) lists `outputs.images.<name>` with
`image_id` (the config digest with Docker's classic store), `digest` (the
manifest digest, or `null` with the classic store), `platform` and `ref`.
With the classic store, which keeps no manifest, the build alone cannot name
an immutable reference: deliver the image first.

Delivery receipts are accepted only when the delivery succeeded: result
`pushed` or `already-present` for a registry delivery, `imported` or
`already-present` for a node import. In `[images.<name>]`, `digest` pins the
expected **manifest** digest of a registry receipt, or the expected **config**
digest of a node receipt.

#### `images_from` with several receipts

`images_from` takes one receipt or a list. A list merges build and delivery
receipts, in any order:

1. Build receipts name the images. A name in two build receipts is refused
   (`image-declared-twice`).
2. Each delivery receipt replaces every built image with the same config
   digest (for a containerd-store build, a registry delivery may instead
   match the build's manifest digest). A delivery that matches no built image
   is refused (`receipt-unmatched`); a delivery receipt carries no image name,
   so use `[images.<name>] receipt = …` to release it without a build
   receipt.
3. An image delivered by two listed receipts is refused
   (`image-declared-twice`).

A name in both `[images]` and `images_from` is refused, with one exception:
`[images.<name>] receipt = "…"` may replace a built image of that name. Its
config digest must be the built image's, otherwise the spec is refused
(`image-digest-mismatch`).

```toml
images_from = ["build.receipt.json", "api.delivery.json", "worker.node.json"]

# or name a delivery explicitly (it must match the built "api"):
# images_from = "build.receipt.json"
# [images.api]
# receipt = "api.delivery.json"
```

#### Refusal codes

A refused spec exits with `2` and prints
`{"state": "rejected", "reason": "<code>", "message": "…"}` (see
[Output and exit codes](#output-and-exit-codes)). The image codes are fixed
words (`piceli.k8s.release_spec.ImageHandoffError.code`):

| Code | Meaning | Fix |
| --- | --- | --- |
| `image-not-immutable` | The only name for the image is a tag that can move: a build receipt without a manifest digest, a node reference that is not a content tag, or a `pull_ref` not pinned to its manifest digest | Deliver to a registry, or node-import with `--ref <repo>:sha256-<12 hex>` (the error prints the exact tag) |
| `receipt-invalid` | Unreadable JSON, an unknown schema or revision, or invalid digests | Pass the receipt the command wrote |
| `delivery-not-succeeded` | The receipt records a rejected or failed delivery | Deliver again |
| `image-digest-mismatch` | The delivery is of another image than the build of that name, or than the pinned `digest` | Deliver the image that was built, or update the pin |
| `image-declared-twice` | One name from two sources (see the merge rules) | Keep one |
| `receipt-unmatched` | A listed delivery receipt matches no built image | Add its build receipt, or name it with `[images.<name>] receipt` |

#### Build → deliver → release

```bash
piceli artifacts build-spec run --spec build.toml --approve-builder sha256:… --out build.receipt.json
piceli artifacts deliver --image <image_id> --approve-digest <image_id> \
  --to oci://127.0.0.1:15000/app/api --node-registry 127.0.0.1:5000 \
  --via-forward deployment/registry --namespace my-app --kubeconfig kc --context ctx \
  --receipt api.delivery.json
# release.toml: images_from = ["build.receipt.json", "api.delivery.json"]
piceli release plan --spec release.toml
```

For node import, name the image by its content tag, which
`piceli.k8s.release_spec.content_tag(config_digest)` also computes:

```bash
ID=$(docker image inspect --format '{{.Id}}' app/api:dev)   # classic store: the config digest
piceli artifacts deliver --image "$ID" --approve-digest "$ID" \
  --ref "app/api:sha256-${ID:7:12}" \
  --to 'ssh://ops@node-1.example?runtime=k3s-containerd' --receipt api.node.json
```

A content-tag image is not pulled, so leave `imagePullPolicy` at its default
(`IfNotPresent`) or set `Never`, and pin the workload to the node that holds it.

#### Two images, one change

`examples/two-images/` releases two small images from a node-local registry
({doc}`node_local_registry`). `registry.toml` installs the registry as its own
release; `release.toml` releases one Deployment per image from the two
registry-delivery receipts:

```{literalinclude} ../examples/two-images/composition.py
:language: python
:start-at: "def build"
```

Rebuilding and re-delivering one image changes one receipt, so the next plan
differs in exactly one desired manifest, and only that Deployment rolls out.
The opt-in test `tests/integration/test_two_images_kind.py` runs the whole
flow on kind (see its docstring). Verified on kind v0.29.0 (Kubernetes
v1.33.1): after `alpha` was rebuilt and re-delivered (2 blobs uploaded, the
shared base layer skipped), `alpha` went to generation 2 with the new digest
and `beta` stayed at generation 1.

The plan of the rebuilt release lists `Deployment/beta` as `no-op` and
`Deployment/alpha` as `apply` with a single field change, its image (see
[What a plan shows](#release-plan-output)).

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

(release-plan-output)=
### What a plan shows

Each object gets one operation: `create`, `adopt`, `replace`, `apply`,
`no-op` or `delete`. An object that exists and that the release already
manages is `no-op` when applying it would change nothing, and `apply`
otherwise. {doc}`plans_and_diffs` explains how that is decided (a server-side
dry run of the write, so server defaults never make an unchanged object look
changed).

Every `apply`, `adopt` and `replace` comes with a **field-level diff**. The
human summary prints up to 12 changed fields per object; the JSON output has
all of them under `diffs`:

```console
$ piceli release plan --spec release.toml
release web-3c1d0a9e22f4 (create, apply): 1 apply, 3 no-op
    apply Deployment/web
            ~ /spec/template/spec/containers/0/image: "docker.io/library/nginx@sha256:6564…" -> "docker.io/library/nginx@sha256:1ead…"
plan hash: 9b0f…c4d1 (valid until 2026-09-24T18:02:11+00:00)
```

```json
"diffs": [
  {
    "resource": {"api_version": "apps/v1", "kind": "Deployment", "namespace": "shop", "name": "web"},
    "operation": "apply",
    "basis": "server-dry-run",
    "changes": [
      {"path": "/spec/template/spec/containers/0/image", "op": "replace",
       "before": "docker.io/library/nginx@sha256:6564…", "after": "docker.io/library/nginx@sha256:1ead…"}
    ],
    "not_compared": [],
    "unified": "--- live/Deployment/web\n+++ release/Deployment/web\n@@ …"
  }
],
"dry_run_unavailable": []
```

* `path` is a JSON pointer into the object, `op` is `add`, `remove` or
  `replace`, and `before`/`after` are `null` when absent. Secret values are
  shown as `"<redacted>"`; values bound to secret versions are listed in
  `not_compared` and never shown. They are compared privately (in-process,
  see {doc}`plans_and_diffs`), so an unchanged Secret is `no-op`.
* A `remove` change on a map key (a label, a ConfigMap key) is a three-way
  removal: an earlier release declared the key and this one no longer does.
  The action lists them under `removes`; see {doc}`plans_and_diffs`.
* `basis` is `server-dry-run` when the "after" side is the API server's
  answer to a dry run of the write, or `client` when no dry run was available
  and the desired manifest was merged onto the live object locally (server
  defaults are then missing from "after").
* `dry_run_unavailable` lists managed objects without a dry run and why
  (`rbac-denied`, `conflict`, `dry-run-limit-exceeded`, …). Such an object is
  compared literally and may show as `apply` although unchanged.
* The diff is evidence, not part of the plan: it is not in the plan hash.

`piceli release diff --spec release.toml` prints only the diffs: unified
diffs on stderr and `{"state": "diffed", "summary", "changes", "actions",
"diffs", "dry_run_unavailable"}` on stdout. It stores no plan and no local
state and sends the cluster only reads and `dryRun=All` requests, so it is
safe to run at any time. With `--exit-code` it exits `1` when the release
would change something.

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
{"blocking": [{"code": "resource-requires-adoption", "kind": "Deployment", "message": "exists and is not managed by this release's owner", "name": "web", "suggest": ["--adopt Deployment/web", "--replace Deployment/web"]}, …], "code": "resource-requires-adoption", "message": "existing objects are not managed by this release's owner: …", "reason": "resource-requires-adoption", "state": "rejected"}
```

and on stderr:

```text
rejected: existing objects are not managed by this release's owner: Deployment/web (--adopt Deployment/web or --replace Deployment/web), PersistentVolumeClaim/cache (--adopt PersistentVolumeClaim/cache), Service/web (--adopt Service/web or --replace Service/web); … [resource-requires-adoption]
  blocking Deployment/web: exists and is not managed by this release's owner -> --adopt Deployment/web or --replace Deployment/web
  blocking PersistentVolumeClaim/cache: exists and is not managed by this release's owner; retained: replace is never allowed -> --adopt PersistentVolumeClaim/cache
  blocking Service/web: exists and is not managed by this release's owner -> --adopt Service/web or --replace Service/web
  next: Plan again with `--adopt Kind/name` …
```

Choosing between adopt and replace:

| Situation | Use | Effect |
| --- | --- | --- |
| The live object can become the composition's object by an update | `--adopt Kind/name` | Takeover: field ownership moves to Piceli and fields the composition does not declare are removed. No downtime, same UID. |
| Adopt everything the composition declares, after reviewing the list | `--adopt-all-desired` | The same takeover for each unmanaged declared object; retained ones metadata-only. |
| A volume claim, Secret or other retained object | `--adopt Kind/name` (never replace) | Metadata-only: owner annotation and declared labels/annotations; the spec/data must already match. |
| An immutable field must change (selector, Service `clusterIP`…), or an adoption fails with `invalid-request` | `--replace Kind/name` | Backup, delete, create. New UID; workload pods restart. |
| The object is not yours to change | neither | Rename the object in the composition, or remove it from the composition. |

Refusal codes (`reason` in the JSON, also per object in `blocking[].code`):

| Code | Cause | Fix |
| --- | --- | --- |
| `resource-requires-adoption` | An object the composition declares exists without this release's owner. | `--adopt` or `--replace` it (see `suggest`), `--adopt-all-desired`, or delete it. |
| `replace-refused` | `--replace` names a retained object, an object already managed, or one owned by another object. | Adopt a retained object instead; remove managed objects from the replace list (`[release] replace` is for a one-off migration). |
| `retained-content-differs` | A retained object's spec or data differ from the composition; only labels and annotations may change. | Make the composition match the live object (or create a new object under a new name). |
| `adopt-and-replace` | The same object is named by an adopt and a replace entry. | Keep one. |
| `adopt-entry-not-declared`, `replace-entry-not-declared` | An entry does not name exactly one resource the composition declares. | Fix the `Kind/name` (or use `apiVersion/Kind/name`). |
| `plan-blocked` | Several of the above at once. | See each `blocking[].code`. |

Other refusals:

| `reason` | What to do |
| --- | --- |
| `discovery-incomplete` | The `message` names each resource type and a code: `rbac-denied` (grant list/get on it to the spec's identity), `limit-exceeded` (raise `[discovery]` limits), `api-unavailable` or `deadline-exceeded` (retry). |
| `cluster-identity-changed` | The kubeconfig/context points at another cluster than the one this `state_dir` deployed to. Fix `[target]`; never reuse a `state_dir` across clusters. |
| `unknown-rotate-secret` | `--rotate` names must be `[secrets.<name>]` entries of the spec. |
| `invalid-release-spec` | Fix the key named in `message`. `images_from` is a top-level key (before the first `[table]`), not part of `[images]`. |
| `invalid-composition` | Fix the composition function (it must return a `DeploymentComposition` in the target namespace and bind every declared secret input). |
| `kubeconfig-rejected`, `namespace-not-found`, `server-target-identity-mismatch`, `rbac-denied`, … | Run `piceli explain <code>`, or see {doc}`reference/errors`. |
| `target-refused`, a code starting with `exec-`, or `auth-provider-refused` | The kubeconfig/context was refused, or the target's exec credential plugin was not allowed, not pinned, or failed. See "If it fails" in {doc}`managed_clusters`. |

`apply --approve HASH` refuses a hash that is unknown, expired or already
applied (`plan-not-found`, `plan-expired`): run `plan` again and approve the
new hash. Any code can be looked up with `piceli explain <code>` or in
{doc}`reference/errors`.

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

## Post-deploy checks

`[[checks]]` tables declare checks that run after an execution becomes ready
(`apply`, `rollback` and `resume`). A release is `ready` only when every check
passes; otherwise the result has `release_state = "checks-failed"`, the command
exits `1` and the release is not selected. With
`[release] rollback_on_failed_checks = true` the previous ready release is
re-applied automatically and the result carries a `rollback` object.
`--skip-checks` skips them (recorded in the history). `release check` runs the
checks against a release without changing anything. The how-to, every check
type and the state machine are in {doc}`checks`.

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `type` | `http`, `exec`, `metric`, `python` | required | The check type |
| `name` | string | derived, e.g. `http-service-web-login` | Unique name in the spec |
| `timeout` | seconds | `10` | Limit of one attempt |
| `retries` | integer | `3` | Extra attempts after a failed one |
| `interval` | seconds | `2` | Wait between attempts |
| `target` | `kind/name` | required (not `python`) | `service`/`deployment`/`pod` for http and metric; `deployment`/`statefulset`/`daemonset`/`pod` for exec |
| `path`, `port`, `expect`, `body_contains` | | `/`, target's first port, `[200, 299]`, none | `http` |
| `command`, `container`, `expect_exit`, `output_contains` | | required, first container, `0`, none | `exec` |
| `query`, `op`, `threshold`, `port`, `path`, `empty` | | required, `<=`, required, target's first port, `/api/v1/query`, `fail` | `metric` |
| `call` | `module:function` or `file.py:function` | required | `python` |

The checks and the rollback policy are stored with the plan, so `apply` runs
exactly what was reviewed; the plan JSON shows them under `checks`.

## Rollback

`rollback <release>` re-plans the named release's archived composition
against current discovery and, once approved, executes it as a new execution
(`ReleaseWorkflow.rollback`). The release is selected only when the rollback
becomes ready. It reuses the release's own secret versions; nothing is
regenerated. `previous` means what was running before the latest change: the
last ready release other than the current one, or, when the latest execution
did not become ready (including `checks-failed`), the last ready release
itself. A rollback runs the spec's `[[checks]]` too; an automatic rollback
after failed checks is described in {doc}`checks`.

## Resume, stop and status

* `resume` continues an interrupted apply of a created release with the same
  grant and operation IDs, while the grant is valid. Re-apply and rollback
  executions are not resumable: plan and apply again.
* `stop` cancels the latest unfinished execution after checking the owner
  and target. A stopped execution is not resumed.
* `status` reads the catalog, journal and history without contacting the
  cluster: releases with their image identities, executions and latest check
  outcome (`checks`), the deployed and previous release, pending plans and
  recent history.

Their refusals: `no-execution-recorded`, `not-resumable`, `resume-refused`,
`nothing-to-stop`, `execution-not-started`, `execution-other-owner` and
`execution-other-target` (see {doc}`reference/errors`).

## Output and exit codes

Every `piceli release` command prints exactly one JSON object on stdout and
human text (summaries, the plan hash to approve, hints) on stderr:

| Exit | stdout | When |
| --- | --- | --- |
| `0` | `{"state": "planned", "plan_hash": …}` (`plan`), `{"state": "succeeded", "execution": {…}, …}` (`apply`, `rollback`, `resume`, `stop`), the status object (`status`), the metadata (`secret show --json`) | Success |
| `1` | `{"state": "failed", "reason": "<code>", "execution": {…}, …}` | The execution ran but did not become ready. `reason` is `execution.failure_category` when it is a registered code, otherwise `execution-not-ready`. |
| `2` | `{"state": "rejected", "reason": "<code>", "message": "<human text>", "code": "<code>", …}` | Rejected before any change. Extra fields such as `blocking` are kept. |
| `3` | `{"state": "approval-required", "plan_hash": …, …}` | `apply`/`rollback` without `--approve` and without a terminal confirmation. Nothing was executed. |

`reason` is always a registered error code: `piceli explain <reason> --json`
prints its cause, fix and whether a retry can succeed. `message` is for
people; do not parse it. `secret show NAME --reveal` without `--json` prints
the raw value on stdout by design (see {doc}`secrets`).

(release-contract-changes)=
### Contract changes in 0.4.0

The output of `piceli release` changed to follow the CLI contract
({doc}`agents`); scripts that parsed the 0.3.0 output need these updates:

| 0.3.0 | 0.4.0 |
| --- | --- |
| Refusal `{"state": "refused", "reason": "<sentence>", "code": "<code>"?}` | `{"state": "rejected", "reason": "<code>", "message": "<sentence>", "code": "<code>"}` |
| `reason` was free text; `code` present only for some refusals | `reason` is always a registered code; the sentence moved to `message` |
| `code` | Kept as an alias of `reason` for 0.4.x only; it will be removed in 0.5.0. Read `reason`. |
| Refusal stderr started with `refused:` | Starts with `rejected:` and ends with `[<code>]`, then a `next:` hint |
| `apply`/`rollback`/`resume`/`stop` result had no top-level `state` | Adds `"state": "succeeded"`, or `"state": "failed"` with `reason` on exit 1 |
| Result JSON was indented | The same object; refusals are printed on one line |

`blocking[]` items keep their fields (`kind`, `name`, `code`, `message`,
`suggest`). Exit codes did not change.

## Safety model

* The kubeconfig and context are explicit. Exec plugins run only with
  `allow_exec = true`, pinned and with a minimal environment, and Piceli
  refreshes their credentials itself ({doc}`managed_clusters`). Legacy auth
  providers, proxies, `insecure-skip-tls-verify` and basic auth are refused.
  `https` requires verified TLS.
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
* `plan` and `diff` send server-side dry runs (`dryRun=All`) of the writes an
  apply would make; the API server persists nothing for them. They need the
  `patch` verb; without it objects are compared literally.

## Migrating from a custom deploy script

To start from what is already running, `piceli import live` generates the
typed module for you; see {doc}`migrate_from_kubectl`. By hand:

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
   by plain `kubectl` are unmanaged and planning refuses them, listing each
   one, until you adopt them (`--adopt Kind/name` or `--adopt-all-desired`)
   or replace them (`--replace Kind/name`); see
   [If `plan` refuses](#if-plan-refuses).
4. Replace `kubectl apply`, hand-written rollout waits and state files with
   `plan`/`apply`, and ad-hoc rollback with `rollback previous`. Keep the
   state directory: it is the release history, and the secret store holds
   the only copy of generated values.
