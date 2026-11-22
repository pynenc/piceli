# Publish images for other people's clusters

When someone else runs your app on their own cluster (a client on GKE, EKS
or AKS), your images must run on their nodes, whatever their CPU, and come
from a registry they can pull from. `piceli artifacts publish` takes a build
and pushes every image as a **multi-platform image**: one manifest per
platform joined in an OCI image index, under a version tag, pinned by
digest, with its SBOM and provenance attached and, optionally, signed. The
result is the `images:` values of the Helm chart that `piceli chart` makes
from the same App (see {doc}`helm_charts`).

```{admonition} Maturity: experimental
:class: note

Multi-platform builds, `piceli artifacts publish`, attached attestations and
cosign signing are new in 0.10.0 and may change in a minor release, with a
changelog entry. See the {doc}`roadmap` for every feature's status.
```

```sh
# 1. Build every platform from one spec (plan, then run the approved plan).
piceli artifacts build-spec preview --spec host-build.toml \
    --platform linux/amd64 --platform linux/arm64
piceli artifacts build-spec run --spec host-build.toml \
    --platform linux/amd64 --platform linux/arm64 \
    --approve-plan sha256:<plan_hash> --out dist/receipt.json

# 2. Publish: without --approve it prints the plan and its digest (exit 3).
piceli artifacts publish --receipt dist/receipt.json \
    --to oci://registry.example/shop --tag 1.4.0 \
    --docker-config ~/.docker/config.json --sign-key cosign.key
piceli artifacts publish --receipt dist/receipt.json \
    --to oci://registry.example/shop --tag 1.4.0 \
    --docker-config ~/.docker/config.json --sign-key cosign.key \
    --approve sha256:<digest> --out dist/publish.json --values-out dist/images.yaml

# 3. The client installs your chart with those images.
helm install shop oci://registry.example/charts/shop --version 1.4.0 \
    --values dist/images.yaml --values client-values.yaml
```

## Build for several platforms from one spec

**Host builds** (no container VM, see {doc}`host_builds`): declare the
platforms in the `host-build.toml`, or pass `--platform` once per platform:

```toml
[build]
tools = ["cargo", "cargo-zigbuild", "zig"]
platforms = ["linux/amd64", "linux/arm64"]
commands = [
    ["cargo", "zigbuild", "--release", "--locked",
     "--target", "{rust_arch}-unknown-linux-gnu.2.36"],
]

[[output.image]]
name = "api"
repository = "shop/api"
target_files = { "{rust_arch}-unknown-linux-gnu/release/api" = "/usr/local/bin/api" }
```

The build runs once per platform, one after another, with the facts of the
platform alone: `{platform}`, `{arch}` and `{rust_arch}` are the platform's,
and the page size is the architecture default (4 KiB, so `{page_size_log2}`
is `12`), because the build cannot know your clients' kernels. Every
platform shares the cache and its target directory (`--cache-dir`, default
`<XDG cache>/piceli/host-build`); `cargo zigbuild --target` keeps each
platform's output apart, so the second platform recompiles only what
differs. Each platform gets its own layers, SBOM and provenance under
`<output-dir>/<platform-slug>/`, and each image one canonical index
(`indexes/<image>.index.json`, manifests sorted by platform): the same
inputs give the same index digest. One approval (`--approve-plan`) covers
every platform; the plan hash covers each platform's plan. With one
platform the plan hash and the receipt are those of a single-platform
build. Steps whose result is the same for every platform (a `wasm32`
browser bundle) belong in `build.shared_commands`: they run once, with the
first platform, into a target directory every platform's images copy from
(see {doc}`host_builds`). A `build.platforms` list also restricts which node a
`Build.spec(..., builder="host")` deploys to (`node-platform-mismatch`).

**Docker builds** ({doc}`containerized_builds`): list the platforms in the
`build.toml` (`platforms = ["linux/amd64", "linux/arm64"]`). Every platform
is built and loaded into the local engine (`repository:tag-amd64`,
`repository:tag-arm64`); a platform other than the engine's runs under
emulation (QEMU), which is slow for compilers: prefer a host build with
`cargo zigbuild` for Rust, or a `buildx_builder` on a machine of that
architecture. The index is assembled when the images are published.

