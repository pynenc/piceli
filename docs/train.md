# Merge train

A merge train lets finished branches join the base branch only after their
merge has passed the project's checks. You name the branches; Piceli
merges them onto the train's head, runs the steps in the merged tree, and
either joins each branch or ejects it with the step that failed. Nobody
merges by hand, and the base only ever moves to a commit whose tree passed.

Maturity: **preview** (0.26.0). The spec API and the result object may
change before 1.0.

## Concepts

- **Base**: the branch candidates join (`main`). It moves by fast-forward
  only, and only with `--promote`.
- **Tested**: a branch marking the last head the train tested and promoted
  (`tested`). Deploys that want "the newest tested commit" follow it.
- **Head**: the train's current commit. A run starts at the base; each
  candidate set that passes becomes the new head.
- **Candidate**: a branch named on the command line. Its tip is pinned when
  the run starts, so a commit pushed during the run is not taken.
- **Trial**: one set of candidates merged onto the head and judged. Its
  steps run in a worktree of the merge (at a stable path under the train's
  cache, for warm builds), removed afterwards.
- **Verdict**: `joined`, `ejected`, or `included` (the branch was already in
  the base).

## The spec

A train is declared in Python, in a module of the repository it tests (the
app's composition module is a good home):

```python
import sys

from piceli.train import CargoCheck, CargoTests, Impact, Step, Train

train = Train(
    "shop",
    base="main",
    tested="tested",
    tag="tested/{date}.{n}",
    remote="origin",
    steps=[
        CargoCheck(),
        CargoTests(select=Impact(roots=("Cargo.toml", "Cargo.lock", "rust-toolchain.toml"))),
        Step.command("fitness", ["scripts/fitness.sh"]),
        Step.command("agent-docs", [sys.executable, "scripts/check_agent_docs.py"]),
        Step.command("branch-check", ["scripts/branch_check.sh"], timeout=1800),
        Step.publish("prebuilt", ["scripts/publish_prebuilt.sh"]),
    ],
)
```

`Train(name, *, steps, repo=".", base="main", tested="tested",
tag="tested/{date}.{n}", remote="origin", builds=None, hosts=("local",),
local_slots=1, fallback="local", target_dir="auto", sccache="auto",
parallel="auto", cache=None, retention=Retention(), telemetry=None,
notify=())`:

- `name` is a DNS label. It names the run history and the merge commits.
- `repo` is the Git repository, relative to the module's directory.
- `tag` names a promoted head: `{date}` is the UTC day (`YYYY-MM-DD`),
  `{n}` one more than the highest number used that day (required), `{name}`
  the train's name.
- `steps` run in order. A train needs at least one test step: a branch
  never joins untested.
- `builds`, `hosts`, `local_slots`, `fallback`: where steps run (see
  [Where steps run](#where-steps-run)).
- `target_dir`, `sccache`: warm cargo builds here (see
  [Warm local builds](#warm-local-builds)).
- `parallel`: `"auto"`, `"on"` or `"off"` (see [Parallel halves](#parallel-halves)).
- `cache`: where slot locks, shared target directories, trial worktrees
  and the content store live (default `$XDG_CACHE_HOME/piceli/train`,
  else `~/.cache/piceli/train`).
- `telemetry` exports each run as an OpenTelemetry trace
  ({ref}`train-telemetry`); `notify` sends a notification when a run ends
  ({ref}`train-notify`). Both come from the spec you run, never from a
  merged tree.

The train's identity, where it runs and how it is observed come from the
spec you run; the steps come from the commit under test.

The steps:

| Step | Runs | Passes when |
| --- | --- | --- |
| `Step.command(name, argv, env=None, timeout=3600, cargo=False)` | `argv` in the merged checkout | it exits 0 |
| `CargoCheck(select=Impact(), args=("--all-targets",))` | `cargo check` on the selected packages | it exits 0 (skipped when nothing selected) |
| `CargoTests(select=Impact(), args=(), retry=1)` | `cargo test --no-fail-fast` on the selected packages, then the failed tests again by exact name | every test passed, first time or on a retry |
| `Step.publish(name, argv, env=None, timeout=3600, cargo=False)` | `argv` in a checkout of the promoted head, only with `--promote` | it exits 0; it never judges a candidate |
| `Step.images("infra.py", components=(), platforms=())` | builds the GitOps controller's images of the promoted head and offers them to it, only with `--promote` | the images were built, pushed and offered (see [Images for the cluster](#images-for-the-cluster)) |

Every step also takes `hosts=` (where it may run, in order; empty: the
train's `hosts`), `inputs=` and `outputs=` (see
[Content-hash reuse](#content-hash-reuse)). `cargo=True` gives a command
the shared cargo target directory and compiler cache, as the cargo steps
get them.

Every step gets the caller's environment, its own `env`, and:
`PICELI_TRAIN` (the train's name), `PICELI_TRAIN_BASE` (the head the
candidates were merged onto), `PICELI_TRAIN_HEAD` (the commit under test)
and `PICELI_TRAIN_BRANCHES` (the candidates in this trial, space
separated). A publish step also gets `PICELI_TRAIN_TAG`. A step that runs
past its `timeout` is stopped with its process group (verdict `timeout`).

Why steps are data (an argv and an environment) rather than Python
callbacks: the spec is read from each merged tree in a separate process and
must cross that boundary as JSON, and a later runner can ship the same step
to a build host unchanged. Anything a step needs to decide (which
browser check a branch has, which rules apply) belongs in the script it
runs, which is read from the merged tree too.

### Impact selection

`Impact()` selects the cargo packages a change can affect, from `cargo
metadata --no-deps` read in the merged tree and the paths that differ
between the head and the commit under test:

- every workspace member with a changed file;
- every member that depends on a selected one (normal and build
  dependencies, transitively);
- every member with a dev-dependency on a selected one (its tests link it);
- every package when a changed path matches one of `roots` (globs,
  relative to the repository root). The default roots are `Cargo.toml`,
  `Cargo.lock`, `rust-toolchain`, `rust-toolchain.toml` and
  `.cargo/config[.toml]`.

Files outside every package that match no root select nothing: other steps
check scripts and documentation. Add a glob to `roots` for a file a build
reads from outside its package. `select="all"` always tests the whole
workspace (`--workspace`).

`Impact(dev_targets=True)` narrows the members that reach a selected
package **only** through a dev-dependency: their library did not change,
only tests that use the changed one can break. Such a member runs only its
integration test targets (`tests/*.rs`) whose source names the selected
library (by the name code uses, `my_core`), as `cargo test -p member
--test target`. When any other Rust file of the member names it (its unit
tests, a module its tests share), the whole member is tested: a doubt
tests more. `cargo check` checks the whole member either way.

### Flaky tests

`CargoTests` runs every test binary (`--no-fail-fast`). When tests fail, it
reads cargo's output for each failed target (the line ``to rerun pass
`-p NAME --lib` ``) and the names of its failed tests, and runs only those again:
`cargo test -p NAME --lib -- --exact NAME...`. A test that passes on the
retry does not eject the branch; it is recorded as **flaky** with its first
failure's output. A target that failed without naming a test (a crash, a
build error) is not retried.

## The command

```sh
piceli train shop/train_spec.py:train --branches feat-a feat-b fix-c --json
piceli train shop/train_spec.py --branches feat-a feat-b --promote
piceli train shop/train_spec.py --branches feat-a --promote --push
```

`APP` is `MODULE[:ATTR]` or `path/to/file.py[:ATTR]`; `ATTR` may be left
out when the module declares one `Train`. `--branches` is optional sugar:
the branch names may follow `APP` directly.

| Option | Effect |
| --- | --- |
| `--promote` | Tag the head (`tested/<date>.N`), move `tested` and the base to it by fast-forward only, then run the publish steps |
| `--push` | Push the tag, `tested` and the base, atomically (needs `--promote`) |
| `--queue` | When the train is busy, queue the request (with its flags) and return at once; see {ref}`train-queue` |
| `--json` | Print the result object on stdout |
| `--state-dir DIR` | The state root (default `$XDG_STATE_HOME/piceli/train`, else `~/.local/state/piceli/train`) |
| `--promote-run RUN` | Promote the head an earlier run reached (its id, or `last`) without running it again; takes `--push`, no branches (see [Keep the tested head](#keep-the-tested-head)) |

Without `--promote` no ref moves: the result names the head the train
reached. Nothing is pushed without `--push`.

Exit codes: `0` every branch joined (or was already in) and the promotion
and push asked for succeeded; `1` a branch was ejected, the promotion was
refused (`train-not-fast-forward`, `train-checkout-dirty`), a publish step
failed (`train-publish-failed`) or the push failed (`train-push-failed`);
`2` rejected before anything ran (`train-not-found`, `train-branch-unknown`,
`train-push-needs-promote`, `train-busy`, `train-run-unknown`, …). A step
no host could take stops the run (`train-host-unavailable`, exit `1`).

## How a branch is judged

1. **Merge.** Each candidate is merged with `git merge --no-ff` onto the
   train's current head (the base plus the candidates already joined), in
   a temporary worktree, without hooks or signing. A conflict is never
   resolved: a branch that conflicts with the head is ejected with
   `train-merge-conflict` and the conflicting files.
2. **Its own rules.** The spec module is loaded from the merged tree, in a
   fresh Python process. So the steps, the scripts they run and the files
   those read (allowlists, rules) are the merged commit's: the base's newer
   rule applies to an old branch, and a branch's own change to an
   allowlist applies to it. The train's identity (name, base, tested, tag,
   remote) comes from the spec you ran. A merged tree whose spec module is
   missing or fails to load ejects the branch with `train-spec-invalid`.
3. **Steps.** The test steps run in order; the first that fails ends the
   trial (`train-step-failed`, or `train-step-timeout`).
4. **Joint run, then bisection.** The first trial merges every candidate.
   When it passes, all join. When it fails, the set is split in halves: the
   first half is tried on the head and its passing members join; then the
   second half is tried on the new head. Each half that fails is split
   again, down to single branches, which are ejected. A branch that only
   conflicted with an ejected one merges cleanly once that one is out, and
   joins. Joined branches keep the order given. The halves may run at the
   same time ([Parallel halves](#parallel-halves)); the verdicts do not
   change.
5. **Promote.** With `--promote`, after every candidate is judged: the tag
   is created at the head, `tested` and the base move to it (with `git
   merge --ff-only` in a worktree that has them checked out and clean, else
   by `git update-ref` with the expected old value), and the publish steps
   run in a checkout of the head (read from its spec). A base or `tested`
   that moved outside the train is never overwritten: nothing is tagged or
   moved (`train-not-fast-forward`).

Merges, not rebases: the branch's own commits join unchanged, so `git
branch --merged` and the tag show exactly which commits joined; branches
with merge commits of their own need no special case; and each merge commit
records which branch joined, by which train.

## The result

`--json` prints one `piceli.train.v1` object:

```json
{
  "schema": "piceli.train.v1",
  "state": "ejected",
  "ok": false,
  "train": "shop",
  "run": "20261023T101500Z-3f2a1c",
  "start": "<base commit>",
  "head": "<the train's head>",
  "started": "2026-10-23T10:15:00.000000Z",
  "duration": 412.7,
  "host": "local",
  "branches": [
    {
      "branch": "feat-a",
      "tip": "<commit>",
      "verdict": "joined",
      "trial": 3,
      "steps": [
        {"name": "merge", "kind": "merge", "verdict": "passed", "started": "…", "duration": 0.08, "host": "local", "log": null},
        {"name": "spec", "kind": "spec", "verdict": "passed", "detail": {"source": "merged-tree", "steps": ["cargo-check", "cargo-test", "fitness"]}, "…": "…"},
        {"name": "cargo-test", "kind": "cargo-test", "verdict": "passed", "duration": 81.2, "host": "local", "log": "…/runs/<run>/03-cargo-test.log", "detail": {"selection": ["api", "core"]}}
      ]
    },
    {
      "branch": "fix-c",
      "verdict": "ejected",
      "reason": "train-step-failed",
      "step": "fitness",
      "tail": "<the last lines of the step's output>",
      "trial": 5,
      "steps": ["…"]
    }
  ],
  "trials": [{"id": 1, "base": "…", "head": "…", "branches": ["feat-a", "fix-c"], "verdict": "failed", "step": "fitness", "steps": ["…"]}],
  "flaky": [{"key": "-p api --test http retries_once", "test": "retries_once", "target": "-p api --test http", "count": 3, "first_seen": "…", "last_seen": "…", "last_tail": "…"}],
  "tags": ["tested/2026-10-23.1"],
  "promote": {"state": "promoted", "tag": "tested/2026-10-23.1", "head": "…", "publish": ["…"]},
  "push": {"state": "not-requested"},
  "record": "…/runs/<run>/run.json"
}
```

Every step has its start (UTC), duration, the host it ran on (`local` or a
build host's name), verdict (`passed`, `failed`, `timeout`, `skipped`,
`reused`) and log path; its `detail` says how it was placed (`placement`:
hosts skipped, a fallback, the cargo target directory) and, with inputs,
its `key` and where a reused result came from (`reused`: run, trial, host,
time). A speculative trial says `"speculative": true`, and `"discarded":
true` when its base was not reached. `promote.state` is `not-requested`,
`nothing-joined`, `promoted`, `rejected` (with `reason`) or
`publish-failed`; `push.state` is `not-requested`, `pushed` or `failed`.
`last` names `refs/piceli/train/<name>/last` and the head it points at; a
`--promote-run` result names the run it promoted (`promoted_from`).

## State and history

Each train keeps, under its state directory
(`$XDG_STATE_HOME/piceli/train/<name>`):

- `runs/<run>/run.json`: the run's result, rewritten after each trial (a
  run that was killed leaves its last state, `"state": "running"`);
- `runs/<run>/<trial>-<step>.log`: every step's output (a step on a build
  host: the dev run's output and progress);
- `runs/<run>/events.jsonl`: append-only, every event of the run as it
  happens (what `piceli trains` reads for the step in progress);
- `flaky.jsonl`: append-only, one line per flaky test per run that saw it,
  with its running totals (`count`, `first_seen`, `last_seen`,
  `last_tail`); the last line of a test is its current state;
- `lock`: held while a run is active; a second run is refused with
  `train-busy`. While held, it names the run and the process holding it,
  so status views read it without taking it.

## Where steps run

A train runs its steps on this machine (`local`) and on the build hosts of
`Train(builds=...)`: the `BuildHost` declarations `piceli dev run` uses
(see {doc}`dev_builds`), as a `DevBuilds`, or `MODULE:ATTR` of one or of a
`Cluster` with `dev=` (read with the spec). A laptop and a build server:

```python
import sys

from piceli.infra import BuildHost, DevBuilds, DevProfile
from piceli.train import CargoCheck, CargoTests, Impact, Step, Train

builds = DevBuilds(
    node=None,                             # no in-cluster runs
    image="ghcr.io/pynenc/piceli-builder@sha256:<digest>",
    hosts=[BuildHost.docker("server", context="ssh://me@server.example.ts.net", slots=3)],
    profiles=[DevProfile("rust", tools=["cargo"], prefetch="cargo")],
)

train = Train(
    "shop",
    builds=builds,
    hosts=["server", "local"],           # the server first, the laptop next
    local_slots=1,                       # the laptop runs one command at a time
    steps=[
        CargoCheck(),
        CargoTests(select=Impact(dev_targets=True)),
        Step.command("fitness", [sys.executable, "scripts/fitness.py"], hosts=["local"]),
        Step.command(
            "stack",
            ["scripts/build_stack.sh"],
            cargo=True,
            inputs=["crates/**", "Cargo.toml", "Cargo.lock", "scripts/build_stack.sh"],
            outputs=["dist/stack.tar.zst"],
        ),
        Step.publish("prebuilt", ["scripts/publish_prebuilt.sh"], hosts=["local"]),
    ],
)
```

Each command goes to the first of its hosts that is up (not quarantined)
with a free slot, else to the first that is up (it queues there; a build
host's queue is shared by every client of the host). A host down when
asked is skipped; a host that turns out unavailable when the command
starts hands it to the next, as `piceli dev run` does. When every host
named is down, `fallback="local"` (the default) runs the command here;
`fallback=None` stops the run (`train-host-unavailable`). `cargo metadata`
(for the impact selection) is always read here.

A command on a build host is a `piceli dev run` of the merged checkout,
with the profile `DevBuilds` lists first: the upload is a delta of what
the host's mirrors lack, the run takes one of the host's slots, a health
signature records an event (and may quarantine the host), and cargo builds
in the profile's lineage of the host's warm cache, the target directory
every run of that repository shares there. Only the `PICELI_TRAIN_*`
variables and the step's own `env` travel with it, never the caller's
environment. The step's `outputs` come back as the run's artifacts.

## Warm local builds

Every trial is a fresh worktree, which would make every cargo build start
cold. Instead, here:

- each command takes one of the machine's `local_slots` (a lock file under
  the cache, shared by every train on the machine and released by the
  operating system when a run dies), so the laptop keeps its limit;
- trials use a few **stable worktree paths** (`checkouts/<train>/<repo>/N`
  under the cache, held while a trial runs, removed after it), each with
  **its own target directory** (`targets/<train>/<repo>/N`): cargo
  fingerprints a member by its absolute path;
- every file whose content (Git blob) is the same as at that path's
  previous trial gets that trial's **modification time** back, while a
  changed file keeps the new time of its checkout: cargo compares source
  times with its last build, so it rebuilds exactly what changed;
- `sccache` is the compiler wrapper when it is on `PATH`
  (`sccache="auto"`; `True` always, `False` never; a `RUSTC_WRAPPER` you
  set wins).

`target_dir="auto"` is the default; a path shares one directory (cargo's
own lock serialises its users); `None` gives each trial its checkout's
`target/`. A `CARGO_TARGET_DIR` in the environment wins over `"auto"`. The
cargo steps get it; a command gets it with `cargo=True`. An output under
`target/` is copied from the shared directory into the checkout after the
command.

## Content-hash reuse

A step that declares `inputs=` is keyed by the SHA-256 of its definition
(kind, argv or cargo arguments, its own `env`, a cargo step's impact
selection; never its host, timeout or the per-trial `PICELI_TRAIN_*`
variables), the Git blob ids of the tracked files its inputs match
(`fnmatch` globs from the repository root; `*` crosses `/`), and the
toolchain pins (`rust-toolchain`, `rust-toolchain.toml`).

When the step passes, the key is recorded in the content store
(`store/` under the cache) with its run, trial, host and head, and each of
its `outputs` (files or directories) kept once by digest. A later step
with the same key (another trial of the run, which is how it survives
bisection, a later run, another train on the machine) is **reused**: it
does not run, its outputs are restored into the checkout, and its result
names where they came from. Failures are never recorded: a failing or
flaky step runs again. A step that declared outputs and did not make one
is not recorded.

On `--promote`, the publish checkout gets the outputs of every tested step
that declared them, restored from the store: the binaries a publish step
ships, or the stack it uploads, are the tested ones, built once per input
hash.

## Parallel halves

After a failed trial, the set is split in halves. With
`parallel="auto"` (the default) and two free slots over the train's hosts
(`"on"`: whenever it has two; `"off"`: never), the left half's merge is
made first; the left half is judged on it while the right half is judged,
at the same time, **on top of it**, speculating that the whole left half
joins. When it does, the train's head is exactly that merge, and the right
half's trial stands. When it does not (some of it is ejected), the
speculative trial is marked `discarded` and the right half is judged again
on the head the left half reached. Either way the verdicts are those of
the sequential order. The two trials prefer different hosts.

## Keep the tested head

Every run that joined something points `refs/piceli/train/<name>/last` at
its head, promoted or not, so a reviewer can look at exactly what was
tested (`git log refs/piceli/train/shop/last`). To promote it later
without running the train again:

```sh
piceli train shop/train_spec.py --promote-run 20261023T101500Z-3f2a1c --push
piceli train shop/train_spec.py --promote-run last
```

It tags that head, moves `tested` and the base to it by fast-forward only
and runs the publish steps, as `--promote` does. It is refused when the
base is no ancestor of that head (it moved in a way that is not a
fast-forward to it: `train-not-fast-forward`), or when the run is unknown,
unfinished or joined nothing (`train-run-unknown`).

## Images for the cluster

The cluster's push-to-running wait is mostly building: the GitOps
controller sees the push, waits for a build host or starts a build Job,
then deploys. `Step.images("infra.py")` makes the train build those
images on `--promote`, before the push:

1. It loads the composition module from the promoted head and computes the
   **change key** the controller computes for that commit, with the same
   functions:
   - for each component built from this repository (its source's URL is
     one of the repository's remotes; or `components=[...]`): its contract
     in `piceli.toml` and the Git object id of every path its build reads
     (`contract_digest`);
   - for each build spec of a pipeline environment (`Build.spec(...,
     builder="host")`: `[build]` commands and shared commands, contexts,
     images with their target and shared files, toolchains): each image's
     key from the spec, the files its contexts select at that commit, the
     declared node facts and the controller's platforms (`image_keys`).
2. It builds them from the promoted head with the same host-build code as
   `piceli builder serve`, on a docker build host of the composition's
   cluster (`Cluster(dev=DevBuilds(hosts=[...]))`; the step's `hosts`
   choose), for the cluster's platforms (or `platforms=`), pushes them to
   the cluster's registry, and attaches their provenance (in-toto, naming
   the train run, tag and host) as OCI referrers.
3. It offers them to the controller: a `prebuilt` request with each
   image's component, key, source, commit, reference and digest, and a
   receipt.

The controller takes offers only when declared `Controller(prebuilt=True)`.
It keeps an offer after checking it names a declared component, points
into that component's repository of its own registry, is pinned by digest,
and that the digest is in the registry. It **uses** an offer only when a
step's own change key for the component, computed from its own clone of
the commit, is the offered key, and the digest is still in the registry;
then it records the image as its own build, with the train's provenance
(`builder: "train"`, the run, the host), and deploys it. An offer under any
other key is never used; a component it computes differently is built as
before.

So with `--promote --push` (publish steps run before the push), the build
the controller wants exists when it sees the commit: it deploys without
waiting for a host or a Job. In theory the push-to-running wait becomes
the time to see the push plus the deploy (about 30 s + 3 s of the measured
334 s median, which had 122 s of host wait and 171 s of build Job); the
images are built while the train promotes, off the critical path.

What a pipeline environment's build spec needs for this:

- **Its contexts read only the composition repository** (the repository
  the train runs in, holding `infra.py` and the spec): a context with
  `source = "other"` makes the key depend on the commit the controller
  will follow of that other repository, which the train cannot know. Such
  a spec fails the step (`train-images-failed`) and the controller builds
  as before.
- **The repository's `origin` remote is the URL `piceli gitops enable`
  recorded** (the controller names the composition repository by it, and
  the source name is part of the key).
- **The same platforms as the controller**: the key covers them. The
  default is the controller's own default, `linux/<arch>` of every node of
  the cluster, sorted; when the controller was enabled with other
  `--platform`s, give the same ones to `Step.images(platforms=[...])`. An
  offer for other platforms has another key and is never used.

The build itself is `piceli builder serve`'s: every platform, one OCI index
over them, the build receipt's toolchains in the provenance.

A failed images step (no `Controller(prebuilt=True)`, no build host, a
key that cannot be computed here, a failed build or push) fails with
`train-images-failed`; the promotion stands and the controller builds the
images itself. Delivery into the nodes is not handed over yet.

## What a train keeps, and pruning

Whatever a train creates it removes, and what it keeps is bounded by
`Train(retention=Retention(...))`. At the end of every run, and with
`piceli train-prune`, it prunes:

| What | Where | Kept |
| --- | --- | --- |
| Trial worktrees | `<cache>/checkouts/<train>/<repo>/<n>/` | none: a trial removes its own; one a crashed run left goes |
| Times records | `<cache>/checkouts/<train>/<repo>/<n>.times.json` | with their target directory |
| Cargo target directories | `<cache>/targets/<train>/<repo>/<n>/` (and `slot-<n>/`) | unused for `target_days=14` days go, then the least recently used beyond `targets_size="100GiB"` (per train and repository) |
| Content store | `<cache>/store/keys/*.json`, `<cache>/store/blobs/<aa>/<sha256>` | records unused for `store_days=30` days go, then the least recently used beyond `store_size="20GiB"` (shared by every train of the machine); bytes no record keeps go once an hour old |
| Run records and logs | `<state>/runs/<run>/` | runs older than `log_days=30` days go, but never the newest `keep_runs=20`; `flaky.jsonl` stays |

`<cache>` is `Train(cache=)`, else `$XDG_CACHE_HOME/piceli/train`, else
`~/.cache/piceli/train`; `<state>` is `$XDG_STATE_HOME/piceli/train/<name>`
(`--state-dir`). `<repo>` is the repository's directory name and a hash of
its path. A target directory's last use is the time of its `.piceli-used`
file, touched whenever a command uses it; a store record's is its file's
time, touched whenever it is reused. Nothing a running trial or command
holds is removed (their lock files are taken first). The slot lock files
under `<cache>/slots/` are a few bytes and stay.

The run's result reports the sizes kept and what went (`prune`: `freed`,
`removed`, `sizes` per kind, `limits`), and the human summary prints one
line (`cache: store 1.2 GiB of 20GiB, targets 14.0 GiB of 100GiB, ...`).
To prune by hand, or to see what would go:

```sh
piceli train-prune shop --dry-run          # a train by name: default limits
piceli train-prune shop/train_spec.py      # its spec: its own Train(retention=, cache=)
```

It prints the sizes per kind with their limits and each path that goes
(`--json`: one `piceli.train-prune.v1` object), and exits `0` (`2` when the
name is neither a train name nor a spec).

(train-queue)=
## The queue

A train runs one request at a time. A second `piceli train` on a busy
train is refused with `train-busy`, unless it asks to wait its turn:

```sh
piceli train shop/train_spec.py --branches feat-d feat-e --promote --push --queue --json
piceli train-queue shop                    # waiting requests, their flags, when queued
piceli train-queue shop --cancel q-20261023T101700123456Z-ab12
piceli train-wait q-20261023T101700123456Z-ab12 --json   # waits for the run that serves it
```

With `--queue` on a busy train, the request is written to the train's
queue and the command returns at once (exit 0) with one
`piceli.train.queued.v1` object: `queue_id`, `position`, `branches`,
`promote`, `push` and `queued_at`. Its branches are checked first (a branch
that does not exist is refused at once). On a free train, `--queue` runs
the request now, after any older waiting ones, and prints its result.

**Who serves the queue.** Every `piceli train` process, once its own run
ends, takes the oldest waiting request and runs it with the flags it was
queued with (`--promote`, `--push`), then the next, until the queue is
empty; then it exits. `piceli train-queue NAME --drain` does the same
without a run of its own. Each run is an ordinary run: its own record
(with `"queue": <id>`), events, notifications and trace. A request whose
branches no longer exist when it starts is rejected
(`train-branch-unknown`) and the queue goes on.

**If the serving process dies** (killed, machine off), its lock has no live
owner and its request stays `running` naming a dead process: `piceli
train-queue NAME` shows it `stale`. The next server (any `piceli train …`
of that train, or `piceli train-queue NAME --drain`) puts it back at the
head of the queue (`resumed` counts how often) and runs it again; the
killed run's record shows `interrupted`. Nothing is lost: requests are
files under `queue/waiting`, `queue/running` and `queue/done` in the
train's state directory, moved by `rename`, so exactly one process claims
each.

`piceli train-wait Q-ID` waits until the request has a run, then for that
run; it exits 1 with `train-queue-cancelled` for a cancelled request, the
request's reason when it was rejected, and `train-interrupted` when its
process died (resume it with `--drain`).

## Seeing a train

```sh
piceli trains                      # every train on this machine
piceli trains shop --follow        # stream the running step's log until the run ends
piceli trains shop --composition shop/composition.py --json
piceli train-wait shop --json      # block until the run ends; exit with its verdict
```

`piceli trains [NAME]` reads the train's state directory only (it never
takes the lock, never loads the spec) and shows:

- **the run in progress**: its branches and their verdicts so far, the
  trial, the steps done and the running step with its start, elapsed time,
  host and the last lines of its log (`--follow` streams the log);
- **the queue**: requests waiting (with their position, branches, flags
  and when they were queued), and the one being served (`stale` when its
  process died);
- **recent runs** (`--runs N`, default 10): state, duration, tag, push and
  each branch's verdict (with the failing step), and the seconds each step
  took, summed over the run's trials. A record still `running` while no
  live process holds the lock was killed: `interrupted`;
- **step trends** (`--trend N`, default 10): per step, the median of its
  last N executions and the last one against it: `slower` above 125 %,
  `faster` below 80 %, else `steady`;
- **flaky tests**: count, first and last seen, the branches of the last
  sighting and the output of the last first failure;
- **deployed by train**, with `--composition MODULE` and/or `--infra
  MODULE` (read as `piceli targets` reads them): each environment and
  NixHost with the run that produced what it runs, matched by its tag
  (`tag`), its commit being a promoted head (`commit`), or the product
  commit it pins being one (`pinned`).

```text
train shop: running 20261023T101500Z-3f2a1c for 2.1 min (trials)
  branches: feat-a=pending fix-c=pending
  trial 1: feat-a fix-c
    merge          passed   0.1 s on local
    spec           passed   0.6 s on local
    cargo-check    passed   52 s on local
    cargo-test     running  41 s on local (started 10:16:02Z)
      log ~/.local/state/piceli/train/shop/runs/20261023T101500Z-3f2a1c/01-cargo-test.log
      | test api::search::case_031 ... ok
  recent runs:
    20261022T090000Z-a1b2c3 ejected      6.9 min tested/2026-10-22.1 pushed  feat-x joined, fix-y ejected (fitness)
      cargo-check 54 s · cargo-test 2.0 min · fitness 3.2 s
  step trends (median of the last runs, last against it):
    cargo-test       median     81 s  last  2.0 min  slower (n=6)
  flaky tests:
      2x -p api --test http retries_once (last 2026-10-22T09:05:00Z, run 20261022T090000Z-a1b2c3)
deployed:
  environment main: 999999999999 deployed <- train shop run 20261022T090000Z-a1b2c3 (pinned)
```

`--json` prints one `piceli.trains.v1` object:

```json
{
  "schema": "piceli.trains.v1",
  "trains": [{
    "train": "shop",
    "state": "running",
    "current": {
      "run": "20261023T101500Z-3f2a1c", "started": "…", "elapsed": 125.0, "phase": "trials",
      "branches": ["feat-a", "fix-c"], "verdicts": {"feat-a": "pending", "fix-c": "pending"},
      "trial": {"id": 1, "branches": ["feat-a", "fix-c"], "base": "…", "started": "…"},
      "step": {"trial": 1, "name": "cargo-test", "kind": "cargo-test", "host": "local",
               "started": "…", "elapsed": 41.0, "log": "…/01-cargo-test.log", "tail": "…"},
      "done": [{"trial": 1, "name": "cargo-check", "verdict": "passed", "duration": 52.4, "host": "local"}],
      "pid": 4242
    },
    "runs": [{"run": "…", "state": "ejected", "ok": false, "started": "…", "duration": 412.7,
              "branches": [{"branch": "fix-y", "verdict": "ejected", "step": "fitness", "reason": "train-step-failed"}],
              "trials": 3, "steps": {"cargo-test": 120.4, "fitness": 3.2}, "tag": "tested/2026-10-22.1",
              "promote": "promoted", "push": "pushed", "flaky": 1}],
    "trends": [{"step": "cargo-test", "samples": 6, "median": 80.5, "last": 120.4, "last_run": "…", "ratio": 1.5, "trend": "slower"}],
    "flaky": [{"key": "-p api --test http retries_once", "count": 2, "first_seen": "…", "last_seen": "…", "last_tail": "…"}]
  }],
  "deployed": [{"kind": "environment", "name": "main", "state": "deployed",
                "revision": {"commit": "…", "tag": null}, "pinned": "…",
                "train": {"train": "shop", "run": "…", "tag": "tested/2026-10-22.1", "head": "…", "joined": ["feat-x"], "pushed": true},
                "matched": "pinned"}]
}
```

`deployed` is `null` without `--composition`/`--infra`.

`piceli train-wait RUN` takes a run id, or a train's name (its running
run, else its newest), waits (`--timeout S`, `--interval S`) and prints the
run's summary (below) with `--json`. Exit 0: the run ended ok; 1: a branch
was ejected, the promotion or push failed, the run was killed
(`train-interrupted`) or the timeout ran out (`train-wait-timeout`); 2: no
such run (`train-run-not-found`). A session that started a train in the
background uses it instead of polling.

The UI shows the same document, live: `piceli ui serve --trains` (and
`--train-composition MODULE` for the deployed-by-train join) adds a
**Trains** page (see {doc}`ui`).

```{image} _static/ui/trains.webp
:alt: The Trains page: the running step with its live log, recent runs, step durations, flaky tests and the targets each run produced
:width: 720px
```

(train-telemetry)=
## OpenTelemetry

```python
from piceli.infra import Otlp

train = Train("shop", steps=[...], telemetry=Otlp("http://localhost:4318", protocol="http/protobuf"))
```

Each run is one trace, sent over OTLP/HTTP (JSON) to `/v1/traces` and
`/v1/logs` of the endpoint, so the product's own Logs & Traces shows a train
like any other request:

```text
piceli.dev.train                     run: train, run id, branches; at the end state, ok, joined,
│                                    ejected (branch=step), tag, promote, push, flaky, duration
│  events: branch-joined, branch-ejected, branch-included, promoted, promote-rejected, pushed, push-failed
├─ piceli.dev.train.trial            trial, branches, verdict, failing step, duration
│  ├─ piceli.dev.train.step          step, kind, phase=test, host, verdict, duration, branches, selection
│  │     events: piceli.dev.train.flaky (test, target)
│  └─ …
└─ piceli.dev.train.step             phase=publish (publish steps hang off the root)
logs: piceli.dev.train.step          one per step, body = the last 20 lines of its output, on its span
      piceli.dev.train.finished      the run's headline and summary attributes, on the root span
```

Attributes are named `piceli.train.*`. A failed step, trial or run has
status `ERROR`. Spans are sent as they end, so a long run is visible while
it runs.

Without `telemetry=`, the standard variables apply, as for `piceli
deploy`: `OTEL_EXPORTER_OTLP_ENDPOINT` (or the per-signal ones),
`OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_SERVICE_NAME`,
`OTEL_RESOURCE_ATTRIBUTES`; `OTEL_SDK_DISABLED=true` turns it off. A
train runs outside the cluster, so `Otlp(protocol="grpc")`,
`headers_secret=` and `ca_secret=` are refused (`train-invalid`): put
headers in `OTEL_EXPORTER_OTLP_HEADERS`. A composition's `Otlp` can be
reused when it is an HTTP endpoint this machine reaches.

A collector that is down never slows or fails a train: requests go
through a bounded queue to a background thread, each with a 2 s timeout;
a full queue drops, three failures in a row stop the exporter, and the end
of the run waits at most the timeout for the queue to drain. The first
failure prints one warning (`train-telemetry-failed`).

Step output goes to your collector in the step log records: steps should
not print secrets.

(train-notify)=
## Notifications

```python
from piceli.train import Notify

train = Train(
    "shop",
    steps=[...],
    notify=[
        Notify.desktop(),
        Notify.webhook(url_env="SHOP_TRAIN_HOOK", headers_env={"Authorization": "SHOP_HOOK_TOKEN"}),
        Notify.command(["scripts/train_done.sh"], when="failure"),
    ],
)
```

When a run ends, its notifiers run one after the other, each within its
`timeout` (default 10 s):

| Notifier | Sends |
| --- | --- |
| `Notify.command(argv, when="always", timeout=10)` | runs `argv` with the summary JSON on stdin; exit 0 is success |
| `Notify.webhook(url=None, *, url_env=None, headers_env=None, when="always", timeout=10)` | `POST`s the summary JSON; the URL is given or read from `url_env` (a webhook URL that is a secret stays out of the spec), each header from the variable `headers_env` names; a 2xx is success |
| `Notify.desktop(when="always", timeout=10)` | a desktop notification with the headline: `osascript` on macOS, `notify-send` elsewhere; skipped when neither exists |

`when="failure"` sends only for a run that is not ok. A run that raises
midway (a Git failure, a crash, Ctrl-C) is notified too: see
{ref}`train-failed`. The summary
(`piceli.train.summary.v1`) is also the `piceli.dev.train.finished`
OpenTelemetry event and what `piceli train-wait --json` prints:

```json
{
  "schema": "piceli.train.summary.v1",
  "train": "shop",
  "run": "20261023T101500Z-3f2a1c",
  "state": "ejected",
  "ok": false,
  "joined": ["feat-a"],
  "included": [],
  "ejected": [{"branch": "fix-c", "step": "fitness", "reason": "train-step-failed"}],
  "promoted": true,
  "tag": "tested/2026-10-23.1",
  "pushed": true,
  "promote": "promoted",
  "push": "pushed",
  "head": "<commit>",
  "duration": 412.7,
  "flaky": [{"key": "-p api --test http retries_once", "count": 3}],
  "record": "…/runs/<run>/run.json"
}
```

It carries names, commits, step names and fixed reason codes, never step
output. A failing notifier never changes the run's result: it prints one
warning (`train-notify-failed`), appends a `notify-failed` event, and the
run record's `notify` list says what each notifier did (`sent`,
`skipped`, `not-wanted`, `failed`).

(train-failed)=
## A run that fails midway

A run that raises after it started (a Git command that fails:
`train-git-failed`; an unexpected error: `train-run-failed`; Ctrl-C:
`train-run-cancelled`) does not leave a `running` record behind. It:

- writes its record with `"state": "failed"`, `"ok": false`, the `reason`
  and the verdicts reached so far (branches already joined stay `joined`;
  without `--promote` no ref moved, and a promotion that had started is in
  the record);
- emits `run-failed` with the summary (`"state": "failed"`, `"reason"`,
  `joined` so far);
- sends the notifications and the `piceli.dev.train.finished` event with
  that summary (the root span carries `piceli.train.reason` and status
  `ERROR`).

`piceli train` exits 1 with the reason (`{"state": "failed", "reason":
…, "run": …}`), and `piceli train-wait` exits 1 for it. Only a run killed
outright (`SIGKILL`, power loss) leaves `running`, shown as
`interrupted`.

## Hooks

Commands run through a runner (`piceli.train.runner.Runner`: one command
in, exit status, start, duration, host and output out; `LocalRunner` runs
them here in a slot, `piceli.train.hosts.HostRunner` places them on the
train's hosts), and the run reports each step and verdict to an event sink
(`run-started`, `trial-started`, `step-started` (with host and log),
`step-finished` (with kind, verdict, duration, the host it ran on, log,
selection, flaky tests and a failing tail), `trial-finished`,
`halves-parallel`, `trial-discarded`, `branch-joined`, `branch-ejected`,
`branch-included`, `outputs-restored`, `promoted`, `promote-rejected`,
`published`, `publish-failed`, `pushed`, `push-failed`, `pruned`,
`run-finished` (with the summary), `run-failed` (with the reason and the
summary), `notify-failed`). Every event is also appended to
`events.jsonl`. A sink that raises never stops the run.
