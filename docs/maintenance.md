# Runner hygiene: disk, caches, doctor and run summaries

A runner that deploys every push fills its disk: build outputs and logs,
receipts, run journals, temporary directories. This page shows what Piceli
keeps, how to see it (`piceli cache status`), how to remove what no release
needs (`piceli cache prune`, `cache_budget=`), how to check a runner before a
build (`piceli doctor`), and where every run's summary is
(`piceli runs`, `runs/<id>/summary.md`).

```{admonition} Maturity: preview
:class: note

New in 0.8.0. The JSON of `cache status`, `cache prune`, `doctor`, `runs` and
`summary.json` only gains fields within a version series.
```

None of these commands contacts a cluster. Each takes the pipeline
(`MODULE:ATTR`, optionally `--env NAME`; without `--env` every environment's
state directory is included) or a state directory (`--state-dir DIR`,
default `./.piceli-deploy`, the pipeline default).

## Temporary files are always removed

Piceli creates temporary directories for TLS material (generated
certificates and keys), OCI layouts, build staging, `--ref` worktrees,
local image imports, state archives and SQLite snapshot copies. Each is
named `piceli-<purpose>-<random>` in the system temporary directory (`TMPDIR`
moves it; next to an output it renames into place, `.piceli-<purpose>-…`),
is owner-only (`0700`), and is removed when the work ends: on success, on an
error, on `Ctrl-C`, and on `SIGTERM` or `SIGHUP` (a cancelled CI job, a
closed terminal) too, before the process exits with the signal's usual
status. A write that fails leaves no partial file.

Only `SIGKILL` (or a power loss) can leave one behind: `piceli cache prune`
removes `piceli-*` temporary directories older than six hours. Piceli's own
test suite fails when any test leaves a temporary file behind.

## See what is on disk

```sh
piceli cache status examples/shop/app.py:pipeline
piceli cache status --state-dir .piceli-deploy --json
```

Expected output (stderr; `--json` adds one object on stdout, schema
`piceli.cache-status.v1`):

```text
shop: 412.3 MiB (budget 20.0 GiB), 14 run(s), 180.2 MiB reclaimable
  builds       398.0 MiB  3 file(s)
  receipts       24.1 KiB  9 file(s)
  runs          312.5 KiB  28 file(s)
  release         2.1 MiB  17 file(s)
  other           0 B  2 file(s)
temporary directories: 0 (0 B), 0 stale (0 B)
```

| Category | What | Pruned |
| --- | --- | --- |
| `builds` | Build outputs (`builds/<name>/outputs/`) and build logs: the local build cache | Only over a budget |
| `toolchains`, `blobs` | Toolchain targets and base or mirror blobs a builder keeps in the state directory | Not yet |
| `receipts` | Build, delivery and mirror receipts | Delivery receipts no kept run or current build uses |
| `runs` | Run journals and run summaries (`runs/`) | Beyond `--keep-last`; see below |
| `release` | Release catalog, execution journal, secret store, history, approved plans, backups, check reports | **Never** |
| `other`, `temp` | Locks, keys, the shared-state marker; partial files of a killed write | Partial files older than an hour |

`reclaimable_bytes` is what `cache prune` would free with the same
`--keep-last` and no budget.

## Prune safely

```sh
piceli cache prune examples/shop/app.py:pipeline --dry-run      # what would go
piceli cache prune examples/shop/app.py:pipeline --keep-last 5
piceli cache prune --state-dir .piceli-deploy --budget 20GiB
```

A prune removes, in this order:

1. stale `piceli-*` temporary directories (older than six hours) and
   partial files older than an hour;
2. runs beyond the last `--keep-last` (default 10), with their summaries,
   and the delivery receipts that no kept run and no current build uses (the
   next deploy of such an image delivers it again);
