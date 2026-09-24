# Containerized builds

```{admonition} Maturity: preview
:class: note

The `piceli.build-spec.v1` format and the receipt may still change in a minor release, with a changelog entry. See the {doc}`roadmap` for every feature's status.
```

A `build.toml` describes a build that runs inside a pinned builder image with
`docker buildx`. Piceli stages the declared inputs, runs the build, extracts
the declared outputs and writes a **build receipt**. The receipt ties the
output digests to the builder digest and to the git identity of every source
(see {doc}`source_identity`).

```sh
piceli artifacts build-spec preview --spec build.toml
piceli artifacts build-spec run --spec build.toml \
    --approve-builder sha256:<builder digest> --out dist/receipt.json
```

A complete example that builds a small Rust HTTP service for `linux/arm64`
is in `examples/builds/rust-hello/`.

```{warning}
Source drift is currently checked over the **whole repository** of each
declared source, not only the files the build context selects (unless the
source sets a `subpath`). Any uncommitted change anywhere in the repository,
even one the build never reads, makes the source dirty (refused unless
`allow_dirty = true`) and changes its diff digest, so verification against
`--lock` fails with `source-drift`, possibly after a long build. Commit or stash
unrelated changes before a build, or narrow the identity with `subpath` in
`inputs.toml` (see {doc}`source_identity`). Checking drift over the build
context only is planned.
```

## The spec

```toml
revision = "piceli.build-spec.v1"
name = "rust-hello"
inputs = "inputs.toml"            # optional: sources to identify (WP1b.1)

[builder]                          # the image the build runs in, by digest
image = "docker.io/library/rust:1.97.0-bookworm"
digest = "sha256:8fa55b2f..."

[build]
platforms = ["linux/arm64"]        # one or more linux/<arch>
workdir = "/work"
command = ["cargo", "build", "--release", "--locked", "--offline"]
# commands = [["cargo", "fetch"], ["cargo", "build", "--release"]]
network = "none"                   # "none" (default) or "default"
source_date_epoch = 0              # default 0
timeout_seconds = 1800             # per docker invocation, at most 3600
env = { CARGO_TERM_COLOR = "never" }
# buildx_builder = "default"       # default: the builder of the current docker context

[context.app]                      # a named build context
source = "piceli"                  # optional: a source from inputs.toml
path = "examples/builds/rust-hello"  # relative to the source (or to this file)
include = ["Cargo.toml", "Cargo.lock", "src/**/*.rs"]
exclude = []                       # optional
target = "/work"                   # where it lands in the builder (default: workdir)
max_files = 64                     # default 10000
max_bytes = 1048576                # default 64 MiB

[cache.cargo-registry]             # a BuildKit cache mount, never a context
target = "/usr/local/cargo/registry"
id = "cargo-registry"              # optional: share across specs
sharing = "shared"                 # locked (default), shared or private

[cache.target]
target = "/work/target"            # default id: piceli-<name>-<cache>-<platform>

[[output.file]]                    # a file extracted from the build
from = "target/release/rust-hello" # relative to workdir, or absolute
to = "bin/rust-hello"

[[output.image]]                   # an image loaded into the local engine
name = "rust-hello"
repository = "piceli-examples/rust-hello"
tag = "dev"
base = { image = "docker.io/library/debian:bookworm-slim", digest = "sha256:8820..." }
files = { "bin/rust-hello" = "/usr/local/bin/rust-hello" }
user = "65532:65532"
entrypoint = ["/usr/local/bin/rust-hello"]
# cmd = [...], workdir = "/..."; base may be "scratch"
```

Unknown fields are errors. Every image reference must carry a digest.

### What Piceli generates

With `command`/`commands`, Piceli writes the whole Dockerfile:

1. `FROM <builder>@<digest> AS piceli-build`, then `COPY --from=<context>` for
   each context.
2. One `RUN` with `--network=<network>` and every cache mount. It runs the
   commands and then copies each declared output out of the tree. This
   happens in the same step because a cache-mounted path such as `target/`
   is only visible while its mount exists. The step runs
   `/bin/sh -euc <script>`, and every argument is quoted with `shlex`. The
   builder image must therefore provide `/bin/sh`, `cp` and `touch`. The host
   never runs a shell.
