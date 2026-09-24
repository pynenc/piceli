# Containerized builds

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

```{admonition} Maturity
:class: note

| Feature | Maturity |
| --- | --- |
| Spec, minimal contexts, pinned builder, receipt `piceli.build-receipt.v1` | beta: the receipt fields are a contract; new fields may be added |
| Drift checked over the staged files ({ref}`build-drift`) | beta (new in this release) |
| Streaming `--log` and `--progress` ({ref}`build-log`) | beta (new); the progress line format is for people, not parsers |
| Tag templates `{image_id:N}` ({ref}`build-tags`) | beta (new) |
| Smoke checks ({ref}`build-smoke`) | experimental (new): the isolation flags and limits may change |
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
tag = "{image_id:12}"              # or a literal such as "dev"; see "Image tags"
base = { image = "docker.io/library/debian:bookworm-slim", digest = "sha256:8820..." }
files = { "bin/rust-hello" = "/usr/local/bin/rust-hello" }
user = "65532:65532"
entrypoint = ["/usr/local/bin/rust-hello"]
# cmd = [...], workdir = "/..."; base may be "scratch"
smoke = { command = ["--self-test"], expect_exit = 0, timeout_seconds = 60 }  # optional
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

1. It records the inputs from {doc}`source_identity`, or verifies them
   against `--lock` (`source-drift` if they differ). A dirty source without
   `allow_dirty` is rejected with `source-identity`. From then on the build
   is judged by the files it staged, not by the whole checkout; see
   {ref}`build-drift`.
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
   BuildKit reported. A templated tag is resolved ({ref}`build-tags`).
6. It runs each image's smoke check, if one is declared ({ref}`build-smoke`).
7. It re-hashes every staged file and re-reads the spec
   ({ref}`build-drift`). Only then are the files published atomically into
   the output directory and the receipt written.

Build output never goes into the receipt. Pass `--log FILE` to keep it; it is
written while the build runs ({ref}`build-log`).

(build-drift)=
### Drift: what is checked and what is recorded

A long build must not be invalidated by edits to files it never read, and
must not be accepted if a file it did read changed underneath it. Piceli
therefore separates the two:

| | Checked (fails the build) | Recorded (provenance only) |
| --- | --- | --- |
| What | The content digest, size and executable bit of **every staged file** of every context, including a user `Dockerfile` (it is a context file); the spec's normalized declaration, re-read from `build.toml`. | The git identity of each whole source at the start (`sources`), and which sources moved during the build (`sources_changed_during_build`). |
| When | Each file is hashed at plan time, re-hashed while staged, and re-hashed after the last build step and smoke check. | At the start, and once more at the end. |
| On a difference | `context-changed` (a staged file) or `spec-changed` (the declaration). No file is published and no receipt is written. | The source's name is listed in `sources_changed_during_build`. The build succeeds. |

So, in a busy checkout declared with `allow_dirty = true`:

- editing a Makefile, docs, or any file outside the contexts' staged set
  during the build is recorded, never rejected;
- editing, deleting or `chmod`-ing a staged file is rejected, even if the
  context has no `source`;
- adding a new file that matches an `include` pattern is not drift: it was
  not staged, so the build did not consume it;
- editing a comment in `build.toml` is not drift; changing a declared value is.

A `--lock` is still compared with the whole source **at the start**: it
states what the operator approved. Use `piceli inputs verify --only NAME` to
check one source before a build.

(build-log)=
### Build log and progress

`--log FILE` is appended to while the build runs: complete lines, flushed as
they arrive, so `tail -f FILE` follows a long compile. A partial line is
written when its step ends. Each run starts with `### run <name> <time>`, and
each step is framed by `### <platform> <kind> started` and
`### <platform> <kind> <state> <seconds>s`. The file is created with mode
`0600` because build output can contain anything the build printed. It never
goes into the receipt or the JSON result.

On stderr, `--progress` chooses what you see while the build runs:

| `--progress` | stderr during the build |
| --- | --- |
| `steps` (default) | One line when each step starts and ends, e.g. `[piceli] 2/3 linux/arm64 image:rust-hello: succeeded in 1.0s`, and the drift check result. No build output. |
| `plain` | The step lines plus the raw build output as it arrives. |
| `quiet` | Nothing until the result. |

