# Releases from a spec (`piceli release`)

`piceli release` deploys a composition to one namespace as a sequence of
immutable, catalogued **releases**. It is a thin command layer over
`ReleaseWorkflow`: every write goes through the durable deployment session,
the plan executor and its journal, so each release can be previewed, applied,
resumed, stopped and rolled back.

```text
piceli release plan     --spec release.toml [--rotate NAME] [--out plan.json]
piceli release preview  --spec release.toml          # alias of plan
piceli release apply    --spec release.toml --approve <plan-hash> | --auto-approve
piceli release rollback <release|previous> --spec release.toml [--approve <hash> | --auto-approve]
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
  unmanaged; planning them fails until they are adopted explicitly.
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
   by plain `kubectl` are unmanaged and planning refuses them; delete them or
   recreate them under the release (explicit adoption from the CLI is not
   available yet).
4. Replace `kubectl apply`, hand-written rollout waits and state files with
   `plan`/`apply`, and ad-hoc rollback with `rollback previous`. Keep the
   state directory: it is the release history, and the secret store holds
   the only copy of generated values.