3. A `scratch` stage `piceli-files` with only the declared files. It is
   exported with `--output type=local`.
4. One stage per `output.image`: the pinned base plus the declared files at
   their final paths.

The preview shows the rendered Dockerfile and its digest.

### Bringing your own Dockerfile

```toml
[build]
platforms = ["linux/arm64"]
dockerfile = "Containerfile"       # a file inside the `context` context
context = "app"                    # the main (positional) context
builder_arg = "RUST_IMAGE"         # receives builder.image@builder.digest
args = { PROFILE = "release" }     # plain build args

[images.PYTHON_IMAGE]              # extra pinned images passed as build args
image = "docker.io/library/python:3.12-slim"
digest = "sha256:..."

[[output.file]]
stage = "build"
from = "/src/target/release/app"
to = "bin/app"

[[output.image]]
name = "app"
repository = "example/app"
tag = "dev"
target = "runtime"
```

The Dockerfile must build `FROM ${RUST_IMAGE}`. Every `FROM`,
`COPY --from` and `RUN --mount=...,from=` must refer to one of these:

- a stage
- a declared context
- `scratch`
- an `image@sha256:` literal
- a pinned build arg

Piceli rejects `# syntax=` directives, because they fetch a frontend image,
and remote `ADD`. Declare caches with `RUN --mount=type=cache` in the
Dockerfile itself. The pin check is a guardrail against drift, not a sandbox.

## Contexts are minimal by construction

BuildKit applies `.dockerignore` only to the main context. A named context
(`--build-context name=dir`) sends the whole directory, including every
virtualenv and `target/` tree below it. Piceli never passes one of your
directories to BuildKit. For each context, it does the following:

- It walks the declared directory and selects only files that match
  `include` and not `exclude`. `*` and `?` match within one path component,
  and `**` matches across components. Patterns select files; use `dir/**`
  to select a whole subtree.
- It never enters `.git`, `target`, `.venv`, `venv`, `node_modules`,
  `__pycache__`, `.cargo`, `.rustup` or the tool caches, unless a pattern
  names that directory literally (for example `target/release/app`). A
  `**` pattern does not count as naming it.
- It never stages private paths: `.git`, `.ssh`, `.env*`, `*.key`, `*.pem`
  and `secrets`. It never follows symbolic links, and a link that matches a
  pattern is an error.
- It enforces `max_files` and `max_bytes` while scanning.
- It copies exactly those files into a private temporary directory. Each
  file is re-hashed as it is copied, and a file that changed since the plan
  fails the build with `context-changed`. Staged files get mode 0644 or 0755
  and the `source_date_epoch` modification time.

The preview and the receipt report each context's file count, bytes, digest,
pruned directories and skipped private files and links. The `rust-hello`
context is 3 files and 2,519 bytes.

**Dependency caches** such as the Cargo registry, Cargo git checkouts or a
`target/` directory are BuildKit cache mounts. They persist in the builder
between builds and are never uploaded. Do not ship `~/.cargo` as a context.
An offline build (`network = "none"`) needs its dependencies already in the
cache. Either run once with `network = "default"` and `--allow-network`, or
vendor the dependencies into the context.

## Running

Python API:

```python
import time
from pathlib import Path
from piceli.artifacts.build_spec import BuildGrant, BuildSpec

spec = BuildSpec.from_toml(Path("build.toml"))
plan = spec.plan()  # reads files, writes nothing, runs nothing
print(plan.preview())
receipt = spec.run(
    BuildGrant(spec.builder.digest, time.time() + 3600, plan_hash=plan.plan_hash),
    Path("dist"),
)
Path("dist/receipt.json").write_text(receipt.to_json())
```

`run` refuses to start unless the grant covers the following:

- **The exact builder digest.** Changing the builder needs a new approval.
- **The exact plan hash, if one is given.** The hash covers the spec,
  context digests, rendered Dockerfiles and argv.
- **Network, if the spec needs it** (`allow_network`).
- **An expiry that has not passed.**

During a run, Piceli does the following:

1. It uses `pinned_sources` from {doc}`source_identity` to record the inputs,
   or to verify them against `--lock`. A dirty source without `allow_dirty`
   is rejected. A source that changes during the build fails the run with
   `source-drift`.