The machine-readable result is always the **last line**: the receipt JSON on
stdout on success, or the `{"state", "reason", "steps"}` JSON on stderr on
failure. Parse the last stderr line, not the whole stream.

In Python, pass `log=Path(...)`, `progress=callable(str)` and
`raw_output=callable(bytes)` to `BuildSpec.run`.

(build-tags)=
### Image tags

`[[output.image]] tag` is either a literal (`"dev"`) or a template with
`{image_id:N}`, the first `N` (4 to 64) hex digits of the built image's
config digest, or `{image_id}` for all 64. Literal text may surround it:
`"release-{image_id:12}"`. Nothing else may appear in braces.

With a template, Piceli loads the image under a private
`piceli-pending-<nonce>` tag, reads its ID, tags it with the resolved tag,
removes the pending tag and inspects the result again. The receipt's
`outputs.images.<name>.ref` is the resolved reference, e.g.
`piceli-examples/rust-hello:b6d80dd1d036`. Two builds with different content
therefore get different tags and both stay in the engine, and a tag never
moves to other content. With several platforms the tag gets the usual
`-<arch>` suffix. A literal tag keeps its old behavior: each build moves it.

(build-smoke)=
### Smoke checks

An image output may declare a check that runs after the image is built:

```toml
smoke = { command = ["--self-test"], expect_exit = 0, timeout_seconds = 60 }
```

| Field | Meaning |
| --- | --- |
| `command` | Arguments after the image, so they replace `CMD` and keep the entrypoint. `[]` runs the image's default command. |
| `expect_exit` | The exit code that passes, 0 to 255 (default 0). A check such as "starts and fails only at a declared later step" expects that step's code. |
| `timeout_seconds` | At most 600 (default 60). |

It runs once per platform as
`docker run --rm --name piceli-smoke-<nonce>-<n> --pull never --platform <p>
--network none --read-only --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m
--cap-drop ALL --security-opt no-new-privileges --pids-limit 256 --memory 512m
<image_id> <command...>`, as the image's own user. It references the image by
ID, so no tag and no registry are involved. An emulated platform needs a
binfmt handler on the engine.

The step is recorded in `steps` as `kind = "smoke:<name>"`; its output goes to
the log only. A different exit code fails the run with `smoke-failed`, and a
timeout with `smoke-timed-out` (the container is then removed). Either way no
file is published and no receipt is written. The image itself stays loaded,
so you can inspect it.

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
- `--log`: stream the build output to a file ({ref}`build-log`).
- `--progress steps|plain|quiet`: what stderr shows during the build.
- `--max-seconds`: the grant lifetime.

Exit codes: `0` success, `1` a build step or smoke check failed or timed out
(stderr lists the steps, never their output), `2` rejected. The last stderr
line is `{"state": "failed" | "rejected", "reason": "<code>"}` with a fixed
code. Paths are never echoed.