## What `piceli artifacts publish` does

Each image goes to `<registry>/<prefix>/<key>`, where `key` is the last path
segment of the image's repository in the build spec (`shop/api` -> `api`),
the name `piceli chart` gives its values.

1. **Plan, no network.** The images, each platform's config digest (and
   manifest digest for a host build), the tag, the attestations and their
   sha256, and the signing key's and cosign's sha256. Without `--approve`
   it prints them and their `digest` (exit `3`). `--approve` must be that
   digest (`publish-not-approved`).
2. **Each platform's manifest by digest.** The config digest recomputed
   from the archive (host build) or from `docker image save` (Docker build)
   must equal the receipt's before anything is sent. Only blobs the
   registry lacks are uploaded; publishing the same build twice sends
   nothing.
3. **The image index**, by digest. When the version tag already names
   another image, publishing is refused (`publish-tag-exists`) unless
   `--move-tag`; for a host build this is known before anything is pushed.
4. **The SBOM and the provenance** of each platform, as OCI 1.1 referrers of
   that platform's manifest (an artifact manifest with `subject` and
   `artifactType` `application/spdx+json` or `application/vnd.in-toto+json`).
   A registry without the referrers API gets the fallback tag
   `sha256-<manifest digest>` that the OCI distribution spec defines, which
   OCI 1.1 clients such as `oras discover` read. `--no-attestations` skips
   them.
5. **Signatures** (`--sign-key`, below): the index, every platform manifest
   and every attestation, before the tag.
6. **The tag, last**, so a client never pulls a version whose signature or
   SBOM is not there yet. Then everything is read back: the tag names the
   index, the index bytes and every child are served, every attestation is
   listed as a referrer (`publish-verification-failed` otherwise).

The receipt (`--out`, `piceli.publish-receipt.v1`) gives per image
`repository` (with the registry), `tag`, `digest` (the index), each
platform's manifest digest and size, the blobs uploaded and skipped (count
and bytes), the attestations (`kind`, `digest`, `subject`, `fallback_tag`),
the signatures and `previous` (the digest a moved tag named). Its `values`
key, and the file `--values-out` writes, is the Helm values fragment:

```yaml
images:
  api:
    repository: registry.example/shop/api
    tag: 1.4.0
    digest: sha256:…   # the index: each node pulls its own platform
```

`piceli chart manifests app.py:app --values dist/images.yaml` and
`helm install … --values dist/images.yaml` render
`registry.example/shop/api:1.4.0@sha256:…`.

(artifacts-copy)=
## Promote a built image by digest: `piceli artifacts copy` (0.74.0)

When a GitOps pipeline already built and tested an image, a release need
not build it again: copy that digest to the release registry. `piceli
artifacts copy` copies the index, every platform's manifest and blob, and
its referrers (SBOM, provenance and signatures, also a signature of an
SBOM), keeping every digest. `skopeo copy --all --preserve-digests` keeps
the image but not the referrers.

```sh
# The digest the environment runs (a pipeline spec that declares
# build.platforms records an OCI index; see compositions).
piceli gitops status --json | jq -r '.envs.main.components.api.digest'

# Reach the in-cluster registry (plain HTTP is allowed on loopback).
kubectl -n piceli-system port-forward svc/piceli-registry 5000:5000 &

# Without --approve it prints the plan and its digest (exit 3).
piceli artifacts copy \
    --from oci://127.0.0.1:5000/shop/api@sha256:<index digest> \
    --to oci://ghcr.io/example/api:1.4.0 --docker-config ~/.docker/config.json
piceli artifacts copy \
    --from oci://127.0.0.1:5000/shop/api@sha256:<index digest> \
    --to oci://ghcr.io/example/api:1.4.0 --docker-config ~/.docker/config.json \
    --approve sha256:<plan digest> --out dist/copy.json
```

1. **Plan** (no network): the source repository and digest, the target
   repository and tag, `--no-referrers` and `--move-tag`. Its digest is
   what `--approve` names.
2. **The image**: the index (or manifest) by digest, each platform's
   manifest, and only the blobs the target lacks.
