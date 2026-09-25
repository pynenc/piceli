# Deploy an app from source

This page shows how to take a typed app from its source to a running, verified
release with one command, `piceli deploy`: it builds the images in a pinned
builder, delivers them by digest, plans the release against the live cluster,
applies it and runs its checks, skipping every stage whose content has not
changed.

```{admonition} Maturity: preview
:class: note

`piceli.pipeline` and `piceli deploy` are **preview**: tested end to end on
`kind`, but names, options and JSON fields may still change in a minor
release (always with a changelog entry).
```

```{figure} _static/img/deploy-flow.gif
:alt: A terminal runs piceli deploy on the shop example with --plan and prints the planned stages, a combined hash and the approve command. It then runs piceli deploy with --approve and that hash, streaming the build, deliver (with registry progress), plan, apply (with readiness progress) and checks stages until the checks pass and "deploy ready" is printed. Finally piceli status reports that shop is up, its release ready and its three Deployments ready, with both declared forwards still down.
:width: 100%

Plan, approve, check: `piceli deploy --plan`, `piceli deploy --approve`
and `piceli status` on the shop example against a local `kind` cluster.
```

## Prerequisites

New to Piceli? Start with {doc}`getting_started/index`.

- Piceli installed (`pip install piceli`), Python 3.12 or later.
- `docker` with `buildx` (the build runs in a pinned builder image) and
  `kubectl` on `PATH` (the node-loopback registry is reached through a
  supervised `kubectl port-forward`).
- A cluster reached through an **explicit** kubeconfig file and context.
  Piceli never reads `~/.kube/config` or the current context. For the
  example, a disposable `kind` cluster:

  ```sh
  kind create cluster --name shop --kubeconfig examples/shop/shop.kubeconfig
  KUBECONFIG=examples/shop/shop.kubeconfig kubectl create namespace shop
  ```

- Data the release must never own already exists. The example's cache keeps
  its data on an `ExistingClaim("cache-state")`, so create that claim once:

  ```sh
  KUBECONFIG=examples/shop/shop.kubeconfig kubectl -n shop apply -f - <<'EOF'
  apiVersion: v1
  kind: PersistentVolumeClaim
  metadata: {name: cache-state}
  spec: {accessModes: [ReadWriteOnce], resources: {requests: {storage: 64Mi}}}
  EOF
  ```

- The example builds `examples/builds/rust-hello` for `linux/arm64`; on an
  `amd64` machine change `platforms` in its `build.toml`.

## Steps

1. **Declare the pipeline.** One module holds the app, its build, where it
   runs and how images get there. This is `examples/shop/app.py`:

   ```{literalinclude} ../examples/shop/app.py
   :language: python
   :lines: 19-
   ```

   - `Target.kubeconfig(...)` names the cluster, namespace and nodes. Relative
     paths (the kubeconfig, the build spec, `state_dir`) resolve from the
     directory of the file that declares them.
   - `Build.spec(...)` wraps a `build.toml` (see {doc}`containerized_builds`);
     `Build.dockerfile(...)` builds one image per Dockerfile target stage;
     its `smoke={"api": Smoke(["--version"], expect_stdout=r"^api ")}` runs
     an isolated check in a built image (arguments, plain `env`, an
     `entrypoint` override, output patterns; see {ref}`build-smoke`) before
     the build counts as done. Smoke checks are part of the build's plan hash,
     so changing one changes the combined hash.
     `images["rust-hello"]` is a *handle*: a placeholder that `piceli deploy`
     replaces with the delivered, digest-pinned reference. Every other image
     must be pinned by digest.
   - `Secrets(...)` declares generators (`Random`, `Template`, `Static`,
     `TlsCa`, or any `[secrets.*]` type from {doc}`secrets`); `secrets.ref(name)`
     is an opaque reference the release binds to the real value at apply time.
   - `deliver=` is `NodeLoopbackRegistry()`, `NodeImport()` or
     `Registry("oci://host/prefix")` (see {ref}`delivery`).
   - `build=` is optional. When every image is already pinned by digest
     (`repo@sha256:…`), `Pipeline(app, target)` works without `build=` or
     `deliver=`: the build and deliver stages are skipped and the pipeline
     plans, applies and checks the release.
   - `access=app.access.forward(...)` on a Service declares how it is reached
     from your laptop; it renders to no Kubernetes object (see {doc}`access`).

2. **Render it without a cluster.** A `Pipeline` renders with its target's
   namespace and declared nodes, so `node="alias"` pins resolve; build
   handles stay placeholders, workloads that use a built image get the
   delivery node's pin, and secret values are never read or generated:

   ```sh
   piceli render examples/shop/app.py:pipeline
   ```

   It reads no kubeconfig, build spec or state. `piceli render
   examples/shop/app.py:app --namespace shop` renders the `App` alone, with
   no target (so no node pins).

3. **Plan every stage.** Nothing is built, pushed or applied. Before the
   images exist, the release plan is a **preview** with placeholder images
   (see {ref}`deploy-preview`):

   ```sh
   piceli deploy examples/shop/app.py:pipeline --plan
   ```

   Expected output (stderr; the result object is on stdout):

   ```text
   deploy plan for examples/shop/app.py:pipeline (until checks):
     inputs   rust-hello: 3 staged file(s), plan 7cd633981e19
     inputs   source piceli: 485a4dc88cee
     build    rust-hello: build (linux/arm64, builder 8fa55b2f3ddf, network none)
     deliver  registry shop-registry-d0c1b1b7947f: create ConfigMap/registry-config,
              create PersistentVolumeClaim/registry-storage, create Deployment/registry
     deliver  rust-hello: pending-build
     plan     preview with placeholder images (rust-hello=pending-build), not approvable:
     plan     preview: create Secret/cache-credentials, create Deployment/cache, create Deployment/web,
              create Service/cache, create Deployment/api, create Service/api, create Service/web
     plan     the real release plan follows delivery; it may not adopt,
              replace or delete more than this preview
     apply    pending
     checks   1 check(s) (rollback on failure)
   combined hash: fab781d97321848dc074bd293a208e1057bb5cdf3b79afc12d906b7c83e6ec25
   approve with:
     piceli deploy examples/shop/app.py:pipeline --approve fab781d9…
   ```

   When the namespace already holds objects with the app's names that the
   release does not manage, `--plan` refuses before anything is built, with
   every blocking object and the flags that unblock it (exit `2`):

   ```text
   rejected: existing objects are not managed by this release's owner: … (resource-requires-adoption)
     blocking Service/web: exists and is not managed by this release's owner -> --adopt Service/web or --replace Service/web
     (release preview with placeholder images: rust-hello=pending-build; nothing was built or delivered)
   ```

   Add `adopt=["Service/web"]` (or `replace=`) to the `Pipeline`, or delete
   the object, and plan again.