| Code | Exit | Cause | Fix |
| --- | --- | --- | --- |
| `invalid-spec` | 2 | The spec is malformed: unknown field, bad value, bad tag template or smoke table. | Correct `build.toml`; the message names the field (Python API only). |
| `spec-unreadable` | 2 | `--spec` cannot be read. | Check the path. |
| `spec-changed` | 2 | The declaration in `build.toml` changed during the build. | Rebuild; do not edit declared values while a build runs. |
| `invalid-grant` | 2 | `--max-seconds` or the approval digests are malformed. | Pass `sha256:<64 hex>` and a positive duration. |
| `builder-not-approved` | 2 | `--approve-builder` differs from `builder.digest`. | Review the builder and approve its digest. |
| `plan-not-approved` | 2 | `--approve-plan` differs from the preview's `plan_hash`. | Re-run `preview`, review, approve the new hash. |
| `network-not-granted` | 2 | `network = "default"` without `--allow-network`. | Grant network, or build offline. |
| `grant-expired` | 2 | The grant expired before or during the build. | Raise `--max-seconds`. |
| `invalid-tool` | 2 | Only one of `--docker`/`--docker-sha256` given. | Give both, or neither. |
| `context-missing` | 2 | A context directory is missing or unreadable. | Fix `path`/`source`. |
| `context-empty` | 2 | `include` matched no files. | Fix the patterns. |
| `context-symlink` | 2 | A pattern matches a symbolic link. | Exclude it or copy the file. |
| `context-budget-exceeded` | 2 | More than `max_files` or `max_bytes`. | Narrow `include` or raise the budget. |
| `context-changed` | 2 | A staged file changed, vanished or changed mode between plan, staging and the end of the build. | Rebuild once the files are stable. Edits elsewhere are not drift. |
| `dockerfile-unpinned` | 2 | A user Dockerfile references an unpinned image or a remote `ADD`. | Pin it by digest or pass it as a pinned arg. |
| `source-drift` | 2 | The sources differ from `--lock` at the start (also: a partial lock). | Re-record the lock, or restore the sources. |
| `source-identity` | 2 | A source is not a git root, has no commit, misses its `ref`, or is dirty without `allow_dirty`. | Fix the checkout or `inputs.toml`. |
| `invalid-inputs` | 2 | `--inputs`/`--lock` is malformed. | Fix the file. |
| `docker-unavailable` | 2 | `docker` is missing or a probe failed. | Install docker/buildx; check the context. |
| `output-invalid` | 2 | Extracted files are missing, extra, not regular or too large. | Fix `output.file` or the build. |
| `image-invalid`, `image-mismatch` | 2 | The loaded image cannot be inspected, has another platform, or differs from what BuildKit reported. | Rebuild; check for a concurrent build of the same tag. |
| `cancelled` | 2 | The run was cancelled (Python API). | — |
| `build-failed` | 1 | A `docker buildx build` step exited non-zero. | Read `--log`. |
| `build-timed-out` | 1 | A step exceeded `timeout_seconds` or the grant. | Raise `timeout_seconds`/`--max-seconds`. |
| `smoke-failed` | 1 | A smoke check exited with another code than `expect_exit`. | Read `--log`; fix the image or the check. |
| `smoke-timed-out` | 1 | A smoke check exceeded its `timeout_seconds`. | Fix the image or raise the timeout (at most 600). |
| `invalid-or-unavailable-build-input` | 2 | Any other unreadable or malformed input (the detail is withheld so no path or output leaks). | Run `preview` and check the files it names. |

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
| `sources` | `InputsLock.to_dict()["sources"]`: the git identity of each declared source at the start of the build (`[]` without inputs). Provenance. |
| `sources_changed_during_build` | Names of sources whose whole-checkout identity differed at the end (`[]` if none, `null` if they could not be recaptured). Provenance only; see {ref}`build-drift`. |
| `inputs_spec_sha256` | Digest of the inputs declaration, or `null`. |
| `contexts` | Per context: `files`, `bytes`, `sha256` of the staged manifest, `pruned_directories`, `skipped_private`, `skipped_symlinks`. |
| `dockerfiles_sha256` | Per platform: digest of the Dockerfile that was built. |
| `network`, `source_date_epoch` | As built. |
| `tool` | `docker_sha256`, `buildx_version`, `buildx_builder`. |
| `outputs.files` | `{path: "sha256:..."}`. With several platforms each path is prefixed with `linux-<arch>/`. |
| `outputs.images` | `{name: {"image_id", "digest", "platform", "ref"}}`. With several platforms the key is `name/linux-<arch>` and the tag gets a `-<arch>` suffix. `image_id` is the config digest. `digest` is the manifest digest, or `null` when the engine keeps none (Docker's classic image store). `ref` is the resolved tag for a template. |
| `steps` | Per invocation and smoke check: `platform`, `kind` (`files`, `image:<name>`, `smoke:<name>`), `state`, `exit_code`, `seconds`. |
| `state`, `started_at`, `finished_at` | `"succeeded"`, RFC 3339 UTC. |

A receipt from the `rust-hello` example (abridged):

```json
{
  "revision": "piceli.build-receipt.v1",
  "spec_sha256": "sha256:dbf9f2dba0e048410535dd479a67932fe65ab0666089576ba1919e9b305694db",
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
      "digest": null, "platform": "linux/arm64", "ref": "piceli-examples/rust-hello:b6d80dd1d036"
    }}
  },
  "sources_changed_during_build": [],
  "steps": [
    {"platform": "linux/arm64", "kind": "files", "state": "succeeded", "exit_code": 0, "seconds": 0.602},
    {"platform": "linux/arm64", "kind": "image:rust-hello", "state": "succeeded", "exit_code": 0, "seconds": 1.002},
    {"platform": "linux/arm64", "kind": "smoke:rust-hello", "state": "succeeded", "exit_code": 0, "seconds": 0.654}
  ],
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