3. **The referrers** of the index, of each platform manifest and of each
   referrer, read from the referrers API or the `sha256-<digest>` fallback
   tag and written the same way.
4. **The tag**, last. A tag that names another digest is refused
   (`copy-tag-exists`) before anything is pushed, unless `--move-tag`.
5. **Read back**: the tag names the digest, each platform manifest is
   there and each referrer is listed (`copy-verification-failed`
   otherwise).

`--to` without a tag copies by digest only. The source is read anonymously
unless `--from-credentials` or `--from-docker-config`; the target uses
`--credentials` or `--docker-config` (below); `--from-ca-file` and
`--ca-file` add a CA. The receipt (`piceli.copy-receipt.v1`) gives the
`digest`, its `media_type`, `platforms` (each platform's manifest digest),
the blobs uploaded and skipped, each referrer (`subject`, `digest`,
`artifact_type`, `result`) and `previous` (the digest a moved tag named).
A second run copies nothing (`result: already-present`).

## Registries and credentials

- A public registry needs no credentials to pull; pushing always does.
- `--docker-config PATH` reads a Docker `config.json`: a `credHelpers` entry
  for the registry (or `credsStore`) asks that helper
  (`docker-credential-<name> get`, for example `gcloud` for Artifact
  Registry or `ecr-login` for ECR); otherwise `auths.<registry>.auth`.
  `--credentials FILE` reads a private JSON file
  (`{"username", "password"}` or `{"token"}`), as `artifacts deliver` does.
  Credentials are never printed, never part of the plan or the receipt, and
  never passed to cosign as arguments.
- TLS is required except for a loopback registry; `--ca-file` adds a CA.
- Your clients pull with their own credentials: the chart's
  `imagePullSecrets` value names the Secret they create (see
  {doc}`helm_charts`).

## Signing and verifying

`--sign-key cosign.key` signs with [cosign](https://docs.sigstore.dev/) 3
(on `PATH` or `--cosign PATH`, pinned by sha256) and a key pair you keep:

```sh
cosign generate-key-pair            # cosign.key (0600) and cosign.pub
export COSIGN_PASSWORD=…            # the key's password, read by cosign only
```

Signatures are stored next to the image, in the Sigstore bundle format, as
referrers of the signed manifest. **Nothing is sent to the public
transparency log**: Piceli gives cosign a signing config without Rekor or a
timestamp authority, so verification uses the public key alone. Give your
clients `cosign.pub`; they verify with:

```sh
cosign verify --key cosign.pub --insecure-ignore-tlog=true \
    registry.example/shop/api@sha256:<index digest>
```

(`--insecure-ignore-tlog` only says that there is no log entry to check; the
signature itself is verified.) An admission policy (Sigstore
policy-controller, Kyverno `verifyImages`) can require the same key.
Keyless signing (an OIDC identity and the public log) is not built in: run
`cosign sign --recursive registry.example/shop/api@sha256:<index digest>`
yourself after publishing if you want it.

## Errors

| Code | Meaning |
| --- | --- |
| `build-platforms-invalid` | No platform, a repeated or unsupported one, or one the spec does not allow |
| `publish-invalid` | Bad `--to`/`--tag`, unknown `--image`, two images with the same last segment |
| `publish-not-approved` | `--approve` is not this plan's digest |
| `publish-tag-exists` | The version tag names another image (use a new version or `--move-tag`) |
| `publish-input-changed` | An SBOM or provenance file differs from the build receipt |
| `publish-verification-failed` | What the registry serves differs from what was pushed |
| `copy-invalid`, `copy-not-approved` | Bad `--from`/`--to` (a digest is required), or `--approve` is not this plan's digest |
| `copy-tag-exists` | The `--to` tag names another digest (use a new tag or `--move-tag`) |
| `copy-verification-failed`, `copy-failed` | The target does not serve what was copied, or the copy stopped |
| `invalid-docker-config`, `credential-helper-failed` | Docker config credentials could not be read |
| `sign-key-invalid`, `sign-failed`, `cosign-tool-required` | Signing could not run |

`piceli explain <code>` prints the cause and the fix of each.
