# Development builds

Run your build and test commands, or a coding agent's, on the cluster's
builder node instead of your laptop: for a commit, with a warm cache shared
by every run, from one command whose result an agent can read (0.18.0).

```sh
piceli dev run --cluster infra.py:my_cluster -- cargo test -p api --lib
```

Piceli packs your working tree (or a commit, `--ref`) on your machine, uploads it into a fresh pod on
the builder, runs the command there and streams its output back. The pod
builds in a **lineage** of the shared cache: a second run of a sibling
branch recompiles only what changed.

## Declare it

Development builds belong to a cluster declaration. The owner declares the
builder node, the image (Piceli's builder image has cargo, rustup targets,
cargo-zigbuild, zig and node) and the profiles runs may use:

```python
from piceli.infra import Cluster, Controller, DevBuilds, DevProfile, Node

my_cluster = Cluster(
    "my-cluster",
    api="https://10.0.0.1:6443",
    credentials="my-cluster",
    nodes=[Node("builder-1", arch="amd64", roles=["builder", "controller"])],
    controller=Controller(on="builder-1"),
    dev=DevBuilds(
        node="builder-1",
        image="ghcr.io/pynenc/piceli-builder@sha256:<digest>",
        run_cpu="6",
        run_memory="12Gi",
        cache_size="300Gi",
        profiles=[
            DevProfile("rust", tools=["cargo"], prefetch="cargo"),
            DevProfile(
                "cross", tools=["cargo", "cargo-zigbuild", "zig"], prefetch="cargo"
            ),
            DevProfile(
                "node",
                tools=["node", "npm"],
                toolchain=["node", "--version"],
                cpu="2",
                memory="2Gi",
            ),
        ],
    ),
)
```

`piceli cluster init infra.py:my_cluster` plans what it installs, and
applies it with `--approve <plan_hash>`:

- the namespace `piceli-dev` (Pod Security `restricted`);
- the cache claim `piceli-dev-cache` (`cache_size`, the cluster's storage
  class);
- the NetworkPolicy `piceli-dev-runs`: no ingress; egress to DNS and, with
  `network="fetch"` (default), to public addresses on ports 80 and 443
  (crates, toolchains), never to private, link-local or cluster addresses,
  so never to the API server or another workload;
- the ConfigMap `piceli-dev-config` (the declaration);
- with a controller, the Role that lets it manage runs in `piceli-dev`.

The owner's approval of that plan is the approval of every run inside it:
runs need no hash of their own.

`DevBuilds` settings: `node` (a declared node), `image` (pinned by digest),
`profiles` (the first is the default), `slots` (`"auto"` or a number),
`run_cpu` (request; no CPU limit, so an idle builder lends its cores),
`run_memory` (request and limit), `run_storage` (the run's scratch),
`cache_size`, `cache_budget` (0.35.0: the share of `cache_size` the cache
may hold, default `0.8`; see "The shared cache"), `timeout` (a command's
deadline, default `1h`), `network` (`"fetch"` or `"none"`).

`DevProfile` settings: `name`, `image` (default the `DevBuilds` image),
`tools` (checked before the command runs: `dev-tool-missing`), `env`,
`prefetch="cargo"` (`cargo fetch --locked` first, so `--offline` commands
find their crates), `toolchain` (the command whose output keys the cache,
default `rustc --version`), `cpu`, `memory`, `timeout`, `toolchains`
(node, wasm-bindgen and binaryen pins; see below).

## Build hosts

Since 0.20.0 runs may also execute outside the cluster, on hosts the
declaration names: a laptop or a Mac mini running Linux containers, a Linux
server, this machine itself. `hosts` lists them in order of preference;
with `node=`, the in-cluster builder is the first one unless you place
`BuildHost.cluster(...)` yourself, and `node=None` means no in-cluster runs
at all:

```python
from piceli.infra import BuildHost, DevBuilds, DevProfile

dev=DevBuilds(
    node="builder-1",                      # or None: no in-cluster runs
    image="ghcr.io/pynenc/piceli-builder@sha256:<digest>",
    hosts=[
        BuildHost.docker("laptop", slots=2, memory="7Gi"),
        BuildHost.docker("mini", context="ssh://me@mini.example.ts.net", slots=3),
        BuildHost.cluster("builder-1"),    # the in-cluster builder, last
        BuildHost.native("macos"),         # macOS-only builds, no container
    ],
    profiles=[DevProfile("rust", tools=["cargo"], prefetch="cargo")],
)
```

| Kind | Where the run executes | Needs |
| --- | --- | --- |
| `docker` | A Linux container of the profile's image, through a Docker-compatible CLI: Docker Desktop, OrbStack, colima or Podman on a Mac, Docker or Podman on Linux; locally, or over the CLI's own `ssh://` transport (`context=`) or a CLI context name | The engine on the host; nothing else is installed there. The cache is a named volume (`piceli-dev-cache`) |
| `native` | This machine itself, the wrapper as a process (no container) | Python 3 and the tools; its lineages and queue live in `state_dir` (default `~/.cache/piceli/dev/NAME`); it keeps the user's own `CARGO_HOME` and rustup (crates already downloaded, `--offline` works). For builds that only work on the host's OS (macOS), and the fastest test runs |
| `cluster` | A Job on `DevBuilds.node`, as before | `piceli cluster init` |

`piceli dev run` asks the hosts in order and takes the first one that is up
and has a free slot (a docker host whose engine does not answer, or a
cordoned builder node, is skipped); when the chosen host turns out
unavailable at start, it tries the next one. `--host NAME` (or
`PICELI_DEV_HOST`) pins one. `--platform linux/amd64` runs another platform
on a docker host, emulated by its engine (QEMU or Rosetta: several times
slower than native; for Rust prefer a native run that cross-compiles with
`cargo zigbuild`). `piceli dev hosts` shows each host: up or down and why,
its platforms, busy slots and waiting runs.

**Each host keeps its own queue,** so it works while the cluster (or its
GitOps controller) is down, and every client of the host shares it: on a
docker host a slot is a container named `piceli-dev-HOST-N` (the engine
refuses a second container of a name, so taking a slot is atomic, locally
and over SSH) and a waiting run is a small attached container that goes
away with its client; on a native host they are lock files. Waiting runs
start in the order of the cluster's queue: priority (`round`, `agent`,
`normal`), then fair share per requester, then age.

