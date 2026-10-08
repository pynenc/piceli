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
            DevProfile("cross", tools=["cargo", "cargo-zigbuild", "zig"], prefetch="cargo"),
            DevProfile("node", tools=["node", "npm"], toolchain=["node", "--version"],
                       cpu="2", memory="2Gi"),
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
`cache_size`, `timeout` (a command's deadline, default `1h`), `network`
(`"fetch"` or `"none"`).

`DevProfile` settings: `name`, `image` (default the `DevBuilds` image),
`tools` (checked before the command runs: `dev-tool-missing`), `env`,
`prefetch="cargo"` (`cargo fetch --locked` first, so `--offline` commands
find their crates), `toolchain` (the command whose output keys the cache,
default `rustc --version`), `cpu`, `memory`, `timeout`.

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

`PICELI_DEV_CLUSTER=infra.py:my_cluster` saves the `--cluster`.
Everything comes from your machine: a working tree is its tracked files as
they are now plus the untracked files Git does not ignore (`git ls-files
--cached --others --exclude-standard`; deleted files are left out), a
commit is `git archive` of it. The cluster never fetches and holds no Git
credentials. A ref your repository does not have is `dev-ref-unknown`:
`git fetch` it first. A sibling `--source NAME=PATH` without `@REF` ships
that working tree too. The result's `sources` records each source's
commit (`HEAD` for a working tree) and whether it was `dirty`.

Options: `--worktree PATH` (default: here, without `--ref`), `--ref`,
`--repo PATH` (the root repository with `--ref`, default here),
`--name` (its directory name in the run, default the repository's),
`--cwd` (where the command runs inside it), `--source NAME=PATH[@REF]`
(repeat), `--profile`, `--priority round|agent|normal`, `--requester`,
`--artifact GLOB` (repeat; `target/...` is the cargo target directory),
`--artifacts-dir`, `--queue-timeout`, `--quiet` (no streamed log), `--keep`
(keep the Job), `--json`.

## The shared cache

Each run builds in a **lineage**: a stable directory holding the synced
tree and its `CARGO_TARGET_DIR`, one of a few per cache key (the root
source's name, the profile and the toolchain's digest). A run takes the
lineage its ref used last, else the most recently used free one, under an
exclusive lock: two runs never write one target directory. The upload is
synced into the lineage **keeping the modification time of every unchanged
file**, so cargo's own fingerprints rebuild only the crates that changed.
`CARGO_HOME` (the registry and downloaded crates) is shared per toolchain;
cargo locks it itself.

When the lineages pass `cache_size`, free ones are evicted, least recently
used first; one in use never is. The dev cache never feeds release builds:
untrusted development code cannot reach an image the controller deploys.

## Isolation

A run is untrusted code from a repository. Each one is its own pod in
`piceli-dev`, pinned to the builder: user 10001, no capabilities, no
privilege escalation, `RuntimeDefault` seccomp, no service account token,
no Secret, a memory limit, a bounded scratch (an `emptyDir` removed with
the pod) and a deadline; the NetworkPolicy above. The wrapper that runs the
command is standard-library Python carried in the Job itself, so any image
with `python3` works.

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
  "sources": {"shop": {"commit": "3f2a…", "ref": "wp-login", "dirty": false}},
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
- `durations` (seconds): `pack` (on your machine), `queue` (until the pod
  ran), `transfer` (the upload), `sync` (into the lineage), `fetch`
  (`prefetch`), `build` and `test` (split at cargo's `Finished` line),
  `command`, `total`.
- `tests`: summed from cargo's `test result:` lines (absent otherwise).
- `cache`: the lineage, whether it was warm, crates compiled against the
  packages in `Cargo.lock` (`hit_ratio`).
- `log_tail`: the last 80 lines, when the command did not pass.
- `artifacts`: the local paths of copied files.

Exit codes: `0` the command passed; `1` it failed, timed out, ran out of
memory, was cancelled or the run broke; `2` refused before anything ran.

## Failure codes

| Code | Meaning | What to do |
| --- | --- | --- |
| `dev-command-failed` | The command exited non-zero | Read `log_tail`, fix, run again |
| `dev-run-timed-out` | Over the profile's `timeout` (process group killed) | Split the command or find what hangs |
| `dev-run-out-of-memory` | Killed at the memory limit | Lower `CARGO_BUILD_JOBS` or ask for a bigger profile |
| `dev-run-failed` | The pod ended without a result or could not start | Check the image; report it |
| `dev-run-cancelled` | The Job was deleted (Ctrl-C) | Run again when wanted |
| `dev-queue-timeout` | No start within `--queue-timeout` | Wait or raise it |
| `dev-upload-failed` | The archive did not arrive intact | Run again; check `pods/exec` rights |
| `dev-tool-missing` | The image lacks a profile tool | Use another profile |
| `dev-ref-unknown` | The commit is not in your repository | `git fetch` it |
| `dev-source-invalid` | A source is not `NAME=PATH@REF` in a Git repository | Fix the argument |
| `dev-profile-unknown` | No such profile | Use a declared one |
| `dev-run-invalid` | No command, a bad priority, `--cwd` or `--node` | Fix the argument |
| `dev-not-enabled` | No `DevBuilds` installed on the cluster | Ask the owner to declare it and run `cluster init` |