4. **Approve the combined hash** after reviewing the plan:

   ```sh
   piceli deploy examples/shop/app.py:pipeline --approve <combined hash>
   ```

   Expected output, after the plan summary is printed again (`running` lines
   and repeated `waiting` lines omitted):

   ```text
   [inputs] done
   [build] rust-hello: building (log: examples/shop/.piceli-deploy/builds/rust-hello/build.log)
   [build] rust-hello: [piceli] 1/3 linux/arm64 files: succeeded in 4.904s
   [build] rust-hello: [piceli] 2/3 linux/arm64 image:rust-hello: succeeded in 3.613s
   [build] rust-hello: [piceli] 3/3 linux/arm64 smoke:rust-hello: succeeded in 2.855s
   [build] rust-hello: [piceli] drift check: 3 staged file(s) unchanged
   [build] done
   [deliver] registry shop-registry-d0c1b1b7947f: applying
   [deliver] registry: applying 1/3: ConfigMap/registry-config
   [deliver] registry: waiting for PersistentVolumeClaim/registry-storage to be ready (1s)
   [deliver] registry: waiting for Deployment/registry to be ready (1s)
   [deliver] rust-hello: pushing to shop/rust-hello
   [deliver] rust-hello: pushed 127.0.0.1:5000/shop/rust-hello@sha256:9b2fbdad1c04…
   [deliver] done
   [plan] release shop-fbcc92695da7 (create): 7 create
   [plan] done
   [apply] shop-fbcc92695da7: applying
   [apply] shop-fbcc92695da7: applying 1/7: Secret/cache-credentials
   [apply] shop-fbcc92695da7: waiting for Deployment/cache to be ready (1s)
   [apply] done
   [checks] shop-fbcc92695da7: passed
   [checks] done
   deploy ready: release shop-fbcc92695da7
   ```

   In CI, `--auto-approve` plans and runs in one step.

5. **Run it again without changes.** Every stage is skipped by content
   identity; on `kind` this takes about 2 seconds:

   ```text
   [build] rust-hello: cached (plan 7cd633981e19)
   [build] skipped
   [deliver] rust-hello: present 127.0.0.1:5000/shop/rust-hello@sha256:9b2fbdad1c04…
   [deliver] skipped
   [plan] done
   [apply] shop-fbcc92695da7: unchanged, already deployed and ready
   [apply] skipped (unchanged)
   deploy ready: release shop-fbcc92695da7
   ```

6. **Check it and reach it.** `piceli status` reads the release's workloads
   and the declared forwards; `piceli access` forwards the ports (see
   {doc}`access`):

   ```sh
   piceli status examples/shop/app.py:pipeline
   piceli access examples/shop/app.py:pipeline --dashboard 9876
   ```

   `status` prints (exit code `0`; the forwards are `down` until `access`
   runs):

   ```text
   shop is UP  (namespace shop, context kind-shop)
   release    shop-fbcc92695da7  ready, apply at 2026-09-25T06:46:30+00:00
   workloads
     ready        Deployment/api    1/1  api=9b2fbdad15e1
     ready        Deployment/cache  1/1  cache=a7cee7c8178f
     ready        Deployment/web    1/1  web=9b2fbdad15e1
   access     down
     down      api          http://127.0.0.1:13080/  -> service/api:8080
     down      web          http://127.0.0.1:13000/  -> service/web:3000
   Start the forwards with: piceli access examples/shop/app.py:pipeline
   ```

7. **Change the source and deploy again.** Only what changed moves: a new
   build plan, a new image digest when the image differs, and a new release
   that rolls the workloads using it. An edit that produces a byte-identical
   image stops at the build stage.

## How stages are skipped

Each stage has a content identity; the run journal records it with the
stage's outputs.

| Stage | Content identity | Skipped when |
| --- | --- | --- |
| `inputs` | The build plan hash over every staged file, plus the sources' git identity (provenance only) and, with `--ref`, the pinned commits | Never; it is the cheap scan the other stages key on |
| `build` | The build plan hash (spec, staged files, Dockerfiles, invocations) | The last receipt has the same plan hash and each image the app uses is still in the local engine, or already delivered (its delivery receipt's digest is in the registry or node: built by another runner) |
| `deliver` | The image's config digest and the target repository | The registry serves the receipt's manifest digest (`HEAD`), or the node holds the config digest behind the content tag |
| `plan` | The release name: a fingerprint of the delivered digests, the rendered objects and the secret settings | Never; it reads live discovery |
| `apply` | The release name | That release is deployed and ready, the plan creates, deletes, adopts and replaces nothing, and no other field manager owns a desired field (drift) |
| `checks` | The release name | No checks are declared, or the apply was skipped and this release already passed its checks |

`apply` objects in an unchanged release are live objects whose form differs
only by server defaults that no dry run could confirm (secret-bound objects
are compared privately and are `no-op` when unchanged). An action that
removes fields an earlier release declared always runs. A change by another
client (`kubectl edit`, `scale`, `set image`) shows as drift and is
re-applied. `--reapply` forces the apply.

## The combined hash

`--plan` prints one hash over every stage's plan: the build plan hashes,
builders and network access, the delivery strategy and the known image
digests, the mirrored images (source digest, target repository, platform),
the registry release's actions and any live registry it adopts or replaces,
the release's actions (or, while the
images are not built yet, the fingerprint of the rendered app and the
preview's adopt/replace/delete set), the checks, `--until`, the target
identity, with `--ref`, the resolved commit of each pinned source and, with
`--env`, the environment's name and resolved override values (see
{doc}`environments`) and, when declared, the owner's `auto_approve` policy
(see {ref}`deploy-approval-policy`). `--approve HASH` re-plans and runs only when the hash is
unchanged; otherwise it is refused with `pipeline-plan-changed` and nothing
runs. Stages whose plan depends on earlier outputs (the release plan after a
build) run under that approval, within the limits of the preview below.

