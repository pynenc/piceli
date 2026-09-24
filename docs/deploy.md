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

## Prerequisites

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
   :lines: 17-
   ```

   - `Target.kubeconfig(...)` names the cluster, namespace and nodes. Relative
     paths (the kubeconfig, the build spec, `state_dir`) resolve from the
     directory of the file that declares them.
   - `Build.spec(...)` wraps a `build.toml` (see {doc}`containerized_builds`);
     `Build.dockerfile(...)` builds one image per Dockerfile target stage.
     `images["rust-hello"]` is a *handle*: a placeholder that `piceli deploy`
     replaces with the delivered, digest-pinned reference. Every other image
     must be pinned by digest.
   - `Secrets(...)` declares generators (`Random`, `Template`, `Static`,
     `TlsCa`, or any `[secrets.*]` type from {doc}`secrets`); `secrets.ref(name)`
     is an opaque reference the release binds to the real value at apply time.
   - `deliver=` is `NodeLoopbackRegistry()`, `NodeImport()` or
     `Registry("oci://host/prefix")` (see {ref}`delivery`).

2. **Render it without a cluster** (build handles show as placeholders):

   ```sh
   piceli render examples/shop/app.py:app --namespace shop
   ```

3. **Plan every stage.** Nothing is built, pushed or applied:

   ```sh
   piceli deploy examples/shop/app.py:pipeline --plan
   ```

   Expected output (stderr; the result object is on stdout):

   ```text
   deploy plan for examples/shop/app.py:pipeline (until checks):
     inputs   rust-hello: 3 staged file(s), plan 7cd633981e19
     inputs   source piceli: 485a4dc88cee
     build    rust-hello: build (linux/arm64, builder 8fa55b2f3ddf, network none)
     deliver  registry shop-registry-d0c1b1b7947f: create ConfigMap/registry-config, create PersistentVolumeClaim/registry-storage, create Deployment/registry
     deliver  rust-hello: pending-build
     plan     after delivery (the release plan needs the image digests)
     apply    pending
     checks   0 check(s) (rollback on failure)
   combined hash: fab781d97321848dc074bd293a208e1057bb5cdf3b79afc12d906b7c83e6ec25
   approve with: piceli deploy examples/shop/app.py:pipeline --approve fab781d9…
   ```

4. **Approve the combined hash** after reviewing the plan:

   ```sh
   piceli deploy examples/shop/app.py:pipeline --approve <combined hash>
   ```

   Expected output:

   ```text
   [inputs] done
   [build] rust-hello: building (log: …/.piceli-deploy/builds/rust-hello/build.log)
   [build] rust-hello: [piceli] 3/3 linux/arm64 smoke:rust-hello: succeeded in 0.324s
   [build] done
   [deliver] registry shop-registry-d0c1b1b7947f: applying
   [deliver] rust-hello: pushing to shop/rust-hello
   [deliver] rust-hello: pushed 127.0.0.1:5000/shop/rust-hello@sha256:9b2fbdad…
   [deliver] done
   [plan] release shop-fbcc92695da7 (create): 7 create
   [plan] done
   [apply] shop-fbcc92695da7: applying
   [apply] done
   [checks] skipped (no checks)
   deploy ready: release shop-fbcc92695da7
   ```

   In CI, `--auto-approve` plans and runs in one step.

5. **Run it again without changes.** Every stage is skipped by content
   identity; on `kind` this takes about 2 seconds:

   ```text
   [build] rust-hello: cached (plan 7cd633981e19)
   [build] skipped
   [deliver] rust-hello: present 127.0.0.1:5000/shop/rust-hello@sha256:9b2fbdad…
   [deliver] skipped
   [plan] done
   [apply] shop-fbcc92695da7: unchanged, already deployed and ready
   [apply] skipped (unchanged)
   deploy ready: release shop-fbcc92695da7
   ```

6. **Change the source and deploy again.** Only what changed moves: a new
   build plan, a new image digest when the image differs, and a new release
   that rolls the workloads using it. An edit that produces a byte-identical
   image stops at the build stage.

## How stages are skipped

Each stage has a content identity; the run journal records it with the
stage's outputs.

| Stage | Content identity | Skipped when |
| --- | --- | --- |
| `inputs` | The build plan hash over every staged file, plus the sources' git identity (provenance only) | Never; it is the cheap scan the other stages key on |
| `build` | The build plan hash (spec, staged files, Dockerfiles, invocations) | The last receipt has the same plan hash and its images are still in the local engine |
| `deliver` | The image's config digest and the target repository | The registry serves the receipt's manifest digest (`HEAD`), or the node holds the config digest behind the content tag |
| `plan` | The release name: a fingerprint of the delivered digests, the rendered objects and the secret settings | Never; it reads live discovery |
| `apply` | The release name | That release is deployed and ready, the plan creates, deletes, adopts and replaces nothing, and no other field manager owns a desired field (drift) |
| `checks` | The release name | No checks are declared, or the apply was skipped and this release already passed its checks |

`apply` objects in an unchanged release are live objects whose form differs
only by server defaults, and secret-bound objects (a plan never compares
private values). A change by another client (`kubectl edit`, `scale`,
`set image`) shows as drift and is re-applied. `--reapply` forces the apply.

## The combined hash

`--plan` prints one hash over every stage's plan: the build plan hashes,
builders and network access, the delivery strategy and the known image
digests, the registry release's actions, the release's actions (or, while the
images are not built yet, the fingerprint of the rendered app), the checks,
`--until` and the target identity. `--approve HASH` re-plans and runs only
when the hash is unchanged; otherwise it is refused with
`pipeline-plan-changed` and nothing runs. Stages whose plan depends on
earlier outputs (the release plan after a build) run under that approval.

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

## Checks and rollback

`checks=` takes one check or a list, run after a successful apply through the
`piceli.checks` runner (`run_checks(checks, context)` returning an object with
`passed` and `results`). The context is a `piceli.pipeline.CheckContext`
(target, kubeconfig, context, namespace, release, delivered image references).
A run is `ready` only when the checks pass. With
`rollback_on_failed_checks=True` a failed check re-applies the previous
release; the rollback is journaled in the run and the result's state is
`rolled-back`. Without `piceli.checks` installed, a pipeline that declares
checks is refused at the checks stage with `pipeline-checks-unavailable`; the
example only declares its check when the module is available.

## Resume an interrupted run

A run is journaled under `state_dir/runs/` after every stage change. When a
stage fails or the run is interrupted, fix the cause and run:

```sh
piceli deploy examples/shop/app.py:pipeline --resume
```

It continues the latest unfinished run at its first unfinished stage with the
approved plan: finished stages keep their receipts, an interrupted release
execution is resumed with the same grant, and a build whose staged files
changed since the approval is refused (`pipeline-resume-changed`). A run that
finished, rolled back or stopped at `--until` has nothing to resume: plan a
new run, and unchanged stages are skipped.

## If it fails

| Output | Meaning | Next step |
| --- | --- | --- |
| Exit `3`, `"state": "approval-required"` | No `--approve`/`--auto-approve` and no terminal to confirm on | Review the plan, then `--approve <combined hash>` |
| `pipeline-plan-changed` | Something changed since `--plan` | Plan again and approve the new hash |
| `pipeline-image-not-pinned` | An image is a movable tag | Pin it by digest or build it |
| `pipeline-release-refused` with `blocking` objects | The release needs adoption or replacement of existing objects | Add `adopt=["Kind/name"]` (or `replace=`) to the `Pipeline`; see {doc}`release_cli` |
| `build-failed`, `smoke-failed` (exit `1`) | The build or its smoke check failed | Read `state_dir/builds/<name>/build.log`, fix, deploy again |
| `pipeline-registry-not-ready` | The node-loopback registry did not start (often its port is taken on the node) | Choose another `port=` or free it, then `--resume` |
| `pipeline-apply-not-ready` (exit `1`) | The release did not become ready | Fix the workload (image, probe, claim), then `--resume` |
| `pipeline-checks-failed` (exit `1`) | A check failed; see `checks.results` and `checks.rollback` in the run | Fix the app and deploy again |
| `pipeline-locked` | Another run uses the state directory | Wait, then retry |

Every code is explained by `piceli explain <code>` and in
{doc}`reference/errors`.

## Command contract

`piceli deploy TARGET [--plan] [--until STAGE] [--resume] [--approve HASH | --auto-approve] [--reapply] [--json]`

| Argument | Type | Default | Meaning |
| --- | --- | --- | --- |
| `TARGET` | text | required | `path/to/file.py:ATTR` or `package.module:ATTR` naming a `Pipeline` |
| `--plan` | flag | off | Plan every stage, print the combined hash, execute nothing |
| `--until STAGE` | text | `checks` | Stop after `inputs`, `build`, `deliver`, `plan`, `apply` or `checks` (bound to the hash) |
| `--resume` | flag | off | Continue the latest unfinished run; takes no other planning flag |
| `--approve HASH` | text | none | Execute exactly this combined plan |
| `--auto-approve` | flag | off | Plan and execute without confirmation (CI) |
| `--reapply` | flag | off | Apply even when the release is deployed, ready and not drifted |
| `--json` | flag | off | Stream one JSON event per stage change on stdout |

- **Side effects.** Reads the pipeline module, build specs and sources, the
  local Docker engine and the cluster (explicit kubeconfig). `--plan` writes
  only the pipeline's `state_dir` (pending release plans, as
  `piceli release plan` does). A run also writes images to the local engine,
  a registry or a node, and applies releases to the cluster.
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
  (with `reason`, the failed `stage` and any `blocking` objects).