3. only while the state directory is still over `--budget` (or the
   pipeline's `cache_budget`): build outputs and logs, largest first, then
   more of the old runs.

It **never** removes the release state (`release/`, `registry/`: the active
release's catalog and execution journal, the secret store, approved plans,
backups), build or mirror receipts, the latest run, a run that can still be
resumed, or the latest run of each of the last `--keep-last` applied
releases (what `piceli release rollback` of them may need). It holds each
state directory's run lock, so it is refused with `pipeline-locked` while a
deploy of that directory runs. With shared state (`state="cluster"`) the
state directory is a working copy: only machine-local files (step 1, build
outputs and logs) are pruned.

The result is one JSON object (`piceli.cache-prune.v1`) with every removed
path and its size. When a state directory is still over the budget after
everything that may go went, the command exits `1` with
`cache-over-budget`: see what is left with `piceli cache status`.

## Keep a pipeline within a budget

```python
pipeline = Pipeline(app, target, build=images, deliver=Registry(url),
                    cache_budget="20GiB")
```

After every run (ready or not), `piceli deploy` prunes the state directory
like `piceli cache prune --budget 20GiB`, holding the run lock it already
has. The deploy result gains `cache` (`budget_bytes`, `bytes`,
`freed_bytes`, `over_budget`); a state directory that stays over budget is
reported on stderr and never fails the deploy. `cache_budget` is not part of
the plan hash. Sizes accept `20GiB`, `500MB` (decimal), `1.5G` or bytes;
anything else is refused with `pipeline-invalid` (and `--budget` with
`cache-budget-invalid`).

## Check a runner before a build

```sh
piceli doctor examples/shop/app.py:pipeline
piceli doctor --json
```

```text
ok       disk:temp: 37.4 GiB free, 4.0 GiB needed
ok       disk:state_dir: 37.4 GiB free, 4.0 GiB needed
warn     memory: 1.2 GiB available, 2.0 GiB needed
ok       tool:docker: found
ok       tool:docker buildx: found
warn     tool:kubectl: `kubectl version --client=true --output=json` did not run
next: piceli explain runner-memory-low
```

- **Disk**: the free space where the state directory and the temporary
  directory live, against what the next build needs: twice the image and
  output bytes of the last build receipts plus 1 GiB, and at least 4 GiB
  (256 MiB for a pipeline without a build).
- **Memory**: available memory (Linux `MemAvailable`; `unknown` elsewhere)
  against 2 GiB for a build.
- **Tools**: `docker` and `docker buildx` for a build, `kubectl` for a
  `NodeLoopbackRegistry` (its port forward); every one without a pipeline.
  Each is run with a version flag only.

Exit `0` when every check passes, `1` with a warning (`runner-disk-low`,
`runner-memory-low`, `runner-tool-missing`), so a CI job can stop before a
build that would fail halfway: `piceli doctor "$PIPELINE" || exit 1`.

(maintenance-summaries)=
## Run summaries

Every `piceli deploy` run that starts executing writes, when it ends
(ready, stopped, failed, rejected, rolled back or interrupted; a resumed run
rewrites them):

- `<state_dir>/runs/<run id>/summary.json`: for agents, schema
  [`piceli-run-summary-v1`](schemas/piceli-run-summary-v1.schema.json);
- `<state_dir>/runs/<run id>/summary.md`: the same for people, ready to post
  as a CI job summary or a pull request comment (see {doc}`ci`).

A summary holds the sources' commits (and `--ref` revisions), each image's
config digest, immutable reference, size and delivery (blobs uploaded and
reused), the release plan's action classes and counts, each changed object
with the JSON pointers of its changed fields (never values), the checks, the
failure (stage, registered code, `piceli explain` command, message, readiness
category) and every stage's time. It never contains a secret value, a
kubeconfig path or process output. The deploy result names both files
(`summary.json`, `summary.markdown`).

```sh
piceli runs examples/shop/app.py:pipeline            # newest first, on stderr
piceli runs examples/shop/app.py:pipeline --json     # piceli.runs.v1
```

Each listed run has its state, the error code when it did not become ready,
the release, the total stage time and its summary files. With shared state
`piceli runs` reads the local working copy: run `piceli state pull --spec
MODULE:ATTR` first to see other runners' runs.