(deploy-preview)=

## Preview the release before the images exist

A release plan needs the image digests, which exist only after the build and
the delivery. So that the owner can see what a release would create, adopt,
replace or delete (and what blocks it) *before* approving a build and a
registry write, `--plan` computes a **preview** of the release plan with
placeholder images:

- Each build image that is not delivered yet is
  `pending-build.piceli.invalid/<image>@sha256:000…` (not built) or
  `pending-delivery.piceli.invalid/<image>@sha256:000…` (built, not
  delivered). The `.invalid` domain never resolves and the digest names no
  image, so a placeholder can never be pulled. Images already delivered and
  images pinned by digest are used as they are.
- Ownership is resolved exactly as for a real plan. An object that needs
  adoption or replacement refuses `--plan` (and `--approve`, before any
  build) with the release engine's `blocking` list. Each object's `suggest`
  names the Pipeline declaration that unblocks it
  (`Pipeline(adopt=["Deployment/web"])` or
  `Pipeline(replace=["Deployment/web"])`); the adoption becomes part of the
  pipeline, and so of the combined hash.
- The preview is **never approvable** and never persisted: no release, plan
  or secret file is written, and no secret value is generated or read. Its
  `preview_hash` only identifies it; `--approve <preview_hash>` is refused
  with `pipeline-preview-not-approvable`.
- **Placeholders never reach the cluster.** The preview sends only reads,
  plus the usual server dry runs (`dryRun=All` patches, which change
  nothing) for managed objects **without** a placeholder. Objects that carry
  a placeholder image get no dry run (`dry_run_skipped`, reason
  `dry-run-placeholder-image`) and are compared literally, so they show as
  `apply`. Secret-bound objects the release already manages also show as
  `apply`, because the preview never reads their stored values.
- The combined hash covers the preview's adopt/replace/delete set, not its
  placeholder digests. After delivery, the real release plan may not adopt,
  replace or delete any object the approved preview did not show (an object
  appeared or changed owner in between). If it would, the run stops at the
  plan stage with `pipeline-preview-changed` and nothing is applied: run
  `--plan` again (build and delivery are skipped), review the real release
  plan and approve its hash.

To review the real release plan before anything is applied, approve
`--until deliver` first, then plan again: the release plan is then computed
with the delivered digests.

(deploy-ref)=

## Deploy a commit, not the working tree

Without `--ref`, `piceli deploy` builds from the checkouts on disk, dirty or
not (a dirty source is refused unless its `inputs.toml` sets
`allow_dirty = true`). With `--ref` it builds the committed state instead, so
a shared or half-edited tree never leaks into a release:

```sh
piceli deploy deploy/app.py:pipeline --ref main --plan            # one source, or all in one repository
piceli deploy deploy/app.py:pipeline --ref api=v1.4.0 --ref web=main --plan
```

`--ref [SOURCE=]REV` names a source from the builds' `inputs.toml` and a
branch, tag or commit. A bare `--ref REV` pins every source when they are all
one repository. For each pinned source Piceli:

1. **resolves** `REV` in the source's repository to the full commit SHA
   (`deploy-ref-unknown` when it is not a local commit: fetch first);
2. **checks it out** with `git worktree add --detach` into a private
   temporary directory. A worktree is a real checkout: the repository's
   filters run (Git LFS files have their content), `export-ignore` attributes
   drop nothing, and hooks are disabled. Submodules are not checked out;
   declare each one as its own source;
3. **reads every build input inside that repository from the commit**: the
   source contexts, `build.toml`, `inputs.toml`, an inputs lock, the
   Dockerfile and contexts relative to the pipeline module. Sources without a
   `--ref` are read from disk as before;
4. **removes the worktrees** when the command ends, also after a failure,
   `Ctrl-C` or `SIGTERM` (a cancelled CI job). A process killed with
   `SIGKILL` leaves a directory that `git worktree prune` cleans up.

**The model runs from the working tree.** The pipeline module is imported
from disk, because its kubeconfig, state directory and secrets are local
files that are not in the commit. When the module's directory is inside a
pinned repository, its tracked Python files (the module and the `.py` files
beside it) must equal the pinned commit, or the deploy is refused with
`deploy-ref-model-differs`; so a release is exactly the commit's model built
from the commit's sources. Keep the pipeline in its own directory (such as
`deploy/`); other files the model reads at render time come from disk.

What the plan and the records show:

- `--plan` lists `inputs   ref <source>: <REV> = commit <sha>` and whether
  the model was checked. The JSON result has `refs` (source → SHA), and
  `stages.inputs` has `refs` (`{"ref", "commit"}` per source), `model`
  (`checked_against`) and the sources' identity (`dirty: false`).
- **The combined hash covers the SHAs, not `REV`.** The printed approval
  command pins them (`--ref api=<sha> --approve <hash>`). Approving with
  `--ref main` after `main` moved, or without `--ref`, is refused with
  `pipeline-plan-changed`: a plan for commit X never applies commit Y.