A run on a docker host is the same as in a pod: the same wrapper, deltas,
lineages, corrupted-cache repair and result (`host`: the host's name,
kind and slots). The container runs as the image's user with no
capabilities, `no-new-privileges`, a PID limit and the profile's memory
(or the host's `memory=`); it has no credential: no kubeconfig, no Git or
registry secret is mounted (the source fetch then only reaches public
remotes; private ones travel as bundles from the client).

### Faulty hosts: health events and quarantine (0.22.0)

A machine with bad memory or a failing CPU keeps building, but its builds
fail in odd ways or, worse, pass with a corrupt artifact. When a run on a
docker or native host fails with a sign that points at the machine rather
than the code, or its cache had to be repaired, `piceli dev run` records a
**health event** for that host:

| Kind | Output that counts |
| --- | --- |
| `internal-compiler-error` | `internal compiler error`, `thread 'rustc' panicked` |
| `segfault` | `Segmentation fault`, `SIGSEGV`, `signal: 11` |
| `illegal-instruction` | `Illegal instruction`, `SIGILL`, `signal: 4` |
| `bus-error` | `Bus error`, `SIGBUS`, `signal: 7` |
| `machine-check` | `mce:`, `Machine check`, `Hardware Error` |
| `link-symbol` | `undefined symbol`, `undefined hidden symbol`: only when the cache was repaired (alone it is usually the code's own error) |
| `cache-*` | a repaired cache (`cache-link`, `cache-loader`, ...), or a cached blob whose bytes no longer match their digest (`cache-corrupt`) |

The result says so (`health: {"kind", "line", "repaired"}`) and stderr
names the host. After `quarantine_after` events within `quarantine_window`
(default **2 in 24 hours**) the host is **quarantined**: `piceli dev run`
skips it as if it were down and prints why (`--host NAME` on it is refused
with `dev-host-unavailable`), and `piceli builder serve` on it claims no
GitOps builds. `piceli dev hosts` shows the state and the reason:

```text
mini         docker   up, linux/amd64; 0/3 busy, 0 waiting
             QUARANTINED: 2 health events within 24h (segfault); last 2026-10-18T09:12:40Z: collect2: fatal error: ld terminated with signal 11 [Segmentation fault] (piceli dev release-host mini)
```

Once the machine is fixed (or a memory test found nothing), clear its
events:

```sh
piceli dev release-host mini --cluster infra.py:lab
```

The events live **with the host**, so they hold with the cluster down and
every client of the host sees the same ones: on a docker host one empty,
labelled volume per event (`piceli-health-mini-...`, created and listed
with the engine's CLI, locally or over `ssh://`), on a native host
`health.json` in its `state_dir`. The policy is per host:

```python
BuildHost.docker("mini", context="ssh://me@mini", quarantine_after=3, quarantine_window="12h")
BuildHost.native("macos", quarantine_after=None)   # never quarantined
```

The in-cluster builder node's health is the GitOps controller's: see
{doc}`gitops` (`piceli gitops hosts`, `piceli gitops release-host`).

A host declared `runs=False` (0.25.0) only publishes Nix releases with
`piceli builder serve --publish` (see {doc}`gitops`): `dev run` and
`dev nix build` never pick it unless `--host` names it, and `piceli dev
hosts` shows it as publishing only.

### The toolchain the source pins

When the source has a `rust-toolchain.toml` (or a legacy `rust-toolchain`)
at or above the run's directory, a run in a pod or a docker host uses that
toolchain, not the image's: it is installed once into the cache with
`rustup` (channel, targets, components; under a lock, so concurrent runs
install it once) and selected for the command; the lineage and
`CARGO_HOME` follow it. The result's `toolchain.rust` says which file,
channel, and whether this run installed it (and how long it took). A
native host's own rustup reads the file itself.

```toml
# rust-toolchain.toml
[toolchain]
channel = "1.97.0"
targets = ["wasm32-unknown-unknown"]
components = ["clippy", "rustfmt"]
```

A profile can pin node, wasm-bindgen and binaryen the same way a host build
does ({doc}`host_builds`, "The toolchain the source pins"): paths relative
to the run's tree root, or versions. Each is installed once into the cache
(node and binaryen from their official tarballs, checked against the
published sha256; wasm-bindgen-cli with `cargo install --locked` and the
pinned Rust) and its `bin` directory comes first on the command's `PATH`.
The result's `toolchain` lists each one. Rust always follows the source's
own `rust-toolchain.toml`.

```python
DevProfile(
    "web",
    toolchains={"node": "shop/.nvmrc", "wasm-bindgen": "shop/Cargo.lock", "binaryen": "123"},
)
```

## Run

```sh
# The working tree you are in, as it is on disk: committed, changed and new
# files, never what Git ignores (target/ stays home). The command runs in
# your directory's place in the repository.
piceli dev run --cluster infra.py:my_cluster -- cargo test -p api --lib --offline

# Another working tree.
piceli dev run --cluster infra.py:my_cluster --worktree ../shop-wp-login -- cargo build

# A commit of the repository you are in (branch, tag or sha).
piceli dev run --cluster infra.py:my_cluster --ref wp-login -- cargo test -p api --lib --offline

# A sibling repository at a pinned commit, next to the root as ../shared-lib.
piceli dev run --cluster infra.py:my_cluster --ref HEAD \
  --source shared-lib=../shared-lib@3f2a91c -- cargo build --workspace

# Another profile, a report copied back, the JSON result on stdout.
piceli dev run --cluster infra.py:my_cluster --ref HEAD --profile node \
  --artifact target/report.json --json -- npm test
```

`PICELI_DEV_CLUSTER=infra.py:my_cluster` saves the `--cluster`. A ref
your repository does not have is `dev-ref-unknown`: `git fetch` it first.
A sibling `--source NAME=PATH` without `@REF` ships that working tree too;
a `--source` that is the tree being run is skipped with a note (`notes`).

## What travels: Git deltas

A run ships only what the builder lacks (0.19.0), so it works over a slow
link (a laptop on a VPN, a phone tether):

1. The builder keeps one Git mirror per repository on the cache claim.
   Before anything from the repository runs, the run pod's init container
   fetches the repository's remote into its mirror with the cluster's Git
   credentials (the GitOps controller keeps a copy for runs, the Secret
   `piceli-dev-git`, limited to the composition's source URLs; the
   container that runs your command never mounts it), then reports which
   of a few commits the mirror has: your branch's upstream tip, its merge
   base with the remote's default branch, and ancestors of `HEAD`.
2. Your machine then uploads a `git bundle` of the commits from the
   nearest of those to `HEAD` (none when the mirror has `HEAD`), plus, for
   a working tree, the uncommitted diff (`git diff --binary HEAD`) and the
   untracked files Git does not ignore. A typical agent run uploads a few
   kilobytes. Without any known commit (a new repository, a remote the
   cluster may not fetch) it uploads the whole history once: that seeds
   the mirror, and the next run is a delta.
3. The pod unbundles into the mirror, exports `HEAD`, applies the diff and
   the untracked files, and syncs the tree into its lineage, keeping the
   modification time of unchanged files as before.

Uploads go in 4 MiB chunks over `pods/exec`, each retried on a dropped
connection (after 1, 2, 4, 8 and 16 s), with progress on stderr (`upload
1.2 MB of 4.0 MB (1.1 MB/s)`); the run waits as long as parts keep
arriving. A `dev-upload-failed` names the chunk, how much was sent and the
cause. An image without `git` gets the whole tree, as in 0.18.

Options: `--worktree PATH` (default: here, without `--ref`), `--ref`,
`--repo PATH` (the root repository with `--ref`, default here),
`--name` (its directory name in the run and its lineage's key; default,
since 0.19.0, the repository's main checkout directory from Git's common
dir, so every worktree of a repository builds warm on the same lineages),
`--cwd` (where the command runs inside it), `--source NAME=PATH[@REF]`
(repeat), `--profile`, `--priority round|agent|normal`, `--requester`,
`--artifact GLOB` (repeat; `target/...` is the cargo target directory),
`--artifacts-dir`, `--queue-timeout`, `--unschedulable-seconds` (0.35.0,
default 60), `--quiet` (no streamed log), `--keep` (keep the Job), `--json`.

**Never a silent wait** (0.35.0). A new run says at once what it waits
behind: the queue (`queue: 2 running of 2 slot(s), 1 queued`), the cache
against its budget, and a builder node under disk pressure. While it waits
it repeats its state every 30 seconds. When the scheduler finds no node for
its pod (a taint, disk or memory pressure, not enough CPU) for
`--unschedulable-seconds`, the run fails with `dev-run-unschedulable` and
the scheduler's own reason (`0/3 nodes are available: 1 node(s) had
untolerated taint {node.kubernetes.io/disk-pressure: }`), instead of
waiting for `--queue-timeout`.

## The queue

Runs share `slots` on the builder. With `slots="auto"` (default) there are
as many as the node's allocatable CPU and memory hold runs of `run_cpu` and
`run_memory` (at least one); `slots=N` fixes it.

The GitOps controller runs the queue on a thread (or the owner runs
`piceli dev schedule --cluster MODULE:ATTR` on a cluster without one). While
it is alive, `dev run` creates its Job suspended and waits (`queued #2 of
5`); the queue starts runs into free slots in this order:

1. priority: `round` (integration rounds) before `agent` before `normal`;
2. fair share: within a priority, the requester with the fewest runs going
   (`--requester`, or `PICELI_DEV_REQUESTER`, else your user and host);
3. the oldest first.

It records every finished run (state, exit code, durations, tests, cache
use; the last 20 log lines of one that did not pass) in the ConfigMap
`piceli-dev-status`, keeps the last 50, and deletes the run's Job, which
removes its scratch. Without a queue, runs start at once (Kubernetes still
schedules them by their requests) and `dev run` deletes its own Job.

```sh
piceli dev status --cluster infra.py:my_cluster          # slots, running, queued (#position), recent, cache
piceli dev logs 20261008t071900-ab12 --cluster infra.py:my_cluster --follow
piceli dev cancel 20261008t071900-ab12 --cluster infra.py:my_cluster
```

`dev status --json` prints `piceli.dev-status.v1`: `node`, `slots`,
`scheduler {alive, at}`, `running[]`, `queued[]` (with `position`),
`recent[]`, `cache {used_bytes, max_bytes}` and, while runs are held
(0.38.1), `held {cause, message}`. `dev logs` streams a
running run (`--follow`) or prints the recorded tail of a finished one.
`dev cancel` ends a queued or running run (`dev-run-cancelled`).

## The shared cache

Each run builds in a **lineage**: a stable directory holding the synced
tree and its `CARGO_TARGET_DIR`, one of a few per cache key (the root
source's name, the profile and the toolchain's digest). A run takes the
lineage its ref used last, else the most recently used free one, under an
exclusive lock: two runs never write one target directory. The upload is
synced into the lineage **keeping the modification time of every unchanged
file**, so cargo's own fingerprints rebuild only the crates that changed.
Folders the sync leaves empty are removed too (0.36.0; `target/` and `.git/`
stay), so a cargo glob member such as `crates/app/*` never meets an empty
folder of a crate that is gone; the sync result counts them in
`removed_dirs`.
`CARGO_HOME` (the registry and downloaded crates) is shared per toolchain;
cargo locks it itself.

**The cache's budget** (0.35.0, held during a run since 0.38.1). On k3s
`local-path` storage a claim is a directory on the node's disk: its
declared size is **never enforced**, and a full cache is a node under
`DiskPressure`. So piceli enforces the budget itself, at three moments:

1. **Before a run.** The wrapper measures the whole cache (the lineages,
   the toolchains, cargo's home and the Git mirrors) and evicts free
   lineages, least recently used first, while it holds more than
   `cache_budget` (default `0.8`) of `cache_size`, or while the cache's disk
   has less than 15% free. A lineage in use is counted, never evicted. A
   lineage whose run was killed before it released it, or that never
   recorded its size, is measured again first.
2. **During a run** (0.38.1). The run's lineage has a **cap**: the budget
   less what the rest of the cache held when the run took it. Every 15
   seconds the wrapper reads the cache's disk (cheap); only when the disk
   says the lineage may be over its cap, or the disk is under 15% free, it
   measures the lineage (at most once a minute; every 10 minutes anyway).
   A lineage over its cap, or one that grew by 1 GiB in this run while the
   disk is under the floor, stops the run with `dev-cache-over-cap` and is
   evicted at once. Run again: it builds cold.
3. **After a run** (0.38.1). Every run, passed or not, evicts free
   lineages to the budget again when it ends, so the cache never sits over
   its budget until the next run.

The run says what went (`[piceli-dev] cache budget: evicted 2 lineage(s),
41.3 GiB (over-budget)`). Its result's `cache` carries the start-time check
(`used_bytes`, `max_bytes`, `other_bytes`, `freed_bytes`, `over_budget`,
`fs_free_bytes`, `fs_low`), the lineage's `cap_bytes` and last measured
`lineage_bytes` (with `over_cap` and `stopped_for` when it was stopped), and
`trim`, the end-of-run check (`evicted`, `freed_bytes`, `used_bytes`,
`over_budget`, `fs_free_bytes`, `fs_low`, and `dropped_bytes` for an
evicted over-cap lineage).

While the last run's `trim` is over budget or its disk low, the queue
(0.38.1) starts one run at a time, and only when none is running, so that
run's start-time eviction can trim the cache; while the builder node is
under disk pressure it starts none. `dev status` and a waiting `dev run`
say so (`held`). The controller never creates a Job in `piceli-dev`.

A cache still over its budget raises the controller's `cache-over-budget`
alarm, a node under disk pressure `node-disk-pressure` ({doc}`gitops`). On
a docker or native build host only the budget applies, not the disk floor.

The dev cache never feeds release builds: untrusted development code cannot
reach an image the controller deploys.

### A corrupted cache (0.19.0)

A run killed while writing (a timeout, a cancel, an out-of-memory kill) or
a faulty disk can leave a damaged artifact in a lineage's target, and then
every run on that lineage fails the same way although the code is fine.
When the command fails with a loader error (`error while loading shared
libraries`, `R_X86_64_RELATIVE`, a proc-macro that cannot be loaded),
invalid crate metadata (`E0786`) or a linker reading a bad object
(`undefined hidden symbol`, `file truncated`), the wrapper removes the
blamed crates' artifacts (deps, fingerprints, build script outputs,
incremental state), runs the command once more, and reports it:

```json
"cache": {"lineage": "shop-rust-1a2b3c4d5e/0", "warm": true,
          "repaired": {"kind": "link", "crates": ["geometry"], "entries": 3,
                       "first_error": ["error: linking with `cc` failed: exit status: 1", "…"]}}
```

Only the build's output counts (not the tests'), and only once per run. When
the damage is recognised but no crate is named, `cache.suspect` says so.
To clear by hand:

```sh
piceli dev cache clear --cluster infra.py:lab                      # every free lineage
piceli dev cache clear --cluster infra.py:lab --lineage shop       # every lineage of a root source
piceli dev cache clear --cluster infra.py:lab --lineage shop-rust-1a2b3c4d5e/0 --crate geometry
```

It runs a short Job on the builder (same user and claim as a run, no
Secret, no repository code) that empties the lineages' cargo targets, or
only the named crates' artifacts. `--host NAME` (0.20.0) clears a docker
host's cache volume (a short container, no network) or a native host's
cache directory instead. A lineage a run holds is skipped and
listed under `busy`. The next run on a cleared lineage is cold.
Since 0.35.0 the Job tolerates the `node.kubernetes.io/disk-pressure`
taint: it frees space, so it runs on a node whose disk is full.
Since 0.38.0 it also has the priority class `system-node-critical`:
under DiskPressure the kubelet admits only critical pods, and the toleration
alone got the pod scheduled and then evicted. Kubernetes allows that class
outside `kube-system` unless a ResourceQuota of the namespace limits it; the
Job's pod is then not created and the command says so with the quota's
message. A pod the node refuses or evicts fails the command with the node's
reason (`the cache-clear pod was rejected by the node: Evicted: The node had
condition: [DiskPressure].`, `dev-cache-clear-failed`, exit 1).

A failed run's result also carries `first_error`: the first `error` line
of the output and the 19 lines after it, so the cause survives a long log
whose `log_tail` only shows the end. `piceli dev run` prints it.

## Speed

What a warm run costs, and where it goes: the result's `timeline` has every
phase to the millisecond. Measured on an Apple M3 Pro laptop with a large
Rust workspace (a test build after a one-line change in one crate):

| Where | Warm build |
| --- | --- |
| Native macOS (`BuildHost.native`, macOS binaries) | 16 s |
| Docker Desktop host, image defaults (GNU ld, full debug info) | 44-63 s |
| Same, `CARGO_PROFILE_DEV_DEBUG=line-tables-only` | 34-41 s |

Piceli's own part of a warm run is 4-6 s: the upload is the delta (a few
hundred bytes for a one-line change), the lineage's tree is refreshed in
place (only the files the change touches; unchanged files keep their
mtime), and `cargo fetch` is skipped while `Cargo.lock` does not change.
The rest is the compiler. To make it faster:

- **Tests that only need to pass:** a `native` host first; Linux containers
  only for Linux artifacts.
- **In a Linux container:** less debug info for test builds
  (`env={"CARGO_PROFILE_DEV_DEBUG": "line-tables-only"}` on the profile;
  backtraces keep file and line), and a faster linker
  (`RUSTFLAGS="-C link-arg=-fuse-ld=lld"` with lld in the image, or the
  toolchain's own `rust-lld` through its `gcc-ld` directory).
- **The container VM on a Mac** is the largest remaining factor (rustc's
  incremental front end runs mostly on one thread): give it more memory
  and free disk (a nearly full VM disk slows every build), or use a lighter
  VM (OrbStack). A Linux host over `ssh://` has no VM at all.
- **One lineage per kind of work:** a lineage evicted from the host's page
  cache by another large build makes its next run up to twice as slow;
  `slots` beyond what the host's memory holds do the same.

## Isolation

A run is untrusted code from a repository. Each one is its own pod in
`piceli-dev`, pinned to the builder: user 10001, no capabilities, no
privilege escalation, `RuntimeDefault` seccomp, no service account token,
no Secret, a memory limit, a bounded scratch (an `emptyDir` removed with
the pod) and a deadline; the NetworkPolicy above. A CNI programs a new
pod's NetworkPolicy a moment after its network comes up, so the wrapper
runs nothing from the repository until the API server is unreachable from
the pod; if it still is after a minute (a CNI without NetworkPolicy
support), the run ends as `dev-isolation-not-enforced` without running
anything. The wrapper that runs the
command is standard-library Python carried in the Job itself, so any image
with `python3` works.

## Observe the development cycle

Each finished run is in the UI's **Development builds** page (running and
queued runs with their position, recent results with their phases, tests
and cache use, the cache's size) and, with `Controller(telemetry=Otlp(...))`,
in OpenTelemetry: one trace per run (`DEV <profile> <requester>`) with a
span per phase (`queue`, `sync`, `fetch`, `build`, `test`), an event
`piceli.dev.run.finished`, and the histograms `piceli.dev.run.duration` and
`piceli.dev.queue.wait` (see {doc}`opentelemetry`).

## The result

`--json` prints one object (`piceli.dev-run.v1`):

```json
{
  "schema": "piceli.dev-run.v1",
  "run": "20261008t071900-ab12",
  "state": "failed",
  "exit_code": 101,
  "reason": "dev-command-failed",
  "command": ["cargo", "test", "-p", "api", "--lib"],
  "cwd": "shop/.",
  "profile": "rust",
  "node": "builder-1",
  "requester": "agent-7",
  "priority": "agent",
  "sources": {"shop": {"commit": "3f2a…", "ref": null, "dirty": true, "mode": "delta",
                       "base": "9c1e…", "bundle_bytes": 1840,
                       "fetch": {"fetched": true, "error": null, "note": null}}},
  "upload": {"mode": "delta", "bytes": 2711, "chunks": 1, "retries": 0, "seconds": 0.4},
  "upload_bytes": 2711,
  "durations": {"pack": 0.4, "queue": 2.1, "transfer": 0.9, "sync": 1.2,
                "fetch": 0.3, "build": 41.0, "test": 12.5, "command": 53.5, "total": 59.1},
  "tests": {"passed": 211, "failed": 1, "ignored": 3, "suites": 4},
  "cache": {"lineage": "shop-rust-1a2b3c4d5e/0", "warm": true,
            "crates_compiled": 3, "crates_locked": 412, "hit_ratio": 0.993},
  "log_tail": ["test api::login ... FAILED", "…"],
  "artifacts": []
}
```

- `state`: `passed`, `failed`, `timed-out`, `cancelled` or `error`;
  `reason` is a registered code (`piceli explain CODE`).
- `sources`: each source's `commit` (`HEAD` for a working tree), `dirty`,
  the `base` the mirror had and the `bundle_bytes` sent; `fetch` says
  whether the init container fetched the remote and, when it could not,
  why (`error`, credentials masked) or why it did not try (`note`).
- `upload`: `mode` (`delta`, or `tree` for an image without git), the
  `bytes` sent, `chunks`, `retries` and `seconds`.
- `durations` (seconds): `queue` (until the pod ran), `pack` (on your
  machine, against what the mirror has), `transfer` (the upload), `sync` (into the lineage), `fetch`
  (`prefetch`), `build` and `test` (split at cargo's `Finished` line),
  `command`, `total`.
- `tests`: summed from cargo's `test result:` lines (absent otherwise);
  since 0.20.0 `failed_names` lists the failed tests (`test NAME ...
  FAILED`, at most 100).
- `timeline` (0.20.0): contiguous phases with wall-clock times in seconds
  to the millisecond: `queue`, `pack`, `transfer` (this client), then the
  run's `extract`, `isolation`, `toolchain`, `lineage`, `sync`, `fetch`,
  `build` and `test` (or `command` when cargo printed no `Finished`),
  `repair` and the retried `command` after a corrupted cache, `artifacts`.
  Every second of the run belongs to one phase.
- `host` (0.20.0): the build host it ran on (`name`, `kind`, `slots`, ...);
  `toolchain.rust`: the source's pinned toolchain (`file`, `channel`,
  `targets`, `components`, `installed`, `seconds`).
- `cache`: the lineage, whether it was warm, crates compiled against the
  packages in `Cargo.lock` (`hit_ratio`).
- `log_tail`: the last 80 lines, when the command did not pass.
- `artifacts`: the local paths of copied files.

Exit codes: `0` the command passed; `1` it failed, timed out, ran out of
memory, was cancelled or the run broke; `2` refused before anything ran.

## For coding agents

Development builds are what an agent should compile and test with when the
cluster has them: your laptop stays free, the builder has the cores and the
cache is warm. The owner approved them once (the `DevBuilds` declaration);
you may run as many as your task needs.

```sh
export PICELI_DEV_CLUSTER=infra.py:my_cluster PICELI_DEV_REQUESTER=agent-login
# Your working tree, as it is now: compile and test one crate.
piceli dev run --json --quiet -- cargo test -p api --lib --offline > result.json
echo "exit $?"
jq '{state, exit_code, reason, tests, durations: .durations | {queue, build, test}}' result.json
```

- Read the result, not the log: `state`, `exit_code`, `reason`, `tests`,
  `log_tail` (the last 80 lines when it did not pass). `--quiet` keeps the
  streamed log out of your context; `piceli dev logs RUN` shows more of it.
- Exit `0`: passed. Exit `1`: `dev-command-failed` is your code (fix it,
  run again); `dev-run-out-of-memory` and `dev-run-timed-out` need a smaller
  command or another profile; `dev-upload-failed`, `dev-run-failed` and
  `dev-queue-timeout` may be retried once, then reported. Exit `2`: fix the
  arguments the message names (`piceli explain CODE`).
- Keep the default `--priority agent`; integration rounds use `round`. Give
  each agent its own `PICELI_DEV_REQUESTER`: fair share is per requester.
- A sibling repository your build reads by path goes in with `--source
  NAME=PATH[@REF]`; pin it to the commit your round uses.
- Copy reports back with `--artifact target/nextest/default/junit.xml`
  (`target/...` is the run's `CARGO_TARGET_DIR`).
- Never edit the declaration, the profiles or `piceli-dev` objects, and
  never cancel another requester's run.

## Recipe: an integration round

A coordinator merges the branches agents finished into one worktree, runs
the integration tests once on the builder at the top priority, and reports:

```sh
#!/usr/bin/env bash
# int-round.sh BASE BRANCH...: merge, test on the builder, report.
set -euo pipefail
base=$1; shift
wt=$(mktemp -d)/int
trap 'git worktree remove --force "$wt" 2>/dev/null || true' EXIT
git worktree add -q -b "int/$(date +%F-%H%M%S)" "$wt" "$base"
merged=() ejected=()
for branch in "$@"; do
  if git -C "$wt" merge -q --no-ff --no-edit "$branch" >/dev/null 2>&1; then
    merged+=("$branch")
  else
    git -C "$wt" merge --abort; ejected+=("$branch")
  fi
done
status=0
piceli dev run --worktree "$wt" --priority round --requester coordinator \
  --source shared-lib=../shared-lib@"$(git -C ../shared-lib rev-parse tested)" \
  --artifact target/nextest/default/junit.xml --json --quiet \
  -- ./scripts/int-test.sh > round.json || status=$?
jq -r '"merged: '"${merged[*]:-none}"'; ejected: '"${ejected[*]:-none}"'",
       "tests: \(.state) (exit \(.exit_code)), \(.tests.passed // 0) passed, \(.tests.failed // 0) failed",
       "time: queue \(.durations.queue)s, build \(.durations.build // 0)s, test \(.durations.test // 0)s"' round.json
[ "$status" -eq 0 ] || jq -r '.log_tail[]' round.json
exit "$status"
```

The run builds the merged tree (`--worktree`) in the lineage the last round
used, so only what the merged branches changed recompiles; `--priority
round` puts it ahead of every agent's run, and `round.json` is the report:
its `tests`, `durations` and `log_tail`.

## Nix builds and a signed binary cache (0.24.0)

`piceli dev nix build ATTR` builds a flake output of your source (a NixOS
system, a disk image) as a development run on a build host with Nix, and
with `--publish` pushes it, signed, to a binary cache that hosts substitute
from. Declare a Nix profile and a host that has Nix:

```python
from piceli.infra import Bucket, BuildHost, DevBuilds, DevProfile, Infomaniak, NixCache

dev = DevBuilds(
    node=None,
    image="ghcr.io/pynenc/piceli-builder@sha256:<digest>",
    hosts=[BuildHost.docker("nix-1", context="ssh://builder@nix-1.example.net")],
    profiles=[DevProfile.for_nix(timeout="2h")],
)
cache = NixCache(
    "my-cache-1:<base64 public key>",
    bucket=Bucket("nix-cache", Infomaniak(credentials="infomaniak")),
    write_credentials="cache-s3-write",  # put and delete: builds, gc
)
```

- **Where it runs.** On a **docker** host (a NixOS machine reached with the
  container CLI's own `ssh://`, nothing else installed), a
  `DevProfile(nix=True)` run mounts the host's `/nix` read-only (the store
  and the daemon's socket) and sets `NIX_REMOTE=daemon`: the run image needs
  only `python3` (and `git` to fetch private flake inputs), evaluation
  runs in the container, the builds in the host
  daemon's sandbox, and the store (the cache that makes the next build
  fast) is the host's own. A **native** host uses its own `nix`, and its
  run's out-link keeps the lineage's last build from garbage collection.
  Cluster nodes do not run Nix builds (`dev-nix-host-unsupported`).
- **Keys.** `piceli secrets nix-cache my-cache-1 --generate` stores the
  secret key (`0600`, never printed) and prints the public key
  `my-cache-1:<base64>` for `NixCache(public_key=)` and the hosts'
  `trusted-public-keys` (`--prompt` imports a key from `nix key
  generate-secret`). `piceli secrets s3 cache-s3-write --prompt` stores
  S3 keys that may put and delete objects (two lines on stdin): only
  `--publish` and `gc` use them. Hosts substitute with separate read-only
  keys that live in sops-nix on the host ({doc}`nix_hosts`), never in
  Piceli; the build side never reads them.

`--cluster` (or `--dev`, or `PICELI_DEV_CLUSTER`) names that `DevBuilds`
itself, or a `Cluster` that declares it as `dev=`: Nix builds need no
cluster.

```sh
# A build: the store path, and with --image the image file and its sha256
piceli dev nix build nixosConfigurations.web-1.config.system.build.openstackImage \
  --cluster infra.py:dev --image --name web-1-image --json
# Publish a commit: the plan and its digest first (exit 3), then approve it
piceli dev nix build nixosConfigurations.web-1.config.system.build.toplevel \
  --ref v1.4.0 --publish infra.py:cache
piceli dev nix build ... --approve sha256:<digest>
```

**Private flake inputs** (0.26.0). `--input NAME[=CREDENTIAL]` (repeat)
names an input of `flake.lock` that the build host fetches into its store
before the build, with a `git-token` of this machine's Piceli secrets
(`piceli secrets git-token NAME --host github.com --prompt`; default name
`git`); with `--publish`, the composition's `NixRelease` of that output
adds its own (its gate's input, its `inputs=`, the inputs that are its
`Source`s), and the plan lists them all. 0.26.2: the composition is
`--composition`, else the `--publish` module, else the `--dev` module; a
build without `--publish` of another output of the flake (an `--image`)
fetches what every `NixRelease` there covers, as the release path does. The build never sees the token; an
input that cannot be fetched refuses before the build
(`infra-nix-input-unreachable`, naming the input, its URL and the
credential). See {doc}`nix_hosts` (private flake inputs).

```sh
piceli dev nix build nixosConfigurations.web-1.config.system.build.toplevel \
  --ref v1.4.0 --publish infra.py:cache --input app=git
```

The approved command builds the commit as any run: isolated, with no key
and no cloud credential. Then, **on the same host**, a separate short step
that runs no project code receives the secret key and the S3 keys on its
stdin, writes the key to a `0600` file in a private temporary directory
(removed on every exit), and runs `nix copy --to 's3://BUCKET?endpoint=...&
secret-key=FILE' PATH`: each uploaded path's narinfo is signed by the key,
so the host's store is never changed and its daemon need not trust the
user. Last, Piceli signs and uploads the **build manifest**
`piceli/NAME/COMMIT.json` (`schema: piceli.nix-build.v1`: `commit`,
`tag`, `attribute`, `store_path`, `system`, `nar_hash`, `nar_size`,
`closure_size` from `nix path-info -S`, `built_at`, `host`, `image`,
`signed_by`, and `signature`: the cache key's Ed25519 signature over the
canonical JSON of the rest). The result
(`piceli.dev-nix-build.v1`) carries the store path, `image.file`,
`image.sha256` and `image.build`, the cache URL, the public key and the
manifest; never a secret. With `--image`, the build also writes
`ARTIFACTS_DIR/NAME.image.json` (`piceli.nix-image.v1`: the image's path
relative to it, `sha256`, `format`, `bytes`, `store_path`, `commit`), which
`Image.from_build("my-image", "piceli-dev-artifacts/web-1-image.image.json")`
reads: nobody copies a checksum by hand (`infra-image-build-invalid` when
it is missing or malformed). 0.25.3: built on a docker host over SSH, the
sidecar also names that host (`built_on`: its name, context, CLI and run
image) and the image's file in its store (`store_file`), and `piceli infra
apply` uploads the image from there, not from this machine
({ref}`image uploads <image-uploads>`). The host's Nix store must still hold it: a garbage
collection in between fails the upload with `infra-image-unavailable`
before anything is created (build again, or `upload="local"`). 0.26.2: on
a docker host with `BuildHost.docker(roots_dir=...)` (see {doc}`nix_hosts`)
the build keeps the image's GC root `ROOTS_DIR/images/NAME` there until the
next build of the same `--name`, so a collection cannot take it.

**Ref pointers.** After the manifest, publishing writes a signed pointer for
the tag at the commit and for the branch `--ref` names (or `--branch NAME`):
`piceli/NAME/refs/tags/TAG.json` and `piceli/NAME/refs/heads/BRANCH.json`
(`schema: piceli.nix-ref.v1`: `ref`, `commit`, `manifest`, the manifest's
`manifest_sha256`, `store_path`, `system`, `published_at` and the
signature). `published_at` only grows, even when the publishing clock is
behind. The plan lists them. When one commit carries tags of several hosts
(`web-v1.4.0`, `api-v2.0.0`), `--tag-pattern 'web-v*'` (a `NixHost`'s
`Tag` pattern) names the tag the pointer gets: `--ref` when it is a version
tag of that pattern (another tag is refused, `dev-nix-invalid`), else the
highest version at the commit after the pattern's prefix (see
{doc}`nix_hosts`). Without it, the pointer gets `--ref` when it is a tag,
else the first tag at the commit. A host resolves its ref from them with read
access to the cache alone, no Git: see {doc}`nix_hosts`.

A host that follows a tag reads the manifest of the tag's commit and
trusts it only when `piceli.dev.nix_verify.verify_manifest(body,
public_key)` holds, and the store path's narinfo in the cache verifies with
the same key (`verify_narinfo`) with the manifest's `NarHash`: an attacker
with write access to the bucket can neither point a commit at another
closure nor forge one. `piceli.dev.nix_verify` is standard library only
(Ed25519 as in RFC 8032): a host needs neither a crypto package nor `nix`
to check.

**Retention.** `piceli dev nix gc --cache infra.py:cache` keeps the
manifests of the newest five tags (`--keep-tags`) and of the current
`main` (`--main`), every store path their closures reach (a kept closure
that shares a path with an old build keeps it), and every object younger
than `--grace` (an hour: a publish in progress); it lists the manifests,
narinfos and NAR files it would delete with a digest (exit 3) and deletes
them only with `--approve <digest>`, manifests first. Branch pointers stay
and keep the build they name; tag pointers of tags it does not keep go
first, then their manifests, so a host never finds a pointer whose build is
gone. When the repository releases several hosts by tag prefix, give each
host's pattern, `--tag-pattern 'web-v*' --tag-pattern 'api-v*'`: gc then
keeps the newest `--keep-tags` versions of each pattern (compared after its
prefix) instead of the newest tags by date, so one host's releases never
push another's out. The plan lists them under `tag_patterns`.

## Failure codes

| Code | Meaning | What to do |
| --- | --- | --- |
| `dev-command-failed` | The command exited non-zero | Read `first_error` and `log_tail`, fix, run again; with `cache.suspect`, `piceli dev cache clear --lineage NAME` |
| `dev-cache-clear-failed` | `dev cache clear`'s Job ended without a result | Check its pod in `piceli-dev`; run it again |
| `dev-run-timed-out` | Over the profile's `timeout` (process group killed) | Split the command or find what hangs |
| `dev-cache-over-cap` | The run's lineage grew past its cap (the budget less the rest of the cache), or grew while the cache's disk was under 15% free; the run was stopped and the lineage evicted | Run again (cold); if it repeats, raise `cache_size` or `cache_budget` |
| `dev-run-out-of-memory` | Killed at the memory limit | Lower `CARGO_BUILD_JOBS` or ask for a bigger profile |
| `dev-run-failed` | The pod ended without a result or could not start | Check the image; report it |
| `dev-run-cancelled` | The Job was deleted (Ctrl-C) | Run again when wanted |
| `dev-queue-timeout` | No start within `--queue-timeout` | Wait or raise it |
| `dev-run-unschedulable` | The run's pod found no node for `--unschedulable-seconds`; the message has the scheduler's reason | Under disk pressure, `piceli dev cache clear`; else free capacity on the node |
| `dev-upload-failed` | The upload did not arrive intact; the message names the chunk and the cause (a dropped link after five retries, a bundle that does not apply, a commit the mirror lacks) | Run again; check `pods/exec` rights and the link |
| `dev-isolation-not-enforced` | The cluster does not enforce the NetworkPolicy; nothing ran | Report it to the owner |
| `dev-tool-missing` | The image lacks a profile tool | Use another profile |
| `dev-ref-unknown` | The commit is not in your repository | `git fetch` it |
| `dev-source-invalid` | A source is not `NAME=PATH@REF` in a Git repository | Fix the argument |
| `dev-profile-unknown` | No such profile | Use a declared one |
| `dev-run-invalid` | No command, a bad priority, `--cwd` or `--node` | Fix the argument |
| `dev-host-unavailable` | No declared build host could take the run (engine down, ssh unreachable, builder cordoned); the message names each | `piceli dev hosts`; start the engine or pass `--host` |
| `dev-host-unknown` | `--host` names an undeclared host | Use one from `piceli dev hosts` |
| `dev-toolchain-invalid` | The source's `rust-toolchain.toml` or a profile's pin cannot be read | Fix the file the message names |
| `dev-toolchain-failed` | rustup (or a profile's tool) could not be installed | Check channel, targets, components or the version; runs need public HTTPS once |
| `dev-toolchain-checksum` | A profile's toolchain download does not match its published sha256 | Run again; check the network path |
| `dev-run-unknown` | No such run queued, running or recorded | `piceli dev status` |
| `dev-not-enabled` | No `DevBuilds` installed on the cluster | Ask the owner to declare it and run `cluster init` |
| `dev-nix-*` (0.24.0) | A Nix build or publish was refused or failed (`dev-nix-invalid`, `-host-unsupported`, `-cache-invalid`, `-key-invalid`, `-key-mismatch`, `-plan-changed`, `-no-output`, `-publish-failed`, `-manifest-failed`, `-gc-failed`) | `piceli explain CODE` |
