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
  steps run in a temporary worktree of the merge, removed afterwards.
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
tag="tested/{date}.{n}", remote="origin")`:

- `name` is a DNS label. It names the run history and the merge commits.
- `repo` is the Git repository, relative to the module's directory.
- `tag` names a promoted head: `{date}` is the UTC day (`YYYY-MM-DD`),
  `{n}` one more than the highest number used that day (required), `{name}`
  the train's name.
- `steps` run in order. A train needs at least one test step: a branch
  never joins untested.

The steps:

| Step | Runs | Passes when |
| --- | --- | --- |
| `Step.command(name, argv, env=None, timeout=3600)` | `argv` in the merged checkout | it exits 0 |
| `CargoCheck(select=Impact(), args=("--all-targets",))` | `cargo check` on the selected packages | it exits 0 (skipped when nothing selected) |
| `CargoTests(select=Impact(), args=(), retry=1)` | `cargo test --no-fail-fast` on the selected packages, then the failed tests again by exact name | every test passed, first time or on a retry |
| `Step.publish(name, argv, env=None, timeout=3600)` | `argv` in a checkout of the promoted head, only with `--promote` | it exits 0; it never judges a candidate |

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

Without `--promote` no ref moves: the result names the head the train
reached. Nothing is pushed without `--push`.

Exit codes: `0` every branch joined (or was already in) and the promotion
and push asked for succeeded; `1` a branch was ejected, the promotion was
refused (`train-not-fast-forward`, `train-checkout-dirty`), a publish step
failed (`train-publish-failed`) or the push failed (`train-push-failed`);
`2` rejected before anything ran (`train-not-found`, `train-branch-unknown`,
`train-push-needs-promote`, `train-busy`, …).

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
   joins. Joined branches keep the order given.
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

Every step has its start (UTC), duration, host (`local` for now), verdict
and log path. `promote.state` is `not-requested`, `nothing-joined`,
`promoted`, `rejected` (with `reason`) or `publish-failed`; `push.state` is
`not-requested`, `pushed` or `failed`.

## State and history

Each train keeps, under its state directory
(`$XDG_STATE_HOME/piceli/train/<name>`):

- `runs/<run>/run.json`: the run's result, rewritten after each trial (a
  run that was killed leaves its last state, `"state": "running"`);
- `runs/<run>/<trial>-<step>.log`: every step's output;
- `flaky.jsonl`: append-only, one line per flaky test per run that saw it,
  with its running totals (`count`, `first_seen`, `last_seen`,
  `last_tail`); the last line of a test is its current state;
- `lock`: held while a run is active; a second run is refused with
  `train-busy`.

## Hooks for later

Commands run through a runner (`piceli.train.runner.Runner`: one command
in, exit status, start, duration, host and output out; `LocalRunner` runs
them here), and the run reports each step and verdict to an event sink
(`run-started`, `trial-started`, `step-started`, `step-finished`,
`trial-finished`, `branch-joined`, `branch-ejected`, `promoted`,
`published`, `pushed`, `run-finished`). Remote build hosts, status views,
OpenTelemetry spans and notifications attach there.