- The run journal records `refs`; `--resume` re-opens exactly those commits
  (it takes no `--ref`). A fresh build's receipt records the sources' commit
  and `refs`; a build whose staged files are unchanged is reused as a cache
  hit, whichever checkout produced it.
- A release created by the run records `provenance.sources` (commit, dirty,
  `ref`) next to its image set; `piceli release status` shows it. Deploys
  without `--ref` record the working tree's commit and `dirty` flag.

`piceli release plan|diff|apply --spec MODULE:ATTR` still compare with the
working tree's build inputs: after a `--ref` deploy of a commit the working
tree differs from, they refuse with `pipeline-not-delivered`. The commands on
catalogued releases (`status`, `rollback`, `check`, `secret show`) work
unchanged. For CI, see {doc}`ci`.

(delivery)=

## Delivery

| Strategy | Where images go | The release pulls |
| --- | --- | --- |
| `NodeLoopbackRegistry(port=5000)` | A {doc}`node_local_registry` released on its own (its own owner, never part of the app's release), pushed through a supervised port-forward | `127.0.0.1:<port>/<app>/<image>@sha256:…` |
| `NodeImport()` | The node's containerd, streamed (`docker://<node>?runtime=containerd` by default, or `target="ssh://…"`) | `<repository>:sha256-<12 hex of the config digest>` |
| `Registry("oci://host[:port]/prefix")` | An OCI registry (`credentials=`, `ca_file=`, `node_registry=`) | `<registry>/<prefix>/<image>@sha256:…` |

Both node strategies pin workloads that use a built image to that node
(`kubernetes.io/hostname`), unless the workload chooses its node itself. One
node-loopback registry holds a node's port: two pipelines on the same node
need different `port=` values. See {doc}`node_delivery` for the delivery
receipts. The release records the whole image set as its source identity
(`ReleaseSource(kind="oci-set", images={name: digest})`).

`NodeLoopbackRegistry` also takes `host_path="/srv/registry"` (keep the data
in a node directory) or `existing_claim="name"` (keep it on a claim the
registry release never creates, changes or deletes) instead of its own
`<name>-storage` claim.

(deploy-mirror)=

### Mirror third-party images

An image the app does not build (a cache, a database) is normally pulled by
the node from its public registry. A node without internet access, or one
that should pull everything from the node-loopback registry, needs a copy.
Declare it on the delivery:

```python
REDIS = "docker.io/library/redis:7.4@sha256:<index digest>"

app.deployment("cache", image=REDIS)
pipeline = Pipeline(
    app,
    target,
    build=images,
    deliver=NodeLoopbackRegistry(port=5000, mirror=[REDIS]),
)
```

- **Only digest-pinned references** are accepted. A tag without a digest is
  refused with `pipeline-mirror-not-pinned`, before anything runs. A tag next
  to the digest (`redis:7.4@sha256:…`) is informational. Short names are
  normalized like `docker pull` does (`redis` is
  `docker.io/library/redis`), so the app and `mirror=` may spell the image
  differently.
- **The deliver stage copies each image by digest** on the machine running
  Piceli, over the OCI distribution API (no `docker pull`): it reads the
  manifest by digest from the source registry, checks that its bytes hash to
  that digest (`mirror-digest-mismatch` otherwise), copies the missing blobs
  through the same supervised port-forward as built images, writes the
  manifests and reads them back. The copy lives at
  `127.0.0.1:<port>/mirror/<registry>/<repository>@<same digest>` and a
  receipt (`piceli.mirror-delivery.v1`) is written to
  `state_dir/mirrors/`.
- **Multi-arch images keep their digest.** For an image index the index itself
  is copied (so the digest the app pinned is the digest it pulls) with only
  the manifest for the registry node's platform (`status.nodeInfo`, for
  example `linux/arm64`). The pipeline configures the registry to accept an
  index that holds only that platform (`validation.manifests.indexes` in its
  `config.yml`; adding the first mirror restarts the registry once). An index
  without the node's platform, or a single-platform image for another
  platform, fails with `mirror-platform-unavailable` before anything is
  written.
- **The release pulls the copy.** Every container whose image is a mirrored
  image is rewritten to the copy, with the same digest, and pinned to the
  registry node like a workload using a built image. The release still records
  the original digest in its `oci-set` source.
- **Skipped when present.** Registries are content-addressed: a copy the
  registry already serves (with a receipt) is `present` in the plan and not
  copied again; a copy that exists without a receipt is verified and recorded
  (`already-present`).
- **Credentials** for a private source come from a private (`0600`) JSON file
  per registry, `mirror_credentials={"ghcr.io": "ghcr.json"}`, in the format
  of {doc}`node_delivery` (`{"username": …, "password": …}` or
  `{"token": …}`); every other source is pulled anonymously. Credentials are
  sent only to that registry's own origin, never to a redirect target (blob
  storage), and are never written to a plan, event or receipt.
- **Part of the combined hash**: the list (source digest, target repository
  and platform) is in the deliver stage's plan, so adding or changing a mirror
  needs a new approval.

`Registry("oci://host/prefix", mirror=[…])` copies to
`prefix/mirror/<registry>/<repository>` in that registry, with every platform
of an index (the target registry may require them all), and the release
pulls `<node_registry>/prefix/mirror/…@<digest>`. `NodeImport` has no
`mirror=`: it imports images from the local engine; use
`NodeLoopbackRegistry` for mirrors.

The plan shows each mirror under `deliver`:

```text
  deliver  mirror docker.io/library/redis@sha256:4d3a1f0a6d2c: mirror (linux/arm64)
```

and the `--json` result carries `stages.deliver.mirrors` (keyed by the
canonical reference, with `action` `mirror` or `present`, `repository`,
`reference`, `platform` and `used`, false for an entry the app does not use).

(deploy-registry-takeover)=

### Take over an existing node-loopback registry

