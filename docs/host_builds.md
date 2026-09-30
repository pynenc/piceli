# Builds without a VM

```{admonition} Maturity: experimental
:class: note

`builder="host"` and the `piceli.host-build.v1` format are new in 0.9.0 and
may change in a minor release, with a changelog entry. The Docker builder
({doc}`containerized_builds`) stays the default. See the {doc}`roadmap` for
every feature's status.
```

A **host build** compiles with the toolchain installed on the machine that
runs `piceli deploy` (for Rust: `cargo`, and `cargo zigbuild` to cross-compile
for `linux/arm64` or `linux/amd64` from macOS) and assembles the image itself.
No container engine takes part, so on macOS Docker Desktop and its VM can
stay closed; a laptop keeps its memory for the compiler.

```python
from piceli import App, Build, Pipeline, Registry, Target

target = Target.kubeconfig(
    "cluster.kubeconfig",
    context="my-cluster",
    namespace="shop",
    nodes={"worker": "worker-1"},
)
images = Build.spec("host-build.toml", builder="host")  # opt-in, per build
app = App("shop")
app.deployment("api", image=images["api"], ports=[8080])
pipeline = Pipeline(
    app, target, build=images, deliver=Registry("oci://registry.example:5000/shop")
)
```

`Build.spec(path)` without `builder=` keeps building a `build.toml` in the
local Docker engine. A spec written for one builder is refused by the other
(`build-builder-mismatch`).

## What a host build does

1. **Plan.** `piceli deploy --plan` reads the target node's facts (below),
   scans the declared contexts, and pins every declared tool by the sha256
   of its binary. Nothing runs. The plan shows `builder: host`, the tools,
   the platform and the node facts; the build plan hash covers the context
   files, the tools, the substituted commands and `env`, and the facts.
2. **Stage.** The contexts are copied to `<cache>/stage/<name>/<context>`:
   the same path every build. The stage is synchronised, not recreated: an
   unchanged file keeps its modification time, a changed one gets the current
   time, an undeclared one is removed. The compiler sees exactly the edited
   files.
3. **Compile.** Each command runs on the host with a minimal environment:
   `PATH` holds only the directories of the declared tools and the system
   default, `CARGO_TARGET_DIR` is `<cache>/target`, **one target directory
   shared by every host build of the cache**, and `SOURCE_DATE_EPOCH`. From
   the caller's environment only locators pass (`HOME`, `USER`, `TMPDIR`,
   `CARGO_HOME`, `RUSTUP_HOME`, `RUSTUP_TOOLCHAIN`, `SSL_CERT_FILE`,
   `NIX_SSL_CERT_FILE`). A tool whose binary changes during the build fails
   it (`tool-pin-mismatch`).
4. **Assemble.** Each image is its base, pulled once by its pinned digest
   into `<cache>/blobs`, plus **one layer per declared file mapping**
   (sorted by destination, directories first). Layers are deterministic:
   sorted entries, `mtime = source_date_epoch`, root owner unless declared,
   modes `0755`/`0644`, a gzip header without name or time. The same files
   give the same digest on any machine. The image is written as an OCI
   image-layout tar under the build's outputs and read back to check its
   digests.
5. **Deliver.** Delivery pushes the archive by digest and sends only the
   blobs the registry lacks. A one-line change that alters one binary
   uploads that binary's layer and the new config; the base and every other
   layer are skipped (the delivery receipt lists each blob as `uploaded` or
   `skipped`). `NodeImport` imports the same archive; its `docker://`
   transport (a kind node container) still needs the docker CLI to reach the
   node, `ssh://` does not.

```{warning}
A host build runs the declared tools **as your user, outside any container**:
build scripts (`build.rs`, proc macros) can read your home directory and use
the network. Approving the plan approves that. Use the Docker builder for
sources you do not trust.
```

## Target node facts