2. It pins the `docker` binary by SHA-256 and re-verifies it around every
   call.
3. It runs `docker` with explicit argv, without a shell, with bounded time
   and output. The environment is minimal: `HOME`, `DOCKER_CONFIG`,
   `DOCKER_CONTEXT`, `DOCKER_HOST` and the TLS locators, plus a `PATH` made
   of the docker binary's own directory and the system default.
4. It builds each platform with `docker buildx build --platform <p>
   --provenance=false --sbom=false --network=<n> --build-arg
   SOURCE_DATE_EPOCH=<n>`. There is one invocation for the files and one per
   image (`--load --tag`). The receipt records which platforms were native
   and which were emulated.
5. It verifies that the extracted files are exactly the declared ones:
   regular files, within budget, none missing and none extra. It verifies
   that each loaded image has the expected platform and the config digest
   BuildKit reported. Files are published atomically into the output
   directory.

Build output never goes into the receipt. Pass `--log FILE` to keep it.

### CLI

| Command | Purpose |
| --- | --- |
| `build-spec preview --spec F [--inputs I]` | Print the plan as JSON: contexts, Dockerfiles, argv, `plan_hash`. |
| `build-spec run --spec F --approve-builder D --out R` | Build and write the receipt to `R`. Outputs go to `--output-dir`, which defaults to the directory of `R`. |

`run` also accepts the following options:

- `--inputs` and `--lock`: identify sources, or verify them against a lock.
- `--approve-plan`: require an exact plan hash.
- `--allow-network`: grant network access to the build.
- `--docker` together with `--docker-sha256`: use a pinned docker binary.
  Without them, Piceli uses `docker` from `PATH` and records its digest.
- `--log`: write the build output to a file.
- `--max-seconds`: the grant lifetime.

Exit codes: `0` success, `1` a build step failed or timed out (stderr lists
the steps, never their output), `2` rejected. On rejection, stderr is
`{"state": "rejected", "reason": "<code>"}` with a fixed code (each is
explained in {doc}`reference/errors` and by `piceli explain <code>`):

- spec: `invalid-spec`, `spec-unreadable`
- grant and tool: `invalid-grant`, `builder-not-approved`,
  `plan-not-approved`, `network-not-granted`, `grant-expired`,
  `invalid-tool`
- contexts: `context-missing`, `context-empty`, `context-symlink`,
  `context-budget-exceeded`, `context-changed`
- Dockerfile: `dockerfile-unpinned`
- sources: `source-drift`, `source-identity`, `invalid-inputs`
- engine: `docker-unavailable`
- outputs and images: `output-invalid`, `image-invalid`, `image-mismatch`

Paths are never echoed.

## Receipt schema (`piceli.build-receipt.v1`)