A registry that already runs on the node (created with `kubectl`, or by
another release) holds the loopback port: a second registry could never
start. `piceli deploy --plan` reads the namespace's Deployments and refuses
with `pipeline-registry-takeover-required`, naming the Deployment, instead of
failing later with `pipeline-registry-not-ready`. Take it over explicitly:

```python
deliver = NodeLoopbackRegistry(
    port=5000,
    adopt="old-registry",  # the live Deployment's name
    host_path="/var/lib/registry",  # where its data already is
)
```

- **`adopt="NAME"`** makes `NAME` the registry objects' name and adopts the
  live Deployment (and `NAME-config`, `NAME-storage` when they exist) by
  ownership transfer, like `piceli release --adopt`: nothing is deleted, the
  pod is updated in place (`Recreate`), and its data, including every image
  already pushed, stays on the node. The live Deployment's selector is kept
  (Kubernetes never changes it), and a live `RollingUpdate` strategy becomes a
  one-pod-at-a-time rolling update (`maxSurge: 0`, `maxUnavailable: 1`, the
  same effect as `Recreate`, which Kubernetes refuses to switch to while
  another client owns the rolling-update settings). The live registry must be compatible: host
  network, the same port and node, and its data on the declared storage
  (`host_path=`, `existing_claim=`, or the claim `NAME-storage`). Otherwise
  the plan is refused with `pipeline-registry-incompatible`, which lists the
  differences.
- **`replace="NAME"`** is for a registry that cannot be adopted in place
  (another port, selector or storage): the release engine writes a backup of
  the live Deployment, deletes it and creates the registry's own. Storage is
  never deleted: a host directory or claim stays where it is, and the plan
  says whether the new registry uses it (`data: kept`) or not
  (`not-carried-over`). As everywhere in Piceli, replace needs its own flag;
  retained objects (the claim) are only ever adopted.
- `inherited_owners=["old-owner"]` lets the registry release take over a
  retained claim that another Piceli release owns.
- The plan shows exactly what happens (`adopt Deployment/old-registry`,
  `replace Deployment/old-registry`, `create ConfigMap/old-registry-config`)
  and, under `deliver.registry.existing` in the JSON, the live registry's port,
  node, storage and whether its data is kept. Both are part of the combined
  hash.
- After the first run the registry is the release's own; a standing
  `adopt=`/`replace=` is then a no-op (`existing.action: managed`).

## Checks and rollback

`checks=` takes one check or a list, run after a successful apply through the
`piceli.checks` runner (`run_checks(checks, context)` returning an object with
`passed` and `results`). The checks get a `piceli.checks.CheckContext` built
from the pipeline's target (kubeconfig, context, namespace, transport, exec
policy), the release and the delivered image references; a custom
`check_runner` receives the `piceli.pipeline.CheckContext` itself.
A run is `ready` only when the checks pass. With
`rollback_on_failed_checks=True` a failed check re-applies the previous
release; the rollback is journaled in the run and the result's state is
`rolled-back`. `Checks` is importable from `piceli` next to `Pipeline`
(`from piceli import Checks`); see {doc}`checks` for every check type.

(deploy-approval-policy)=

## Let a policy approve routine plans

The owner can declare in the pipeline which plans may run without them
approving the hash, for example an agent or a CI job that ships routine
image updates:

```python
from piceli import ApprovalPolicy, Pipeline

pipeline = Pipeline(
    app,
    target,
    build=images,
    deliver=NodeLoopbackRegistry(),
    auto_approve=ApprovalPolicy(
        allow={"create", "apply", "no-op"},  # the default
        deny={"cluster_scoped"},  # optional: remove classes from allow
        max_objects=10,  # optional: at most this many changes
    ),
)
```

```console
$ piceli deploy deploy/app.py:pipeline --approve-if-policy --json
```

`--approve-if-policy` plans every stage like `--plan`, then runs the plan
only when **every** action is inside the policy: the release's actions (or,
before the images exist, the placeholder preview's), the node-loopback
registry's actions and a registry takeover. The run records
`"approved_by": "policy"` in its journal and result, and the release plan
made after delivery is checked against the policy again before anything is
applied (`approval-policy-exceeded`, exit `2`, nothing applied). A plan
outside the policy runs nothing: the command prints the plan, each
`policy.violations` item (`delete Service/web`, `cluster_scoped
ClusterRole/x`, `max_objects: 12 changed objects > 10`) and the usual
`--approve <combined hash>` command, and exits `3` with
`"reason": "approval-policy-exceeded"`.

The policy can only narrow what runs unattended:

- `delete`, `replace` and `adopt` are never inside a policy; `allow` naming
  one is refused (`approval-policy-invalid`), so they always need the hash.
- `cluster_scoped` objects and `drift` (a desired field another manager wrote,
  which the apply overwrites) are outside unless `allow` names them.
- `no-op` actions change nothing and are always inside.
- The policy is part of the combined hash (and of the release plan hash), so
  a changed policy makes earlier plans unapprovable and resuming a run
  refuses it (`pipeline-resume-changed`).
- There is no command-line flag that sets or widens a policy;
  `--approve-if-policy` without one is refused (`approval-policy-missing`),
  and it cannot be combined with `--plan`, `--approve`, `--auto-approve`,
  `--resume` or `--apply` (`deploy-flags-conflict`). Secrets, exec
  credential plugins (`allow_exec`) and the release's own refusals work as
  without a policy.

Keep the policy in reviewed code: an agent must never add or widen it (see
{doc}`agents`).

## Plan here, apply there

`--plan --out FILE` also writes a portable plan file
(`piceli.deploy-plan-file.v1`): the combined hash, every stage's plan, the
`--ref` commits, the pipeline and observed target identity, and the build,
delivery and mirror receipts it used. Another runner applies it with no
checkout of the state and no build cache:

```sh
piceli deploy examples/shop/app.py:pipeline --ref main --plan --out deploy-plan.json
piceli deploy --apply deploy-plan.json --approve <combined hash>   # on any runner
```

