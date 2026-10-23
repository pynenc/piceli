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
parallel="auto", cache=None)`:

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

The train's identity and where it runs come from the spec you run; the
steps come from the commit under test.

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
- `flaky.jsonl`: append-only, one line per flaky test per run that saw it,
  with its running totals (`count`, `first_seen`, `last_seen`,
  `last_tail`); the last line of a test is its current state;
- `lock`: held while a run is active; a second run is refused with
  `train-busy`.

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
- trials use a few **stable worktree paths** (`checkouts/<train>-<repo>/N`
  under the cache, held while a trial runs, removed after it), each with
  **its own target directory** (`targets/<train>-<repo>/N`): cargo
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

1. It loads the composition module from the promoted head and, for each
   component built from this repository (its source's URL is one of the
   repository's remotes; or `components=[...]`), computes the **change
   key** the controller computes for that commit: the component's contract
   in `piceli.toml` and the Git object id of every path its build reads
   (the same function, `contract_digest`).
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

A failed images step (no `Controller(prebuilt=True)`, no build host, a
failed build or push) fails with `train-images-failed`; the promotion
stands and the controller builds the images itself. Pipeline environments
(build specs) and delivery into the nodes are not handed over yet.

## Hooks for later

Commands run through a runner (`piceli.train.runner.Runner`: one command
in, exit status, start, duration, host and output out; `LocalRunner` runs
them here in a slot, `piceli.train.hosts.HostRunner` places them on the
train's hosts), and the run reports each step and verdict to an event sink
(`run-started`, `trial-started`, `step-started`, `step-finished`,
`trial-finished`, `halves-parallel`, `trial-discarded`, `branch-joined`,
`branch-ejected`, `outputs-restored`, `promoted`, `published`, `pushed`,
`run-finished`). Status views, OpenTelemetry spans and notifications
attach there.
