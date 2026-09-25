# Hand off to Flux or Argo CD

This page shows how to keep modelling an app in typed Python with Piceli while
a GitOps controller (Flux or Argo CD) applies it: `piceli publish` pushes the
rendered manifests as an OCI artifact, and `piceli render --out DIR` writes
them as files to commit to Git. It also says exactly what Piceli stops doing
for you once a controller applies.

```{admonition} Maturity: preview
:class: note

`piceli publish` and `render --out` are **preview**: the artifact layout is
the one `flux push artifact` produces and a kind test has Flux reconcile a
published artifact, but options and JSON fields may still change in a minor
release. Signing (cosign) is not built in yet (see [Signing](#gitops-signing)).
```

## The handoff boundary

With `piceli deploy` or `piceli release`, Piceli computes a plan against the
live cluster, applies only the plan you approved, journals every write and
can resume or roll back. When a controller applies the manifests, **none of
that runs**: the controller applies whatever the artifact holds, on its own
schedule. Choose deliberately.

| | `piceli deploy` / `release` | Flux or Argo CD from a Piceli artifact |
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

Do not run both on the same objects: a Piceli release and a controller would
fight over the fields.

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

## Flux

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

## Argo CD

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

`piceli explain <code>` prints the cause and the fix; {doc}`reference/errors`
lists every code.