`--apply` takes the pipeline, stages, commits, `--reapply` and `--env` from the file
and plans again against live state; it runs only when the hash is still the
approved one, and refuses a file for another pipeline or cluster (or an
`--env` other than the file's: `deploy-plan-file-mismatch`). Across
runners, use shared state (below): the release catalog and secret store of
the plan runner are needed to compute the same plan.

## Share the state between runners

`Pipeline(..., state="cluster")` keeps the run journal, receipts, release
catalog, execution journal and secret store in the release namespace, behind
a release lock (a Lease with fencing and stale-owner takeover), so any runner
can plan, apply, resume or roll back, and two deployers of one release never
interleave. `state_dir` becomes a working copy. See {doc}`state`.

## Resume an interrupted run

A run is journaled under `state_dir/runs/` after every stage change. When a
stage fails or the run is interrupted, fix the cause and run:

```sh
piceli deploy examples/shop/app.py:pipeline --resume
```

It continues the latest unfinished run at its first unfinished stage with the
approved plan: finished stages keep their receipts, an interrupted release
execution is resumed with the same grant, and a build whose staged files
changed since the approval is refused (`pipeline-resume-changed`). A run
planned with `--ref` is resumed from the same commits (checked out again),
never from a branch's newer head. With `state="cluster"` any runner resumes
it (the journal is in the namespace); a build that finished elsewhere but
was not delivered is built again first. A run that
finished, rolled back or stopped at `--until` has nothing to resume: plan a
new run, and unchanged stages are skipped.

## Run summaries and disk use

When a run ends, whatever the outcome, it writes
`state_dir/runs/<run id>/summary.json` (for agents; schema
`docs/schemas/piceli-run-summary-v1.schema.json`) and `summary.md` (for
people and CI job summaries): commits, image digests and sizes, the plan's
action classes and changed fields, checks, the failure's code and stage
timings, never secret values. The result names them (`summary`), and
`piceli runs MODULE:ATTR` lists the runs. `Pipeline(cache_budget="20GiB")`
keeps the state directory within a budget after each run; `piceli cache
status|prune` and `piceli doctor` show and free disk on the runner. See
{doc}`maintenance`.

## Operate the release: rollback, status and secrets

`piceli deploy` never writes a `release.toml`; every `piceli release`
subcommand takes the pipeline instead, as `--spec MODULE:ATTR` (the same
target syntax as `piceli deploy`, `piceli status` and `piceli access`):

```sh
piceli release status --spec examples/shop/app.py:pipeline
piceli release rollback previous --spec examples/shop/app.py:pipeline       # plan; prints the hash
piceli release rollback previous --spec examples/shop/app.py:pipeline --approve <plan hash>
piceli release secret show cache_password --spec examples/shop/app.py:pipeline --reveal
```

The commands resolve exactly what `piceli deploy` uses: the release state in
`state_dir/release`, the release name (the app's name), owner and field
manager, the target (kubeconfig, context, namespace, exec policy), the secret
generators and the composition. They never build or deliver:

- `rollback`, `resume`, `stop`, `check`, `status` and `secret show` work on
  the catalogued releases. A rollback re-applies the archived composition
  with the image digests recorded in it (the `oci-set` source).
- `plan`, `preview`, `diff` and `apply` plan a release from the pipeline's
  current model with the images `piceli deploy` last built from the
  **current** build inputs and delivered. When a build input changed since,
  or an image was never delivered, they are refused with
  `pipeline-not-delivered`: run `piceli deploy`, which builds only what
  changed.
- `apply`, `rollback`, `resume` and `stop` hold the pipeline's run lock, so
  they are refused with `pipeline-locked` while a `piceli deploy` of the same
  state directory runs (with `state="cluster"`: of the same release, on any
  runner); since 0.6.0 `plan` and `check` hold it too.
- The pipeline's `checks=` (`piceli.checks` declarations) run after readiness
  of an `apply`, `rollback` or `resume`, and `release check` runs them now;
  `rollback_on_failed_checks=True` applies as for `piceli deploy`.

The node-loopback registry is a separate release in `state_dir/registry`
that `piceli deploy` manages; the release commands operate on the app's
release only. After a manual rollback, the next `piceli deploy` re-applies
the pipeline's current release (it converges on the model).

## Managed clusters

A target whose kubeconfig user runs an exec credential plugin (GKE, EKS,
AKS, OIDC) is refused with `exec-auth-not-allowed` unless the `Target` opts
in, with the same keys and semantics as `[target]` in a `release.toml`:

```python
target = Target.kubeconfig(
    "gke.kubeconfig",
    context="gke_proj_zone_prod",
    namespace="shop",
    allow_exec=True,
    exec_sha256="sha256:…",  # optional pin of the resolved plugin
    exec_pass_env=["CLOUDSDK_CONFIG"],  # optional extra variables
    exec_timeout_seconds=60,  # optional, at most 300
)
```

The policy is used by `piceli deploy` (plan, apply, checks), `piceli status`,
`piceli access` and the `piceli release … --spec MODULE:ATTR` commands. See
{doc}`managed_clusters`.

## If it fails

| Output | Meaning | Next step |
| --- | --- | --- |
| Exit `3`, `"state": "approval-required"` | No `--approve`/`--auto-approve` and no terminal to confirm on | Review the plan, then `--approve <combined hash>` |
| `pipeline-plan-changed` | Something changed since `--plan` | Plan again and approve the new hash |
| `pipeline-load-failed` | Importing the pipeline's module raised (`message`: the exception's type and text, never a traceback) | Fix the module until it imports; `PICELI_DEBUG=1` prints the traceback on stderr |
| `resource-requires-adoption` (or `plan-blocked`) with `blocking` and `"preview"` | The release preview needs adoption or replacement of existing objects; nothing was built | Do what `blocking[].suggest` names: `Pipeline(adopt=["Kind/name"])` (or `replace=`), after the owner chose it, or delete the objects, then plan again. `deploy` takes no `--adopt`/`--replace` |
| `pipeline-preview-not-approvable` | `--approve` got the preview's `preview_hash` | Approve the `combined_hash` |
| `pipeline-preview-changed` | After delivery the release plan adopts, replaces or deletes beyond the approved preview; nothing was applied | Plan again (build and delivery are skipped) and approve the real plan |
| `pipeline-image-not-pinned` | An image is a movable tag | Pin it by digest or build it |
| `pipeline-release-refused` with `blocking` objects | The release needs adoption or replacement of existing objects | Add `adopt=["Kind/name"]` (or `replace=`) to the `Pipeline`, as `blocking[].suggest` names; see {doc}`release_cli` |
| `build-failed`, `smoke-failed`, `smoke-output-mismatch` (exit `1`) | The build or its smoke check failed (for a mismatch, stderr shows an excerpt of the unmatched output) | Read `state_dir/builds/<name>/build.log`, fix, deploy again |
| `pipeline-registry-not-ready` | The node-loopback registry did not start (its port may be held by a process outside the namespace) | Choose another `port=` or free it, then `--resume` |
| `pipeline-registry-takeover-required` | A live registry holds the port, or a Deployment with the registry's name is not the registry release's | `NodeLoopbackRegistry(adopt="NAME")` or `replace="NAME"` (see {ref}`deploy-registry-takeover`), or another `port=` |
| `pipeline-registry-incompatible` | `adopt=` names a registry on another port, node or storage | Declare matching `host_path=`/`existing_claim=`/`port=`, or `replace=` |
| `pipeline-mirror-not-pinned` | A `mirror=` entry has no digest | Pin it: `repository@sha256:…` |
| `mirror-platform-unavailable` (exit `1`) | The mirrored image has no manifest for the node's platform | Pin an image that supports the node |
| `registry-unauthorized` at `deliver` | A mirror source needs a login | Add `mirror_credentials={"registry": "file.json"}` |
| `pipeline-apply-crashloop` (exit `1`) | A workload's new pods cannot start: crash loop, image pull or configuration error, or `crash_restarts` restarts. The apply stopped at once; stderr has one line per cause and the result's `diagnosis` the redacted log tails and events | Fix the cause, then deploy again (or `--resume`); `piceli release status --spec MODULE:ATTR --run RUN_ID` shows the causes again ({ref}`deploy-diagnosis`) |
| `pipeline-apply-not-ready` (exit `1`) | The release did not become ready in time (`diagnosis` lists what its pods show, when anything) | Fix the workload (image, probe, claim), then `--resume` |
| `pipeline-checks-failed` (exit `1`) | A check failed; see `checks.results` and `checks.rollback` in the run | Fix the app and deploy again |
| `pipeline-locked` | Another run uses the state directory (with `state="cluster"`: the release, see `lock.holder`) | Wait, then retry |
| `deploy-plan-file-mismatch`, `deploy-plan-target-mismatch` | `--apply`: the hash, pipeline or cluster is not the plan file's | Approve the file's hash with its pipeline and kubeconfig |
| `deploy-ref-unknown` | `--ref` names no local commit | `git fetch`, or pass a full SHA |
| `deploy-ref-source-unknown`, `deploy-ref-ambiguous`, `deploy-ref-invalid` | `--ref` names no declared source, is bare with several repositories, or is malformed | `--ref SOURCE=REV` with a name from `inputs.toml` |
| `deploy-ref-model-differs` | With `--ref`, the pipeline's Python files differ from the pinned commit | Commit them, or deploy from a checkout of that commit |
| `deploy-ref-checkout-failed` | `git worktree add` failed (disk, LFS objects, permissions) | Fix the cause on stderr, retry |
| `pipeline-not-delivered` | `piceli release plan/diff/apply --spec MODULE:ATTR` needs images of the current sources | Run `piceli deploy` |
| `exec-auth-not-allowed` | The kubeconfig user runs an exec plugin and the `Target` does not allow it | Review the plugin, then `Target.kubeconfig(…, allow_exec=True)` |

Every code is explained by `piceli explain <code>` and in
{doc}`reference/errors`.

(deploy-diagnosis)=
### A workload that cannot start

While the apply waits for a Deployment, StatefulSet, DaemonSet, Job or Pod to
become ready, Piceli looks at the pods of the revision being rolled out every
two seconds. A container in `CrashLoopBackOff` (`Init:CrashLoopBackOff` for an
init container), `ImagePullBackOff`, `ErrImagePull`, `InvalidImageName`,
`CreateContainerConfigError`, `CreateContainerError` or `RunContainerError`,
one that restarted `crash_restarts` times (default 3), a failed Job or a
failed Pod stops the apply **at once**, instead of at `readiness_seconds`:

```text
[apply] failed (pipeline-apply-crashloop)
failed: release shop-1a2b3c4d5e6f cannot start (apply-crashloop) (pipeline-apply-crashloop)
  2 workloads not starting:
    cache  cache  exit 101  "config file must be owner-only"
    web    web    exit 1    "fatal: cannot open catalog"
```

One line per cause: workload, container, exit code (or the reason when the
container never ran, such as `ImagePullBackOff`) and the last log line (or the
status message or latest event). The result JSON adds `diagnosis`
(`piceli.diagnosis.v1`): per workload, each cause's pod, container, reason,
exit code, restart count, the last 20 log lines and the latest 5 events.
Every text read from the cluster is **redacted** first: any value in the
release's secret store (and its decoded form) and anything that looks like a
secret (`password=…`, bearer tokens, URL credentials, JSON Web Tokens, private
key markers) becomes `[REDACTED]`, and lines are cut to 300 characters.

The causes are kept in the private execution journal, so they can be read
again later without the cluster:

```sh
piceli release status --spec app.py:pipeline --run RUN_ID   # or an execution id
piceli explain --run RUN_ID --spec app.py:pipeline          # the same
```

The run journal (`state_dir/runs/`) and `release status` keep only the compact
causes (workload, container, reason, exit code, restarts), never log lines.

Only pods of the new revision count: an old pod that crash-loops while the new
one starts never fails the apply, and when the revision cannot be told apart
(no ReplicaSet yet, the credentials cannot read pods) Piceli simply waits, as
before. Reading pods, their logs (`pods/log`), events and ReplicaSets needs
`get`/`list` on them in the namespace; without it the diagnosis is skipped.

A failed apply is not rolled back automatically, exactly as for an apply that
does not become ready in time: `rollback_on_failed_checks` only acts when the
release became ready and a check failed. Roll back by hand with
`piceli release rollback previous --spec MODULE:ATTR`, or fix and deploy
again. An app that is expected to crash a few times while its dependencies
start can raise `crash_restarts` or turn the early stop off:

```python
Pipeline(..., execution={"fail_fast": False})  # wait for readiness_seconds
Pipeline(..., execution={"crash_restarts": 10})  # tolerate more restarts
```

(`[execution] fail_fast = false` / `crash_restarts = 10` in a `release.toml`.)
Even then, an apply that times out reports what the pods show in `diagnosis`.

## Command contract

`piceli deploy TARGET [--ref [SOURCE=]REV]... [--plan [--out FILE]] [--until STAGE] [--resume] [--approve HASH | --auto-approve] [--reapply] [--json]`

`piceli deploy [TARGET] --apply FILE --approve HASH [--json]`

| Argument | Type | Default | Meaning |
| --- | --- | --- | --- |
| `TARGET` | text | required | `path/to/file.py:ATTR` or `package.module:ATTR` naming a `Pipeline` |
| `--plan` | flag | off | Plan every stage, print the combined hash, execute nothing |
| `--out FILE` | path | none | With `--plan`: also write the portable plan file (new in 0.6.0) |
| `--apply FILE` | path | none | Apply a plan file (with `--approve` its hash); `TARGET` defaults to the file's (new in 0.6.0) |
| `--until STAGE` | text | `checks` | Stop after `inputs`, `build`, `deliver`, `plan`, `apply` or `checks` (bound to the hash) |
| `--resume` | flag | off | Continue the latest unfinished run; takes no other planning flag |
| `--approve HASH` | text | none | Execute exactly this combined plan |
| `--auto-approve` | flag | off | Plan and execute without confirmation (CI) |
| `--reapply` | flag | off | Apply even when the release is deployed, ready and not drifted |
| `--ref [SOURCE=]REV` | text, repeatable | none | Build SOURCE from commit REV in a temporary worktree ({ref}`deploy-ref`); bound to the hash by SHA |
| `--json` | flag | off | Stream one JSON event per stage change on stdout |

- **Side effects.** Reads the pipeline module, build specs and sources, the
  local Docker engine and the cluster (explicit kubeconfig). With `--ref`,
  runs `git` in the sources' repositories and adds temporary worktrees
  (removed on exit). `--plan` writes
  only the pipeline's `state_dir` (pending release plans, as
  `piceli release plan` does); before the images exist its release preview
  sends the cluster only reads and `dryRun=All` patches of objects without a
  placeholder image. A run also writes images to the local engine, a
  registry or a node, and applies releases to the cluster.