| Field | Meaning |
| --- | --- |
| `revision` | `"piceli.build-receipt.v1"` |
| `spec_sha256` | Digest of the normalized spec (not of the file's bytes). |
| `plan_hash` | Digest of the spec, context digests, Dockerfiles and argv. |
| `builder` | `{"image", "digest"}` as declared, plus `platform_manifests`: the per-platform manifest BuildKit resolved (when reported). |
| `base_images` | Other pinned images (`[images]`, `output.image.base`). |
| `platforms` | The built platforms, e.g. `["linux/arm64"]`. |
| `native_platform`, `emulated_platforms` | The engine's platform and the platforms built under emulation. |
| `sources` | `InputsLock.to_dict()["sources"]`: one git identity per declared source (`[]` without inputs). |
| `inputs_spec_sha256` | Digest of the inputs declaration, or `null`. |
| `contexts` | Per context: `files`, `bytes`, `sha256` of the staged manifest, `pruned_directories`, `skipped_private`, `skipped_symlinks`. |
| `dockerfiles_sha256` | Per platform: digest of the Dockerfile that was built. |
| `network`, `source_date_epoch` | As built. |
| `tool` | `docker_sha256`, `buildx_version`, `buildx_builder`. |
| `outputs.files` | `{path: "sha256:..."}`. With several platforms each path is prefixed with `linux-<arch>/`. |
| `outputs.images` | `{name: {"image_id", "digest", "platform", "ref"}}`. With several platforms the key is `name/linux-<arch>` and the tag gets a `-<arch>` suffix. `image_id` is the config digest. `digest` is the manifest digest, or `null` when the engine keeps none (Docker's classic image store). |
| `steps` | Per invocation: `platform`, `kind`, `state`, `exit_code`, `seconds`. |
| `state`, `started_at`, `finished_at` | `"succeeded"`, RFC 3339 UTC. |

A receipt from the `rust-hello` example (abridged):

```json
{
  "revision": "piceli.build-receipt.v1",
  "spec_sha256": "sha256:6e9e8c3d7bb5416ff485d8d36bcf23cf10b385a102c23910f41380c2684f1bef",
  "builder": {
    "image": "docker.io/library/rust:1.97.0-bookworm",
    "digest": "sha256:8fa55b2f3ddf97471ab6a767bfa3f37e6bad0986ba823e75fea57e2a2a5c3073",
    "platform_manifests": {"linux/arm64": "sha256:b9e35fea1df440fc548fe293354d8dd74b2a762b117752c7471df8b3f25ee872"}
  },
  "platforms": ["linux/arm64"],
  "native_platform": "linux/arm64",
  "sources": [{
    "name": "piceli", "repository": "piceli", "subpath": "examples/builds/rust-hello",
    "commit": "fcc01a17dd08dff73b2b31ae64c2720dc09850a5", "dirty": true,
    "diff_sha256": "sha256:39bad9a9...", "changed_paths": 5, "...": "..."
  }],
  "contexts": {"app": {"files": 3, "bytes": 2519, "sha256": "sha256:65745046..."}},
  "outputs": {
    "files": {"bin/rust-hello": "sha256:bc297a0476d30d539e56c0ef6818640f14528f272f2406195836cddd421a5aa4"},
    "images": {"rust-hello": {
      "image_id": "sha256:b6d80dd1d0360f7d6aebfb1d950ba674f896f91c03a2149a29b9ea140df96f58",
      "digest": null, "platform": "linux/arm64", "ref": "piceli-examples/rust-hello:dev"
    }}
  },
  "started_at": "2026-09-24T17:06:49Z",
  "finished_at": "2026-09-24T17:06:50Z"
}
```

## Reproducibility

Rebuilding the same spec gives the same output digests:

- **Files.** The file digest is the same with a warm cache. It is also the
  same after `--no-cache` with an empty `target/` cache, as long as the
  compiler output is deterministic. `rust-hello` sets `codegen-units = 1`
  and `strip = true`, and builds in a fixed `/work`.
- **Images.** The image ID is the same with a warm cache, and after a
  `--no-cache` rebuild. The following choices make this work:
  - `SOURCE_DATE_EPOCH` fixes the config and history timestamps.
  - The build step lays each image's files out at their final paths and
    sets every entry, directories included, to the epoch.
  - The image stage copies that tree into the base in one layer.

  Without the last two steps, the layer records the build-time modification
  time of existing parent directories such as `/usr/local/bin`. Older
  BuildKit (before 0.13) cannot rewrite those timestamps.
- **The manifest digest.** It depends on the exporter. Docker's classic
  store keeps no manifest (`digest: null`). An OCI or registry export would
  add a compressed manifest whose digest depends on the compression settings.
  Compare `image_id` values, not manifest digests, across engines.
- **Your own Dockerfile.** You are responsible for the timestamps in your
  own stages.

Measured on an arm64 Mac (Docker Desktop, buildx 0.11.2, BuildKit 0.11.6):

| Run | Time |
| --- | --- |
| First run of `rust-hello` (compile about 3 s) | 5.6 s |
| Warm rerun | 1.2 to 1.7 s |
| Cold `--no-cache` rebuild with the base images present | under 3 s |

All runs produced the same file digest and image ID.

## Not in scope yet

- Build secrets (`--secret`).
- Pushing from the build itself. Deliver the built image with
  `piceli artifacts deliver --to oci://…`; see {doc}`node_delivery`.
- A single multi-platform manifest list. Each platform is loaded
  separately, because the classic Docker store cannot hold an index.