The build compiles for one node: `Build.spec(..., builder="host", node="worker")`
(default: the target's only declared node). At plan time Piceli reads that
Node object (one read-only `GET`) and records:

| Fact | Source | Placeholder |
| --- | --- | --- |
| `platform`, `architecture` | `status.nodeInfo` (`linux/arm64`, `arm64`) | `{platform}`, `{arch}`, `{rust_arch}` (`aarch64`, `x86_64`) |
| `page_size` | label `piceli.io/page-size`, else the kernel release (`16k`, `64k` suffixes, the Raspberry Pi 5 `rpi-2712` kernel: 16 KiB), else the architecture (4 KiB) | `{page_size}`, `{page_size_log2}` |
| `kernel_version` | `status.nodeInfo.kernelVersion` | shown, not substituted |

`page_size_source` says which rule gave the page size (`label`, `kernel` or
`architecture`). When a kernel is not recognised, check `getconf PAGESIZE`
on the node and label it: `kubectl label node worker-1 piceli.io/page-size=16384`.
A new label value changes the plan hash, so the next deploy rebuilds.

Placeholders work in `build.commands`, `build.env` values and
`target_files` paths. For example jemalloc fixes its page size at compile
time:

```toml
env = { JEMALLOC_SYS_WITH_LG_PAGE = "{page_size_log2}" }
```

## `host-build.toml`

```toml
revision = "piceli.host-build.v1"
name = "shop"
inputs = "inputs.toml"                  # optional source identity

[build]
tools = ["cargo", "cargo-zigbuild", "zig"]   # the only programs a command may start
commands = [
    ["cargo", "zigbuild", "--release", "--locked",
     "--target", "{rust_arch}-unknown-linux-gnu.2.36",
     "--manifest-path", "app/Cargo.toml"],
]
env = { CARGO_TERM_COLOR = "never", JEMALLOC_SYS_WITH_LG_PAGE = "{page_size_log2}" }
# platform = "linux/arm64"   # optional: refuse a node with another platform
# workdir = "."              # relative to the stage root
source_date_epoch = 0
timeout_seconds = 1800

[context.app]                   # staged into <stage>/app (or `into = "dir"`)
source = "shop"
path = "."
include = ["Cargo.toml", "Cargo.lock", "src/**", "crates/**"]

[[output.image]]
name = "api"
repository = "shop/api"
base = { image = "docker.io/library/debian:bookworm-slim", digest = "sha256:…" }
target_files = { "{rust_arch}-unknown-linux-gnu/release/api" = "/usr/local/bin/api" }
files = { "app/static" = "/srv/static" }          # from the stage (files or trees)
dirs = { "/var/lib/api" = "10001:10001:0700" }    # owned directories
user = "10001:10001"
entrypoint = ["/usr/local/bin/api"]
env = { RUST_LOG = "info" }
```

- `target_files` paths are relative to the shared target directory,
  `files` paths to the stage root. Each entry is one layer.
- `base` must be pinned by digest (an index or a manifest); `"scratch"` or
  no base builds from nothing. Only `tar` and `tar+gzip` base layers are
  supported (`base-layer-unsupported`).
- An `entrypoint` without `cmd` clears the base image's `Cmd`, as a
  Dockerfile `ENTRYPOINT` does.
- `build.env` cannot set `PATH`, `CARGO_TARGET_DIR` or `SOURCE_DATE_EPOCH`.
- There is no smoke check: nothing can run a `linux/arm64` binary on the
  build machine without a VM. Use {doc}`checks` after the rollout.

A full example: `examples/builds/rust-hello/host-build.toml`.

## Several platforms

A `host-build.toml` may declare `platforms = ["linux/amd64", "linux/arm64"]`
in `[build]`. `piceli artifacts build-spec run --spec host-build.toml` then
builds every platform (without a node: the platform's facts, 4 KiB pages)
and joins each image's manifests in an OCI image index; `piceli artifacts
publish` pushes them for other people's clusters. See
{doc}`publishing_images`. A deploy still builds for its one node, and the
node's platform must be in the list.

## The cache directory

`Build.spec(..., cache_dir="…")` (relative to the declaring file) sets where
the stage, the shared target directory and the base blobs live; the default
is `<state_dir>/toolchains/host-build`, reported by `piceli cache status`
under `toolchains` and `blobs`. Builds that share a cache share their
compiled dependencies. A `.lock` file per build name keeps two runs from
syncing the same stage at once. Deleting the directory is always safe: the
next build compiles and pulls again.

## Receipts

The build receipt (`piceli.build-receipt.v1`) records `builder: {kind: host,
tools: {name: sha256}}`, `node_facts`, and for each image its config digest
(`image_id`), manifest digest, platform, archive path (relative to the
outputs), every layer (`digest`, `diff_id`, `size`, `origin`: `base` or
`build`) and every file the build put in it (`path`, `sha256`, `size`).

## SBOM and provenance

Next to each archive the build writes `attestations/<image>.spdx.json`, an
SPDX 2.3 document (the image by manifest digest, its base by digest, every
file the build put in it with its sha256, and every package pinned by a
staged `Cargo.lock`, with its registry checksum and `pkg:cargo` URL; the
lockfiles pin a superset of what one binary links), and
`attestations/<image>.provenance.json`, an in-toto Statement v1 with a SLSA
provenance v1 predicate (subject: the manifest digest; the spec and plan
hashes, commands, `env`, node facts, tool digests, source commits,
contexts and base). The receipt records both by path and sha256. They hold
no local path, secret or process output. The SBOM is deterministic; the
provenance records the build's start and end times.

## Errors

| Code | Meaning |
| --- | --- |
| `node-facts-unavailable` | The node reports no supported architecture or kernel |
| `node-page-size-invalid` | The `piceli.io/page-size` label is not 4096, 16384 or 65536 |
| `node-platform-mismatch` | `build.platform` differs from the node's |
| `host-tool-missing` | A declared tool is not on `PATH` at plan time |
| `host-output-missing` | An image file does not exist after the build |
| `base-image-unavailable`, `base-image-invalid`, `base-platform-unavailable`, `base-layer-unsupported` | The base image cannot be used |
| `build-builder-mismatch` | The spec was written for the other builder |

`piceli explain <code>` prints the cause and the fix of each.
