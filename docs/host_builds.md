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
   it (`tool-pin-mismatch`). Before the first command, the toolchains the
   spec pins are installed into the cache and selected (below).
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

### Building while the API is down

Every successful read is cached in `<state_dir>/node-facts/<node>.json` (the
facts and when they were read). When the API cannot be reached, a plan or
build uses that cache, says so on stderr with its age ("using node facts
cached 12 minute(s) ago"), and the plan hash covers the facts used, so a build
planned from the cache and one planned from a live read agree while the node
is unchanged. Without a cache (a fresh checkout) it stops with
`node-facts-unavailable`.

To build with no API at all, declare the facts:

```python
Build.spec(
    "host-build.toml",
    builder="host",
    node_facts={
        "node": "worker-1",
        "os": "linux",
        "architecture": "arm64",
        "kernel_version": "6.6.51+rpt-rpi-2712",
        "page_size": 16384,
        "page_size_source": "label",
    },
)
```

Declared facts are never read from or written to the cluster.

Placeholders work in `build.commands`, `build.env` values and
`target_files` paths. For example jemalloc fixes its page size at compile
time:

```toml
env = { JEMALLOC_SYS_WITH_LG_PAGE = "{page_size_log2}" }
```

(host-builds-source-rev)=
### The source commit in `build.env` (0.37.0)

A `build.env` value may also use `{source_rev:<context>}`: the full commit
of the source the named context reads (its `source`, from `inputs`). It
lets a program know the commit it was built from:

```toml
env = { APP_BUILD_REV = "{source_rev:shop}" }

[context.shop]
source = "shop"
path = "."
include = ["src/**"]
```

Piceli fills it in when the build runs, after the plan hash: a new commit
that changes none of the staged files keeps the same plan hash, so it needs
no new approval. The plan and its preview
show the placeholder, not the commit. The name must be a declared context
that names a source; anything else fails when the file is loaded. Only
`build.env` values accept it.

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
# toolchains = { rust = "app/rust-toolchain.toml", node = "app/.nvmrc" }   # optional, below
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
labels = { "org.opencontainers.image.licenses" = "MIT", "org.opencontainers.image.source" = "https://example.org/shop" }
```

- `target_files` paths are relative to the shared target directory,
  `files` paths to the stage root. Each entry is one layer.
- `base` must be pinned by digest (an index or a manifest); `"scratch"` or
  no base builds from nothing. Only `tar` and `tar+gzip` base layers are
  supported (`base-layer-unsupported`).
- An `entrypoint` without `cmd` clears the base image's `Cmd`, as a
  Dockerfile `ENTRYPOINT` does.
- `labels` (0.31.0) are the image config's labels, the same on a build host
  and in the cluster; registries read the OCI ones
  (`org.opencontainers.image.licenses`, `.source`, `.description`). A
  `build.toml` image takes the same table (`docker buildx build --label`).
- `build.env` cannot set `PATH`, `CARGO_TARGET_DIR`, `SOURCE_DATE_EPOCH` or
  `PICELI_SHARED_TARGET_DIR`.
- `contexts = ["api", "config"]` (optional, default every context) names
  the contexts an image reads. A composition controller rebuilds an image
  only when the files of those contexts (or its tables) changed (see
  {doc}`compositions`); a local build still builds every image.
- There is no smoke check: nothing can run a `linux/arm64` binary on the
  build machine without a VM. Use {doc}`checks` after the rollout.
- **A command that names a file its context does not stage is refused when
  the spec is loaded** (0.41.0, `host-build-path-not-staged`). A context is
  staged as `<stage>/<into or name>/` with only the files its `include`
  selects, so `["bash", "app/scripts/build.sh"]` with
  `include = ["src/**"]` would fail on the build host with `No such file or
  directory`. The error names the command, the path and the include list.
  The check is careful not to refuse a good spec: it looks only at plain
  relative paths whose first directory is a context's (taking `workdir`
  into account), and in a `sh -c` / `bash -c` script only at the programs it
  runs. It skips paths with a placeholder (`{rust_arch}`), paths after an
  output option (`-o`, `--out-dir` …), commands that write files (`cp`,
  `mkdir` …), directories that hold staged files and an image's `files`
  paths.

A full example: `examples/builds/rust-hello/host-build.toml`.

## Plain files, without an image (0.39.0)

To ship binaries as release downloads, declare `[[output.files]]` instead
of (or beside) `[[output.image]]`. No image is assembled for them:

```toml
[build]
tools = ["cargo", "cargo-zigbuild", "zig"]
platforms = ["linux/amd64", "linux/arm64"]
commands = [["cargo", "zigbuild", "--release", "--locked",
             "--target", "{rust_arch}-unknown-linux-gnu.2.36"]]

[[output.files]]
name = "bin"
target_files = { "{rust_arch}-unknown-linux-gnu/release/app-cli" = "app-cli-{arch}",
                 "{rust_arch}-unknown-linux-gnu/release/app-agent" = "app-agent-{arch}" }
```

- Keys are paths under the target directory, as an image's `target_files`;
  each must be a regular file. Values are file names (one path segment,
  placeholders allowed). Put `{arch}` in the name when you build several
  platforms, so the files keep different names when published together.
- Each file is copied to `files/<name>/<file name>` in the output
  directory (`<platform>/files/…` for several platforms, for example
  `linux-arm64/files/bin/app-cli-arm64`).
- The receipt lists each file (see [Receipts](#receipts)).
  `piceli artifacts publish-files --receipt receipt.json --to … --tag …
  --artifact-type …` publishes them (see {doc}`release_files`).
- A spec with only files is for `piceli artifacts build-spec run`. A deploy
  (`Build.spec(…, builder="host")`, `piceli build job`) needs at least one
  image and refuses it (`pipeline-invalid`); a composition controller builds
  images only and refuses any `[[output.files]]` (`composition-invalid`).
- Adding the section changes the spec digest and the plan hash only of a
  spec that declares it.

## Steps that are the same for every platform

Some steps give the same result whatever the node: a browser bundle compiled
for `wasm32`, generated assets, a site. Declared in `build.commands`, a
multi-platform build would run them once per platform. Declare them in
`build.shared_commands` instead (0.23.0):

```toml
[build]
tools = ["cargo", "cargo-zigbuild", "zig", "sh"]
platforms = ["linux/amd64", "linux/arm64"]
shared_commands = [
    ["cargo", "build", "--release", "--locked", "--target", "wasm32-unknown-unknown",
     "--manifest-path", "app/Cargo.toml", "-p", "web", "--lib"],
    ["sh", "-ec", "mkdir -p \"$CARGO_TARGET_DIR/site\" && cp -R app/web/assets/. \"$CARGO_TARGET_DIR/site/\""],
]
commands = [
    ["cargo", "zigbuild", "--release", "--locked",
     "--target", "{rust_arch}-unknown-linux-gnu.2.36",
     "--manifest-path", "app/Cargo.toml", "-p", "server"],
]

[[output.image]]
name = "server"
repository = "shop/server"
target_files = { "{rust_arch}-unknown-linux-gnu/release/server" = "/usr/local/bin/server" }
shared_files = { "site" = "/srv/site" }      # from the shared target directory
```

- Shared commands run before `build.commands` (or alongside them, see
  below), from the same working directory, with the same tools and
  toolchains. A build for several
  platforms runs them once, with its first platform; the other platforms
  skip them (their log says so) and copy the same outputs into their images.
  A build for one platform (a deploy) runs them every time, first.
- Their target directory is `<cache>/target/shared`, the same for every
  platform: it is their `$CARGO_TARGET_DIR`, and every command of the build
  finds it in `$PICELI_SHARED_TARGET_DIR` (set only when the spec declares
  shared commands). A later per-platform command reads their outputs there,
  and an image copies them with `shared_files` (paths relative to that
  directory). Write their outputs there, not into the stage: the stage is
  synchronised again for each platform, which removes anything the build
  wrote into it.
- They cannot use the node's facts: a placeholder (`{rust_arch}`,
  `{page_size_log2}` …) in a shared command or a `shared_files` path is
  refused (`invalid-spec`). A `build.env` value that uses a placeholder is
  not set for them; the others are. The plan preview shows both
  (`shared_commands`, `shared_env`).
- The plan hash and the provenance cover them. Each runs as a step of kind
  `shared-<n>` with platform `any`; a multi-platform receipt lists it once,
  in `steps` and in `shared_commands` (`ran_with`: the platform whose run
  ran it, and its steps with their times).
- A spec without `shared_commands` builds, hashes and records exactly as
  before.

Piceli does not find such steps by itself: a command whose arguments are the
same for every platform still sees its platform's `$CARGO_TARGET_DIR` and
`build.env`, and its outputs land in that platform's target directory, so
running it once is only correct when the spec says so.

### Shared commands alongside the platform commands (0.78.0)

By default the shared commands finish before the first platform's commands
start, so a platform command may read their outputs. When no platform
command reads them (only images do, through `shared_files`), the build can
run both at once:

```toml
[build]
shared_commands_alongside = true
```

- The shared commands start at the same time as the first platform's
  commands, in a thread of their own. They keep their own target directory
  (`<cache>/target/shared`, the platform's is `<cache>/target/<arch>-<page
  size>`), so cargo's build-directory lock never makes one side wait for the
  other. Both share `CARGO_HOME`; cargo's package-cache lock covers that
  (you may see a short "waiting for file lock" line).
- An image is assembled once its platform's commands have finished and
  every shared command that names it has finished (every shared command,
  for a plain argv entry; see the next section for `images`). The
  platform's `[[output.files]]` need only its commands. The next platform
  starts once the first platform's images are assembled, so a two-platform
  build takes about the longer of the two sides plus the second platform.
- The first failure on either side fails the build as before and stops the
  other side: its running command gets `SIGTERM`, then `SIGKILL` five
  seconds later, and is recorded with state `cancelled`. The error, the
  receipt's `steps` (the failing step last) and the log name the failing
  command; the log also says which command was stopped and why.
- A platform command must then not read the shared outputs: they may not
  exist yet. Leave the field out when one does.
- The field is part of the spec digest and the plan hash (and the plan
  preview shows it) only when it is `true`; it needs `build.shared_commands`
  (`invalid-spec` otherwise).

Every step of a host build records `start_seconds` (0.78.0): when it started,
in seconds from the start of the whole build (all platforms), next to its
`seconds`. Steps that overlap show it: `piceli gitops builds` prints the
wall time of the commands and, when they overlapped, the sum as `of work`.

## Steps for some images only (0.68.0)

A composition controller builds only the images whose change key changed
(see {doc}`compositions`). Every command still ran for them, and every image
was assembled. To skip the steps of an image that did not change, write the
command as a table that names its images, in `build.commands` or
`build.shared_commands`:

```toml
[build]
tools = ["cargo", "sh"]
shared_commands = [
    { run = ["sh", "-ec", "cargo build --release -p site --target wasm32-unknown-unknown"], images = ["site"] },
]
commands = [
    { run = ["cargo", "build", "--release", "-p", "api"], images = ["api"] },
    ["cargo", "build", "--release", "-p", "common"],   # a plain list: every build
]
```

- A build of some images runs a table command only when one of its
  `images` is among them, and assembles, packages and pushes only those
  images. The other images keep the image they already have.
- A plain argv list runs in every build, as before.
- `images` lists 1 to 32 names of `[[output.image]]` tables of the same
  spec, each once; `run` is checked like a plain command. Anything else is
  refused when the spec is loaded (`invalid-spec`).
- The plan, its preview and its hash hold only the commands and images the
  build runs, so a build of `api` alone has another plan hash than a build
  of every image. A local `piceli artifacts build-spec run` builds every
  image and runs every command.
- `$PICELI_SHARED_TARGET_DIR` stays set when the build runs none of the
  shared commands: a command can read their outputs of an earlier build.

(host-builds-derived)=
## Derived images (0.89.0)

A release often adds one small file (its number) to an image the test
environment already ran. Rebuilding for that takes minutes and ships digests
nobody tested. A **derived image** instead takes a built image of the same
spec, by its digest, and appends the files as one layer. Nothing is compiled
and no command runs:

```toml
[context.release]
source = "decisions"
include = ["release/**"]

[[output.image]]
name = "web-base"
repository = "shop/web-base"
contexts = ["app"]                     # the base no longer reads the decision
# base, target_files, entrypoint ... as usual

[[output.image]]
name = "web"
repository = "shop/web"
from = "web-base"                      # another [[output.image]] of this spec
files = { "release/release.json" = "/usr/local/share/release.json" }   # from the stage
contexts = ["release"]                 # only these feed its key
```

- A derived image takes only `name`, `repository`, `from`, `files`,
  `contexts` and `labels`. Anything else (`base`, `target_files`,
  `entrypoint`, `user` ...) is refused when the spec is loaded
  (`invalid-spec`). `from` must name a built `[[output.image]]` of the same
  spec: not itself, not another derived image. Each `files` key must be in
  a context the image reads, and a command's `images` cannot name it.
- **Its change key** (see {doc}`compositions`) is the base image's key plus
  the files of its own contexts. A new release decision changes only the
  derived keys; a change of the base changes both.
- **No build.** When only derived keys changed, a composition controller
  claims no build host and starts no build Job. It reads the base image the
  environment already recorded for the base's key, pinned by digest, and
  appends the files in the cluster registry over the OCI (Open Container
  Initiative) Distribution API, as `crane append` does: the base layers and
  config are kept, the config's `rootfs.diff_ids` and `history` get one
  entry, and a new manifest is pushed by digest. The base layers are mounted
  from the base's repository, not downloaded.
- **Never from a rebuilt base.** When the base's key is the one the
  environment last recorded but the controller lost its image record, the
  controller does not build the base again on its own: the step fails with
  `derived-base-unrecorded`. Rebuild it on purpose with
  `piceli gitops sync ENV --component BASE`.
- **Several platforms.** A base that is an OCI image index gets one derived
  manifest per platform and a new index over them (attestation manifests of
  the base are left out).
- **The same files give the same layer**: sorted entries, `mtime` from
  `build.source_date_epoch`, root owner, modes `0644`/`0755`, a gzip header
  without name or time. One layer blob is shared by every image derived with
  the same files.
- **Recorded like a built image**: digest, `pull_ref`, and a provenance
  record with `builder: derive`, `derived_from: {image, digest}`, the added
  files' sha256 and the layer digest. The status, `piceli gitops history`
  and `piceli gitops show --json` show `derived_from` on the derived
  component, so a release can compare it with the digest of the run that
  passed. The SLSA (Supply-chain Levels for Software Artifacts) provenance
  statement (build type `https://piceli.dev/derived-image/v1`) names the
  base digest and each file's sha256; it is attached to the image as an OCI
  referrer when the registry takes it.
- The controller, its builder image and every build host must run 0.89.0
  or later: an older one refuses a spec with `from` (`invalid-spec`).
- **Only a composition controller makes them.** `piceli artifacts
  build-spec run` refuses a spec with a derived image
  (`derived-image-local-unsupported`). A `piceli deploy` outside a
  composition does not make them either: an app that uses one is refused
  (`pipeline-image-unknown`).

## The toolchain the source pins

By default a command uses whatever Rust, node or wasm tools the build
machine has: on a laptop your own, in the cluster the builder image's. A spec
can instead pin them in `[build]`, so the build follows the source:

```toml
[build]
tools = ["cargo", "sh"]
commands = [["sh", "-c", "cargo build --release --target wasm32-unknown-unknown && wasm-bindgen … && wasm-opt …"]]
toolchains = { rust = "app/rust-toolchain.toml", node = "app/.nvmrc", wasm-bindgen = "app/Cargo.lock", binaryen = "123" }
```

Every key is optional. A value is a path relative to the stage root
(`<context>/<file>`, and the context must include the file) or, except for
`rust`, a version:

| Key | Reads | Installs |
| --- | --- | --- |
| `rust` | a `rust-toolchain.toml` (or legacy `rust-toolchain`): channel, targets, components, profile; plus the build platform's `<arch>-unknown-linux-gnu`, which a host build links for | `rustup toolchain install` into `<cache>/toolchains/rustup` |
| `node` | a `.nvmrc` or `.node-version` holding an exact version, or `"24.11.0"` | the `nodejs.org` tarball, checked against that release's `SHASUMS256.txt` |
| `wasm-bindgen` | the `wasm-bindgen` version of a `Cargo.lock`, or `"0.2.114"` | `cargo install --locked wasm-bindgen-cli --version …`, with the pinned Rust |
| `binaryen` | a file holding a version, or `"123"` | the GitHub release tarball, checked against its published `.sha256` |

- **Plan.** The versions are read from the scanned contexts. The plan hash
  and the preview cover them, and a composition's image change key covers
  the pin files (even in a context the image does not read), so changing a
  pin rebuilds. A node alias (`lts/*`) or a major alone (`24`) is refused:
  it would not give the same tool tomorrow.
- **Install.** Each tool is installed once per cache, keyed by its version
  (Rust: by the parsed pin), under a file lock, so concurrent builds install
  it once; a done-marker lets the next build skip it. Downloads are for the
  machine that runs the commands (`linux` or `darwin`, `amd64` or `arm64`);
  in the cluster that is the builder node. `rustup` and `cargo` are looked
  up on the commands' `PATH`, then on yours.
- **Run.** `RUSTUP_HOME` and `RUSTUP_TOOLCHAIN` select the pinned Rust (the
  rustup proxies of a declared `cargo` honour them; `build.env` cannot set
  them), and the `bin` directories of node, wasm-bindgen and binaryen come
  first on `PATH`. Commands still start declared tools only: call `node`,
  `wasm-bindgen` or `wasm-opt` from a declared `sh`, a build script or a
  cargo command.

```text
<cache>/toolchains/rustup/                          RUSTUP_HOME
<cache>/toolchains/node/<version>-<os>-<arch>/bin
<cache>/toolchains/binaryen/<version>-<os>-<arch>/bin
<cache>/toolchains/wasm-bindgen/<version>-<os>-<arch>/bin
<cache>/toolchains/<tool>-<key>.done
```

Installing needs public HTTPS once per version. The receipt's `toolchains`
records each tool's pin, whether this build installed it, how long it took,
and the download's URL and sha256; the provenance records the pins and the
downloads (below). Development runs read the same pins ({doc}`dev_builds`).

## Several platforms

A `host-build.toml` may declare `platforms = ["linux/amd64", "linux/arm64"]`
in `[build]`. `piceli artifacts build-spec run --spec host-build.toml` then
builds every platform (without a node: the platform's facts, 4 KiB pages)
and joins each image's manifests in an OCI image index; `piceli artifacts
publish` pushes them for other people's clusters. See
{doc}`publishing_images`. `piceli deploy` still builds for its one node,
and the node's platform must be in the list. A GitOps pipeline
environment (0.74.0) builds every declared platform and records the index
digest, so a release can copy it by digest (see
{ref}`pipeline-declared-platforms`). Steps that are the same for every
platform (a `wasm32` bundle) run once when declared in
`build.shared_commands` (see above).

## The cache directory

`Build.spec(..., cache_dir="…")` (relative to the declaring file) sets where
the stage, the shared target directory and the base blobs live; the default
is `<state_dir>/toolchains/host-build`, reported by `piceli cache status`
under `toolchains` and `blobs`. Builds that share a cache share their
compiled dependencies, except across architectures and page sizes: the shared
target directory is `<cache>/target/<architecture>-<page size>` (for example
`arm64-16384`), so builds for 4 KiB and 16 KiB pages keep separate caches
instead of recompiling jemalloc and everything above it on each switch.
`build.shared_commands` use `<cache>/target/shared`, shared by every
platform. A `.lock` file per build name keeps two runs from
syncing the same stage at once. Deleting the directory is always safe: the
next build compiles and pulls again.

**Nothing cached is trusted by its name alone (0.22.0).** A staged file is
reused only when its size and sha256 match the context's; a blob (a base
layer, a manifest, a config) only after its bytes were hashed again and
matched its digest. A blob that does not match (a faulty disk or memory, a
write cut short) is removed, fetched or written again, and the build says
so in its log:

```text
[piceli] cache-corrupt: blob sha256:3c1f… discarded (content hashes to sha256:9a0e…); fetching or writing it again
```

On a build host (`piceli builder serve`) that line is a health event for
the machine, as it is for the cluster's builder node when its build Job
fails (see {doc}`dev_builds` and {doc}`gitops`).

## Building in the cluster

`piceli build job` (and `piceli.artifacts.cluster_build.run_build_job`) runs
the pipeline's host builds as a Kubernetes Job on a labelled builder node
(default selector `piceli.io/builder=true`, `kubernetes.io/arch=amd64`), so a
push can be built without a laptop:

```console
$ piceli build job deploy/app.py:pipeline --commit <sha> --cache-key wp/fix-1 \
    --image registry.example/builder@sha256:… --repo https://git.example/shop.git
$ piceli build job … --approve <plan hash>
```

The first command prints the plan (Job, cache claim, node facts, hash; exit
`3`); `--approve HASH` runs it. The Job fetches exactly `--commit` (the
credentials come from the Secret `--git-secret`, keys `username` and
`password`, and reach Git only through `GIT_ASKPASS`: never a URL, argument
or log line), builds every `--platform` (default `linux/arm64` and
`linux/amd64`; the arm64 image is cross-built with the tools the spec
declares, such as `cargo-zigbuild`), and pushes each image by digest to the
node registry, sending only the blobs it lacks. The builder image is pinned by
digest and holds `piceli` and the declared tools.

The cache lives on a claim `piceli-build-cache-<branch>-<page sizes>`,
created when missing and kept after the Job: one per branch (`--cache-key`)
and per set of page sizes, with a separate target directory per architecture
and page size inside it. Node facts come from the build's declared
`node_facts`, else the target node (API, else the cache above), else the
platform's 4 KiB defaults.

The receipt has the shape of a local host build's (first platform's images in
`outputs.images`) plus `delivered` (per platform and image: repository,
manifest digest, `pull_ref`), `platform_receipts` (the other platforms), and
`job` (name, commit, cache claim, plan hash, seconds); the command writes it to
`--receipt-out` (default `<state_dir>/cluster-builds/<commit>.json`). A failed
or timed-out Job raises `cluster-build-failed` with a scrubbed log tail; the
Job is always removed. A Registry on the node's loopback is reached with
`hostNetwork`, so the builder node must be the registry's node (or set
`--registry-url` to an address the Job can reach).

### A digest built on a laptop

`piceli env push BRANCH MODULE:ATTR --receipt FILE` (or `--digest
IMAGE=sha256:…`, repeated) records the digest for a branch environment in
the ConfigMap `piceli-env-<branch>` of the environment's namespace (keys
`images`, `commit`, `pushed_at`), where `env up` and the controller read it.
It plans first (exit `3`, hash) and needs `--approve HASH`; it pushes no
image.

## Receipts

The build receipt (`piceli.build-receipt.v1`) records `builder: {kind: host,
tools: {name: sha256}}`, `node_facts`, and for each image its config digest
(`image_id`), manifest digest, platform, archive path (relative to the
outputs), every layer (`digest`, `diff_id`, `size`, `origin`: `base` or
`build`) and every file the build put in it (`path`, `sha256`, `size`).

With `[[output.files]]` (0.39.0), `outputs.files` maps each file's path
(relative to the output directory) to its sha256, the shape a Docker
`build.toml` receipt has, and `outputs.file_details` gives, per path,
`output` (the `[[output.files]]` name), `file` (the file name), `platform`,
`sha256` and `size`. A spec without files keeps `outputs.files = {}` and has
no `file_details`.

## SBOM and provenance

Next to each archive the build writes `attestations/<image>.spdx.json`, an
SPDX 2.3 document (the image by manifest digest, its base by digest, every
file the build put in it with its sha256, and every package pinned by a
staged `Cargo.lock`, with its registry checksum and `pkg:cargo` URL; the
lockfiles pin a superset of what one binary links), and
`attestations/<image>.provenance.json`, an in-toto Statement v1 with a SLSA
provenance v1 predicate (subject: the manifest digest; the spec and plan
hashes, commands, `env`, node facts, tool digests, source commits,
contexts and base; the pinned toolchains, and each toolchain download by URL
and sha256). The receipt records both by path and sha256. They hold
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
| `host-toolchain-invalid` | A pin of `build.toolchains` cannot be read (the message names the file) |
| `host-toolchain-checksum` | A toolchain download does not match its published sha256 |
| `host-toolchain-failed` | A pinned tool could not be installed |
| `base-image-unavailable`, `base-image-invalid`, `base-platform-unavailable`, `base-layer-unsupported` | The base image cannot be used |
| `build-builder-mismatch` | The spec was written for the other builder |
| `host-build-path-not-staged` | A command names a path inside a context that its `include` does not stage |
| `derived-base-unavailable`, `derived-base-invalid` | The base of a derived image cannot be read by its digest, or does not match it |
| `derived-base-unrecorded` | A derived image changed, but the record of its unchanged base is gone; the base is not rebuilt silently |
| `derived-image-local-unsupported` | A local `build-spec run` was given a derived image |

`piceli explain <code>` prints the cause and the fix of each.
