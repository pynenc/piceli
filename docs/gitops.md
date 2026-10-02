# GitOps: Piceli's controller and manifest export

Piceli runs the GitOps loop itself. `piceli gitops enable` installs a
controller in the cluster that polls a Git repository and keeps
[one environment per branch](#gitops-controller) at the branch's head,
deployed with the same plans, approvals, builds and checks as `piceli deploy`;
`piceli env`, `piceli envs` and `piceli promote` operate on those
environments (see {doc}`environments`), and the {doc}`web UI <ui>` shows the
controller and its pending approvals.

For clusters that apply manifests some other way, Piceli also exports what it
models: `piceli publish` pushes the rendered manifests as an OCI artifact and
`piceli render --out DIR` writes them as files to commit to Git. This page
says exactly what the plan, approval and journal no longer cover once
something other than Piceli applies the export.

```{admonition} Maturity: preview
:class: note

`piceli publish` and `render --out` are **preview**: the artifact layout is
the one `flux push artifact` produces and a kind test has Flux reconcile a
published artifact, but options and JSON fields may still change in a minor
release. Signing (cosign) is not built in yet (see [Signing](#gitops-signing)).
The controller is experimental (see its section).
```

(gitops-controller)=
## Piceli's own GitOps controller

```{admonition} Maturity: experimental
:class: warning

`piceli gitops` and `piceli promote` are new in 0.13.0; options, the status
document and the RBAC may change in a minor release.
```

A cluster runs Piceli itself as the controller: it polls a Git repository and
keeps **one environment per branch** at the branch's head, reusing
`piceli deploy`'s plans, approvals, builds and checks. No webhook, no laptop
step.

```sh
piceli gitops enable deploy/app.py:pipeline \
  --repo https://git.example.com/org/shop.git --branches 'main,wp-*' \
  --poll 60s --credentials-secret shop-git \
  --image registry.example.com/piceli@sha256:<digest> \
  --builder-image registry.example.com/piceli-builder@sha256:<digest> \
  --kubeconfig cluster.kubeconfig --context my-cluster
# prints the install plan and its hash, exit 3; then the same command with
#   --approve sha256:<plan hash>
```

What the controller does on every poll (`--poll`, default 60s):

| Event | What happens |
| --- | --- |
| A branch matching `--branches` (not main) has a new head | Build it in the cluster (a Job on the builder node, cache per branch) or use the images pushed for that exact commit with `piceli env push`, then deploy it to the branch's environment (its own namespace). Every push redeploys. |
| An untagged push to main | Nothing. |
| A new tag matching `--tags` (default `v*`) | Main is planned at the tag's commit. Tags that exist when the controller starts are the baseline and deploy nothing. |
| `piceli promote BRANCH@SHA` | Main is planned at that commit (only a commit the controller saw on that branch). |
| A branch is deleted (or stops matching) | Its environment is torn down (namespace and claims); main's environment never is. |

Approval stays the owner's:

- A **branch** deploys by itself only when the pipeline declares an
  `auto_approve=ApprovalPolicy(...)` and the plan is inside it; otherwise the
  branch waits in `approval-required` with the plan hash.
- **Main** always waits for a hash approval, unless the owner enabled the
  controller with `--main-auto-approve` (then main too applies plans inside
  the policy).
- `piceli gitops approve BRANCH sha256:<hash>` releases the waiting plan; the
  controller applies it on its next poll only if the plan is still the same
  (a new push or a changed plan makes the approval stale:
  `gitops-approval-stale`). When the approved plan changes under the
  controller (an apply that stopped half way), it plans again: the policy
  applies the new plan when it covers it, otherwise the branch waits in
  `approval-required` with the new hash.

The controller works **one step at a time**: teardowns first, then deploys,
oldest push first. A failed step is retried with exponential backoff (30s,
60s, … capped at 1h) and, after five attempts, left `failed` until the next
push; a failing branch never blocks the others, and a teardown is retried
until it succeeds. `piceli gitops status [--json]` shows everything:

```sh
piceli gitops status --kubeconfig cluster.kubeconfig --context my-cluster
# gitops controller: healthy (last poll 2026-09-30T10:00:00Z)
#   repo https://git.example.com/org/shop.git branches main,wp-*
#   main: approval-required 3f2a91c07d4e; approve: piceli gitops approve main sha256:…
#   wp-login: deployed 8c1d2e3f4a5b in shop-wp-login
```

`piceli gitops disable` plans (exit 3) and, with `--approve`, removes the
controller. It never deletes an environment or the namespace, and keeps the
state claim unless `--delete-state`.

### The controller image

The controller runs the Piceli CLI (`piceli gitops run`) and `git`; its build
Jobs run a builder image with Piceli and the host-build tools. Every release
publishes both, for linux/amd64 and linux/arm64, built from the released
wheel; the [release notes](https://github.com/pynenc/piceli/releases) list
their digests:

- `ghcr.io/pynenc/piceli-controller@sha256:…` for `--image` (Piceli with the
  `ui` extra, `git`, `openssh-client`; the UI installed in the cluster runs
  it too);
- `ghcr.io/pynenc/piceli-builder@sha256:…` for `--builder-image` (the
  controller plus `cargo` with the linux and wasm32 targets,
  `cargo-zigbuild`, `zig`, `uv`, and `gcc`, `make`, `cmake`, `pkg-config`,
  `curl`, `xz` for crates with C builds such as `jemalloc-sys` and
  `aws-lc-sys`).

Both packages are public: nodes pull them anonymously. A release checks that
an anonymous pull works before it finishes.

`--image` must be pinned by digest; the controller never pulls a moving tag,
and the image's Piceli version should match your CLI's. To build them
yourself (another registry, more tools), use `images/Dockerfile` with the
wheel of your version:

```console
$ pip download --no-deps --dest ctx/dist "piceli==0.14.2"
$ docker buildx build -f images/Dockerfile --target controller \
    --platform linux/amd64,linux/arm64 -t REGISTRY/piceli-controller:0.14.2 --push ctx
$ docker buildx build -f images/Dockerfile --target builder \
    --platform linux/amd64,linux/arm64 -t REGISTRY/piceli-builder:0.14.2 --push ctx
```

The push prints each digest. Add whatever your pipeline module imports
besides Piceli with a `FROM …piceli-controller@sha256:…` image of your own.
Without `--builder-image`, a branch deploys only images pushed with `piceli
env push`.

### What gets installed

| Object | Purpose |
| --- | --- |
| Namespace `piceli-system` (`--namespace`) | The controller's home (never deleted by `disable`). |
| ServiceAccount, Role and RoleBinding `piceli-gitops` | In its namespace only: ConfigMaps (status, requests), Leases, build Jobs, their Pods and logs, cache claims, Events. |
| ClusterRole and ClusterRoleBinding `piceli-gitops` | Read nodes; create, read and delete namespaces (one per branch); read and delete PersistentVolumes (a branch teardown removes the volumes bound to its claims); create RoleBindings that bind **only** `piceli-gitops-deployer` (the `bind` verb is limited to that name). No Secrets, nothing else cluster-wide. `--cluster-rbac` adds ClusterRoles/ClusterRoleBindings for apps that declare them; `piceli cluster init` keeps that rule. |
| ClusterRole `piceli-gitops-deployer` | The namespaced kinds an app deploys, the `scale` subresource of Deployments and StatefulSets (restore points) and reads (`get`, `list`, `watch`) of `metrics.k8s.io` pods and nodes, so a release can grant them to its own Role without an escalation refusal; used only through a RoleBinding in each environment's namespace, so the controller can change nothing outside them. |
| PersistentVolumeClaim `piceli-gitops-state` (`--storage`, `--storage-class`) | The Git mirror, the last-seen commits and tags, build receipts. |
| ConfigMap `piceli-gitops-config` | The settings (pipeline, repository, globs, poll, build options); never a credential. |
| Deployment `piceli-gitops` | One replica, `Recreate`, non-root, read-only root filesystem, no privilege escalation; the credentials Secret mounted read-only. Its pod template carries `piceli.io/config-hash`, so `gitops enable` with changed settings (a new `--builder-image`) restarts the controller. |

**Git credentials** stay in a Secret you create in the controller's namespace
(`--credentials-secret`): `username` and `password` (a token) for HTTPS, or
`ssh-privatekey` and `known_hosts` for SSH. The controller hands them to Git
through a credential helper that reads the files, so they appear in no command
line, environment, log, status or error; a `--repo` URL with credentials in
it is refused (`gitops-repo-invalid`). The build Job reads its own Secret
(`--build-git-secret`, default `piceli-build-git`).

```{warning}
The controller imports the pipeline module of every branch it deploys:
whoever can push a branch matching `--branches` runs code with the
controller's rights. Keep the globs narrow and protect those branches as you
would protect a CI runner's secrets.
```

### Named environments and idle stop

When the pipeline declares `EnvConfig(environments=[...])` (see
{doc}`environments`), `gitops enable` reads them from the working tree
(`--root`, default `.`) into the controller's config, so the install plan
hash covers every environment's trigger; change them and run `gitops enable`
again. A pipeline file that is there and does not load is refused
(`gitops-pipeline-invalid`); without the file the controller runs as in
0.13. Per environment:

- `Branch("main")`: every push to `main` deploys it (no tag needed);
- `Tag("v*-rc*")`: the latest new matching tag deploys it;
- `Promote()`: `piceli promote rc main@<sha>` deploys that commit (an
  environment without `Promote()` drops the request with
  `gitops-promote-not-allowed`; `piceli promote BRANCH@SHA` still targets
  main).

Each waits for `piceli gitops approve NAME HASH` unless it declares
`auto_approve=True` and the plan is inside the pipeline's `auto_approve`
policy. A commit already built for one environment is not built again for
another. `EnvConfig(idle_stop="24h")` scales a branch environment without a
push for that long to zero (state `stopped`, reason `idle-stop`); the next
push starts it again. The status lists the rules under
`controller.environments` and `controller.idle_stop_seconds`.

### Compositions: many sources

`piceli gitops enable infra.py` (a module, no `:ATTR`) installs the same
controller for a composition (see {doc}`components`): it polls every
`Source` of the module with one Git Secret, resolves each environment's
`follow={source: rule}` to one commit per source, builds only the components
whose source digest changed (one build Job that fetches every source it
needs) and rolls only those; unchanged components apply as no-op.
`--branches` and `--tags` are not used: the module names its sources and
rules. When an environment deploys a pipeline (`Environment(pipeline=…)`,
see {doc}`compositions`), the controller also follows the composition's own
repository (`--repo`, default the origin remote of `--root`; branch
`--main-branch`) and imports the module at its commit; each output image is
rebuilt only when its change key changed. The status adds `sources`,
`envs.<env>.revision` and `envs.<env>.components` (see {doc}`components`).

`piceli gitops sync [ENV] [--component NAME]` asks the controller to deploy
an environment (or every one) now at its revision; with `--component` a
composition controller also rebuilds that component. The deploy still needs
the environment's usual approval.

### Status and requests (for tools)

The controller publishes its status in the ConfigMap `piceli-gitops-status`
(key `status.json`, schema `piceli.gitops-status.v1`) of its namespace;
`piceli gitops status --json` prints it with `health` (`healthy`,
`degraded`, `stale`, `starting`, `down`) and `deployment_ready`, and
`piceli envs` reads it (`piceli.gitops.state.read_status`). Per branch,
`envs.<branch>` holds `commit` (wanted), `deployed_commit`, `state`
(`pending`, `retrying`, `approval-required`, `deployed`, `failed`,
`deleting`,
`stopped`), `plan_hash`, `reason` (an error code), `attempts`,
`next_attempt_at`, `pushed_at`, `updated_at`, `namespace` and `trigger`
(`push`, `tag v1.2.0`, `promote BRANCH@SHA`). A failed step adds `failure`:
`log_tail` (the scrubbed end of a failed build Job's log, at most 4000
characters) and `kept_job` (that Job, kept with its pod log until the next
build of the same branch), or `denied` (the `verb`, `resource`, `namespace`
and HTTP `status` of the request the Kubernetes API refused, as Piceli asked
for it; never the server's answer). Dropped requests are listed in
`rejected_requests` with their code.

`piceli gitops approve`, `piceli gitops sync` and `piceli promote` add one
key each to the ConfigMap `piceli-gitops-requests` (kinds `approve`,
`sync`, `promote`); the controller removes a request once it
handled it. Locally, `piceli gitops run --once --state-dir DIR` (with
`--kubeconfig`/`--context`) runs one poll, and `status`, `approve` and
`promote` take `--state-dir DIR` instead of a cluster.

## What an export does not cover

With `piceli deploy` or `piceli release`, Piceli computes a plan against the
live cluster, applies only the plan you approved, journals every write and
can resume or roll back. When another controller applies an exported
artifact, **none of that runs**: that controller applies whatever the
artifact holds, on its own schedule.

| | `piceli deploy` / `release` / Piceli's controller | An exported artifact applied by Flux or Argo CD |
| --- | --- | --- |
| Typed model, environments, `render`, `--diff-env` | yes | yes (you render before publishing) |
| Review before the change | the plan, field by field, against the live cluster | the rendered files (and the artifact digest you approve); no live diff from Piceli |
| Approval | the plan hash | the artifact digest, for the **push**; what the controller then applies is governed by its own configuration (sync policy, PR review) |
| Journal, resume, `release rollback` | yes | no; roll back by pointing the controller at a previous digest |
| Adoption and replacement rules, owner labels, pruning safety | yes | the controller's (Flux `prune`, Argo CD `prune`) |
| Generated and rotated secrets, private versions | yes | no: Secrets must come from outside the artifact (see [Secrets](#gitops-secrets)) |
| `[[checks]]` and automatic rollback | yes | no; use the controller's health checks |
| Builds and image delivery | yes | no; render from a spec whose images are pinned by digest |
| `piceli status`, `piceli access` | yes | `status` reads a Piceli release's state, so it does not see a controller's objects; `access` port forwards still work |

To let other people install the app on their own clusters with Helm or
plain YAML, see {doc}`helm_charts`.

Do not run both on the same objects: a Piceli release and another controller
would fight over the fields.

## Publish an OCI artifact

```sh
piceli publish app.py:app --env prod --to oci://registry.example/shop/app:prod
```

Without `--approve` nothing is sent. Piceli renders the target (same
`TARGET`, `--spec`, `--namespace` and `--env` as `piceli render`), turns every
object into a file, packages the files, and prints the artifact's digest
(exit `3`):

```text
{"state": "approval-required", "digest": "sha256:8b83…", "files": ["settings_configmap.core_shop_settings.yaml", "web_deployment.apps_shop_web.yaml", "web_service.core_shop_web.yaml"], "target": "oci://registry.example/shop/app:prod?tls=true", "namespace": "shop", "omitted_secrets": [], "config": {…}, "layer": {…}, "media_type": "application/vnd.oci.image.manifest.v1+json"}
```

Review the files (`piceli render … --out /tmp/review` writes the very same
bytes), then push that exact artifact:

```sh
piceli publish app.py:app --env prod --to oci://registry.example/shop/app:prod \
  --credentials /path/to/registry.json --approve sha256:8b83…
```

Piceli uploads the blobs the registry does not have yet, puts the manifest
**by digest**, then under the tag (when `--to` has one), reads both back and
prints `{"state": "published", "digest": …, "reference":
"registry.example/shop/app@sha256:8b83…", "url": "oci://registry.example/shop/app", "tag": "prod", …}`.

- **Deterministic.** The same render always has the same digest, so the
  approval binds the content: if the model, an option or an annotation
  changed, `--approve` is refused with `gitops-artifact-changed`. Publishing
  the same artifact again pushes nothing new and is safe to retry.
- **Credentials** come only from `--credentials FILE`, a private (`chmod 600`)
  JSON file with `{"username": …, "password": …}` or `{"token": …}`, the same
  as `piceli artifacts deliver`. They are never printed. `--ca-file` trusts a
  private CA. Plain HTTP is accepted only for a loopback registry.
- **Annotations.** `--source URL` and `--revision REV` set
  `org.opencontainers.image.source` and `org.opencontainers.image.revision`
  (Flux shows the revision, for example `main@sha1:<commit>`); the namespace
  and `--env` are recorded as `io.piceli.render.namespace` and
  `io.piceli.render.environment`. No creation time is recorded, so the
  digest stays reproducible.
- It never contacts a cluster.

### Layout

The artifact is exactly what `flux push artifact` produces:

| Part | Media type | Content |
| --- | --- | --- |
| Manifest | `application/vnd.oci.image.manifest.v1+json` | canonical JSON, one layer, the annotations above |
| Config | `application/vnd.cncf.flux.config.v1+json` | an empty image config naming the layer's uncompressed digest |
| Layer | `application/vnd.cncf.flux.content.v1.tar+gzip` | a tar of the files, sorted, with zero timestamps and owners, gzip without name or time |

Each object is one file, `<component>_<kind>.<group>_<namespace>_<name>.yaml`
(`core` for the core group, `cluster` for cluster-scoped objects), YAML with
sorted keys and a `# piceli component: …` comment. There is no
`kustomization.yaml`: Flux generates one, Argo CD reads the directory.
Components and their dependencies are not ordering hints for a controller;
Flux and Argo CD apply namespaces and CRDs first, then the rest.

## Consuming the artifact with Flux

Point an `OCIRepository` at the repository and a `Kustomization` at it. Pin
the digest to apply exactly what was approved, or follow a tag:

```yaml
apiVersion: source.toolkit.fluxcd.io/v1
kind: OCIRepository
metadata:
  name: shop
  namespace: flux-system
spec:
  interval: 5m
  url: oci://registry.example/shop/app
  ref:
    digest: sha256:8b83…        # or: tag: prod
  secretRef:
    name: registry-pull          # a docker-registry Secret, for a private registry
---
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: shop
  namespace: flux-system
spec:
  interval: 10m
  sourceRef:
    kind: OCIRepository
    name: shop
  path: ./
  prune: true
  wait: true
```

The namespace the objects name must exist (create it in another
Kustomization, or add a `Namespace` to the app with `app.resource`). An HTTP
registry needs `insecure: true` on the `OCIRepository`; use it only for a
test registry. When the artifact follows a tag, Flux reports the applied
revision as `prod@sha256:<digest>`, the digest `piceli publish` printed.

## Consuming the export with Argo CD

**From OCI** (Argo CD 3.1 and later). Argo CD's repo server accepts only a
few layer media types by default; add Flux's content type to
`ARGOCD_REPO_SERVER_OCI_LAYER_MEDIA_TYPES` on the `argocd-repo-server`
Deployment (keep the defaults in the list):

```text
ARGOCD_REPO_SERVER_OCI_LAYER_MEDIA_TYPES=application/vnd.oci.image.layer.v1.tar,application/vnd.oci.image.layer.v1.tar+gzip,application/vnd.cncf.helm.chart.content.v1.tar+gzip,application/vnd.cncf.flux.content.v1.tar+gzip
```

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: shop
  namespace: argocd
spec:
  project: default
  source:
    repoURL: oci://registry.example/shop/app
    targetRevision: sha256:8b83…   # or the tag, e.g. prod
    path: .
  destination:
    server: https://kubernetes.default.svc
    namespace: shop
  syncPolicy:
    automated:
      prune: true
```

**From Git.** Render into a directory of your GitOps repository and commit it:

```sh
piceli render app.py:app --env prod --out deploy/shop/prod
git -C deploy add shop/prod && git -C deploy commit -m "shop: render prod"
```

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: shop
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://git.example/platform/deploy.git
    targetRevision: main
    path: shop/prod
  destination:
    server: https://kubernetes.default.svc
    namespace: shop
```

`--out DIR` writes the same files as the artifact's layer and a
`.piceli-render` marker listing them, and prints one `{"state": "written",
"files": […], "removed": […], "omitted_secrets": […]}` object. The directory
must be absent, empty, or one a previous `--out` wrote: then only the files
that render listed are replaced or removed, files of your own (a README, a
`kustomization.yaml`) stay, and a new file never overwrites one Piceli did not
write (`render-out-refused`). Flux can consume the same directory with a
`GitRepository` source.

(gitops-secrets)=
## Secrets

A controller applies the files as they are, so they must never carry secret
values, and Piceli's secret store (generated, templated and rotated values,
private versions) does not reach the controller. The rules:

- **A `Secret` object is refused** (`gitops-secrets-present`) unless you pass
  `--secrets external`. Then every Secret is left out of the files and listed
  in `omitted_secrets` (`namespace/name`); the workloads still reference them
  by name, so they must exist before the controller applies. Provide them with:
  - **SOPS:** commit SOPS-encrypted Secret manifests to Git and let a Flux
    `Kustomization` with `decryption.provider: sops` apply them (Argo CD needs
    a SOPS plugin). Piceli's own `Sops` source decrypts at plan time for
    `piceli deploy`; that does not apply here.
  - **External Secrets Operator:** declare an `ExternalSecret` in the app. Its
    `secretKey` and `secretStoreRef` fields look sensitive, so declare them
    public:

    ```python
    app.resource(
        "external-secrets.io/v1",
        "ExternalSecret",
        "db",
        {
            "secretStoreRef": {"name": "vault", "kind": "ClusterSecretStore"},
            "target": {"name": "db"},
            "data": [
                {
                    "secretKey": "password",
                    "remoteRef": {"key": "shop/db", "property": "password"},
                }
            ],
        },
        public=["spec.secretStoreRef", "spec.data.*.secretKey"],
    )
    ```

  - or created by hand or by another tool.
- **A redacted value is refused** (`gitops-secret-value`): an env var or a
  field named like a password, token or credential that holds a literal, or a
  value Piceli injects at apply time. Move it into a Secret, or declare a field
  that is not secret with `public=` (the `piceli.io/public-fields`
  annotation).
- **A placeholder image is refused** (`gitops-image-unresolved`): a
  `Pipeline`'s build images are placeholders until `piceli deploy` builds
  them. Render from a `release.toml` whose `[images]` or receipts pin every
  image by digest.

(gitops-signing)=
## Signing

`piceli publish` does not sign. Until it does, sign the published digest with
cosign and have Flux verify it:

```sh
cosign sign registry.example/shop/app@sha256:8b83…
```

```yaml
spec:
  verify:
    provider: cosign
    secretRef:
      name: cosign-public-key
```

Always sign the digest, never the tag.

## Errors

| Code | Meaning |
| --- | --- |
| `gitops-secrets-present` | The render holds a Secret; pass `--secrets external` and provide it outside the files. |
| `gitops-secret-value` | A non-Secret object holds a redacted or injected value. |
| `gitops-image-unresolved` | A placeholder image; pin the image by digest. |
| `gitops-empty` | Nothing left to hand off. |
| `gitops-target-invalid` | `--to` is not `oci://host[:port]/repository[:tag]`, or an annotation value is invalid. |
| `gitops-artifact-changed` | `--approve` is not this render's digest; publish without it and approve again. |
| `gitops-push-failed` and the registry codes (`registry-unauthorized`, `registry-unreachable`, …) | The push failed (exit `1`); safe to retry. |
| `render-out-refused` | `--out` names a directory Piceli did not write, or would overwrite a foreign file. |
| `gitops-repo-invalid`, `gitops-image-unpinned`, `gitops-config-invalid` | `gitops enable` refused its options: credentials in the URL, an image not pinned by digest, a bad glob or size. |
| `gitops-plan-changed` | `gitops enable --approve` or `gitops disable --approve` names a hash that is not the current plan's. |
| `gitops-target-required`, `gitops-not-installed`, `gitops-cluster-failed` | No cluster named, no controller in the namespace, or the API refused a request. |
| `gitops-approval-stale`, `gitops-promote-unknown`, `gitops-request-invalid` | A request the controller dropped (listed in `rejected_requests` of the status). |
| `gitops-git-failed`, `gitops-pipeline-invalid`, `gitops-step-failed`, `gitops-port-unavailable` | A poll or a branch step failed; recorded in the status and retried. |
| `gitops-state-invalid`, `gitops-controller-locked` | The controller's state is unreadable, or another controller holds it. |

`piceli explain <code>` prints the cause and the fix; {doc}`reference/errors`
lists every code.