- **Approval.** Required: `--approve <combined hash>` from `--plan`,
  `--auto-approve`, or typing the hash's first 12 characters on a terminal.
  `--resume` continues an approved run and needs no new approval.
- **Idempotency.** Every stage is idempotent and skipped when unchanged, so
  re-running the same command is safe; after a failure, `--resume` continues
  the same run.
- **Exit codes.** `0` ready (or planned, or stopped at `--until`), `1` a
  stage ran but did not succeed, `2` rejected, `3` approval required.
- **Output.** Human text on stderr. On stdout, the result object; with
  `--json`, first one line per stage change, then the result. Every line
  follows `docs/schemas/piceli-deploy-event-v1.schema.json`
  (`"schema": "piceli.deploy-event.v1"`):

  ```json
  {"schema": "piceli.deploy-event.v1", "event": "stage", "run_id": "20260924T221851121638Z-fc0a493b", "stage": "apply", "state": "skipped", "at": "2026-09-24T22:18:52.901Z", "detail": {"release": "shop-fbcc92695da7", "why": "unchanged"}}
  {"schema": "piceli.deploy-event.v1", "event": "result", "state": "ready", "run_id": "20260924T221851121638Z-fc0a493b", "combined_hash": "9ddce524…", "release": "shop-fbcc92695da7", "images": {"cache": "sha256:a7cee7c8…", "rust-hello": "sha256:9b2fbdad…"}, "stages": {"inputs": "done", "build": "skipped", "deliver": "skipped", "plan": "done", "apply": "skipped", "checks": "skipped"}}
  ```

  Stage states: `planned`, `running`, `done`, `skipped`, `failed`,
  `rejected`, `interrupted`. Result states: `planned`, `approval-required`,
  `ready`, `stopped`, `failed`, `rolled-back`, `interrupted`, `rejected`
  (with `reason`, the failed `stage` and any `blocking` objects). New in
  0.6.0: a planned result written with `--out` adds `plan_file`, and a
  `pipeline-locked` rejection adds `lock` (`holder`, `expires_in`).

  While the images are not delivered, `stages.plan` keeps
  `"state": "pending"` and adds `preview` (new in 0.5.0): `approvable`
  (always `false`), `placeholders` (image → `pending-build` or
  `pending-delivery`), `state` (`previewed`), `summary`, `changes`, `drift`,
  `authorized` (adopt/replace), `dry_run_skipped`, `dry_run_unavailable` and
  `preview_hash`. A refusal of the preview adds `"stage": "plan"` and
  `preview` (`approvable`, `placeholders`) next to `blocking`.
