# Using Piceli from an agent

This page tells a coding agent or CI bot how to drive Piceli safely: which
commands only read, which need the owner's approval, how to read the output and
the errors, how to resume, and what never to print.

```{admonition} Maturity: preview
:class: note

The output contract below is new in 0.3.0 and, since 0.4.0, covers every
`release`, `inputs`, `artifacts`, `observe` and `operator` command (marked
`conforms` in {doc}`reference/cli`). Only `render` is still `partial`, and the
legacy `model`/`deploy` commands are `legacy`. See the {doc}`roadmap` for
every feature's status.
```

## Start here

| Need | Where |
| --- | --- |
| Every command, option, side effect and approval rule, as JSON | `piceli help-json` (same as `piceli --help-json`) |
| What an error code means and what to do next | `piceli explain <code> --json` |
| The same, as pages | {doc}`reference/cli`, {doc}`reference/errors` |
| An index of the documentation for language models | [`llms.txt`](https://docs.pynenc.org/projects/piceli/en/latest/llms.txt) |

`piceli help-json` is generated from the command definitions. For each
command, `contract.side_effects` says what it reads and writes and whether it
contacts a cluster, `approval_required` says whether it changes anything
outside local files, and `safe_to_retry` says whether re-running it after an
interruption is harmless.

## The output contract

- **stdout** carries machine output: one JSON object per command, or JSON lines
  for streaming commands (`observe forwards apply`). Parse it; do not scrape
  stderr.
- **stderr** carries human text: summaries, hints and the plan hash to approve.
  It never carries a JSON object.
- A refusal prints
  `{"state": "rejected", "reason": "<code>", "message": "<human text>", …}`
  and changes nothing. `reason` is stable and explained by
  `piceli explain <code>`; `message` is for people and may change. Some
  commands add fields: `release` refusals keep `blocking[]` (each item with
  its own `code` and `message`), `inputs` adds `source`, `observe forwards
  apply` adds `preflight`.
- An operation that ran but did not succeed exits `1` and its result names
  the code in `reason`: `{"state": "failed", "reason": …}` for `release
  apply|rollback|resume` and `artifacts build-spec run`, `"state": "drift"`
  for `inputs verify`, a receipt with `state` `failed`/`rejected` for
  `artifacts deliver`.

| Exit code | Meaning | What to do |
| --- | --- | --- |
| `0` | Success | Continue. |
| `1` | The operation ran but did not succeed (not ready, drift, build failed) | Read the JSON result; do not retry blindly. |
| `2` | Rejected before any change | Read `reason`, run `piceli explain <reason> --json`, apply its `fix`. |
| `3` | Approval required; nothing was executed | Show the plan to the owner and wait. |

Commands whose contract is `conforms` in {doc}`reference/cli` follow these rules
exactly (including `explain`, `help-json`, `status`, `access` and `deploy`). For
a refused release plan, see [If `plan` refuses](release_cli.md#if-plan-refuses)
for the next step.

### Contract changes in 0.4.0

0.4.0 moved the remaining preview commands onto the contract. Their machine
output changed as follows; exit codes did not change.

| Commands | Before (0.3.0) | Now (0.4.0) |
| --- | --- | --- |
| `release plan/preview/apply/rollback/resume/stop/status/secret show` | Refusal `{"state": "refused", "reason": "<sentence>", "code": "<code>"?}` on stdout | `{"state": "rejected", "reason": "<code>", "message": "<sentence>", "code": "<code>"}`; `code` is an alias of `reason` for 0.4.x only. Results of `apply`/`rollback`/`resume`/`stop` add `state` (`succeeded`, or `failed` with `reason`). |
| `inputs record/verify` | Refusal on **stderr**, `reason` a sentence | Refusal on stdout, `reason` a code, sentence in `message`; drift adds `"reason": "source-drift"` |
| `artifacts …` including `build-spec` and `deliver` | Refusal on **stderr** (`build-spec`: the last stderr line) | Refusal or failure object on stdout, with `message` (the code's title); a failed `execute-command`/`import-local` result adds `reason` |
| `observe …`, `operator …` | Usage errors, tracebacks, or `{"ok": false, "preflight": …}` on stderr | Refusal on stdout with a code; `forwards status` adds `state` and, on exit 1, `reason` |

See also {ref}`the release contract changes <release-contract-changes>`.

## Commands an agent may run without asking

These never change a cluster, registry or node. Some write local files, as
noted.

- `piceli explain`, `piceli help-json`
- `piceli render`: imports the model module and reads the spec; never contacts
  a cluster or reads secret values.
- `piceli release plan` and `piceli release preview`: read the cluster, store
  a pending plan and secret candidates in the spec's `state_dir`, and print the
  plan hash. They never write to the cluster.
- `piceli release diff`: reads the cluster and sends only server-side dry runs
  (`dryRun=All`); prints what `plan` would change, field by field. Writes no
  local state.
- `piceli release status`: reads local state only.
- `piceli status TARGET --json`: reads the release state, the cluster through
  the target's explicit kubeconfig, and probes the declared forwards on
  `127.0.0.1`. It says whether the app is up (`state`) and how to reach it
  (`access.forwards[].url`); exit `1` means not up. See {doc}`access`.
- `piceli inputs record` and `piceli inputs verify`: read git; `record --out`
  writes the lock file.
- `piceli artifacts preview`, `piceli artifacts inspect`, `piceli artifacts pin`,
  `piceli artifacts preview-command`, `piceli artifacts build-spec preview`
- `piceli observe status`, `piceli operator status`: read the cluster through an
  explicit `--kubeconfig`.
- `piceli observe forward-list`, `piceli observe forward-command`,
  `piceli observe logs-command`: print what would run, start nothing.
- `piceli observe forwards status`: probes the declared loopback ports once
  (never the cluster); exit `1` with `"reason": "forward-unhealthy"` when a
  required forward is down.
- `piceli import live` (reads the cluster through an explicit `--kubeconfig`
  and `--context`) and `piceli import yaml` (reads local files): generate a
  typed module; `--out` writes that one file and refuses to overwrite it
  without `--force`. Secret values are never read into the output.
- `piceli deploy MODULE:ATTR --plan`: plans every stage (reads sources,
  the local image store, the registry or node, and the cluster), writes only
  the pipeline's `state_dir`, and prints the combined hash. It never builds,
  pushes or applies.
- `piceli artifacts build`: assembles an OCI layout in `--output` without
  running any code.
- `piceli observe forward-save`, `piceli operator backup`: write a local
  preferences file or backup archive.

## Commands that need the owner's approval

Ask before running these, and show the owner what will happen first.

| Command | Changes | Approve with |
| --- | --- | --- |
| `piceli deploy` | Builds images, pushes them to a registry or node, applies a release | `--approve <combined hash>` from `piceli deploy MODULE:ATTR --plan`, after the owner reviewed that plan; `--resume` continues an approved run |
| `piceli release apply` | The cluster | `--approve <plan hash>` from `release plan`, after the owner reviewed that plan |
| `piceli release rollback` | The cluster | `--approve <plan hash>` from `release rollback <target>` without `--approve` |
| `piceli release resume` | The cluster (continues an approved execution) | The owner's go-ahead to continue |
| `piceli release stop` | Local journal (cancels an execution) | The owner's go-ahead |
| `piceli release check` | Nothing by itself, but runs the spec's checks (declared pod execs and Python functions) | The owner's go-ahead for a spec you did not write |
| `piceli artifacts deliver` | A registry or node | `--approve-digest <config digest>` |
| `piceli artifacts build-spec run` | Runs a build, writes outputs and images | `--approve-builder <digest>` and `--approve-plan <hash>` |
| `piceli artifacts execute-command` | Runs a pinned tool | `--approve-plan <hash>` |
| `piceli artifacts import-local` | The local Docker image store | `--approve-digest <digest>` |
| `piceli operator approve`, `piceli operator promote`, `piceli operator restore` | Operator state, catalog or files | The owner's go-ahead |
| `piceli access`, `piceli observe serve`, `piceli operator serve`, `piceli observe forward-run`, `piceli observe forwards apply`, `piceli observe logs-run` | Long-running local processes and ports | The owner's go-ahead |

The legacy `piceli deploy run` command (current kube context, delete and
recreate, no approval) is no longer registered: `piceli deploy` is now the
pipeline command, which plans first and needs an approval.

Never add `--auto-approve` unless the owner has said that this run is an
unattended CI job for this exact spec.

## The approval workflow

1. Run `piceli release plan --spec release.toml`. Keep the JSON from stdout.
2. Show the owner the summary from stderr: every `create`, `adopt`, `delete`
   and `drift` line, the changed fields under each `apply` (all of them are in
   `diffs` in the JSON), and the adoption notes (a takeover removes fields
   other clients wrote). A plan whose `summary` has only `no-op` changes
   nothing.
3. Wait for the owner to approve **that plan hash**. A plan expires after
   `approval_window_seconds`; if it did, plan again and ask again.
4. Run `piceli release apply --spec release.toml --approve <hash>`.
5. Exit `0` means the release is ready and its `[[checks]]` passed. Exit `1`
   means it ran but is not ready. Report `reason` (`check-failed`, the failure
   category, or `execution-not-ready`) and `execution.state`. When
   `release_state` is `checks-failed`, report the failed checks and the
   `rollback` object; an automatic rollback has already run if the spec
   enables it. Offer `piceli release rollback previous` (which needs its own
   approval) only when no automatic rollback succeeded. Never add
   `--skip-checks` unless the owner asked for it (see {doc}`checks`).

### Deploying a pipeline

1. Run `piceli deploy MODULE:ATTR --plan --json`. The last line is the result
   with `combined_hash` and every stage's plan.
2. Show the owner the stderr summary: which builds run, which images are
   delivered, the release's `create`/`apply`/`delete` lines and the checks.
3. After the owner approves **that combined hash**, run
   `piceli deploy MODULE:ATTR --approve <hash> --json`. Each stdout line is
   one stage event; the last one is the result.
4. Exit `0` means the release is ready and its checks passed. Exit `1`
   means a stage ran but did not succeed (`reason` names it; with
   `rollback_on_failed_checks` the result's `checks.rollback` says what was
   restored). Exit `2` with `pipeline-plan-changed` means something changed
   since the plan: plan and ask again.

## When something fails

1. Read `reason` from the JSON.
2. Run `piceli explain <reason> --json`. The entry has `cause`, `fix` and
   `retry_safe`.
3. If `retry_safe` is `true`, re-run the same command once. Otherwise apply the
   `fix` (it names the flag or command) or report it to the owner.
4. An unknown code (`piceli explain` exits `2` with `unknown-error-code`)
   should not happen for a `conforms` command: report the whole JSON object
   verbatim.

## Resuming interrupted work

- **`release apply` was interrupted** (timeout, crash, lost connection): run
  `piceli release status --spec release.toml`, then
  `piceli release resume --spec release.toml`. Resume reuses the approved
  grant and operation ids; do not plan and apply a new release instead.
  Re-applies and rollbacks are not resumable: plan them again.
- **`piceli deploy` was interrupted** (or failed at a stage you then fixed):
  run `piceli deploy MODULE:ATTR --resume`. It continues the approved run at
  the first unfinished stage and reuses the finished stages' receipts; an
  interrupted apply is resumed with the same grant.
- **`artifacts deliver` was interrupted**: run it again with the same
  arguments. Registry pushes are content addressed and send only missing
  layers.
- **`artifacts build-spec run` was interrupted**: run it again; outputs are
  published atomically.

## Secrets

- Never read or print secret values. Do not open the secret store in the
  release `state_dir`, and do not run `kubectl get secret -o yaml` or similar.
- Plans, receipts and errors carry digests and pointers, never values; the
  plan written by `release plan --out` is redacted.
- Pass registry credentials only as a file (`--credentials`, mode `0600`),
  never on the command line or in logs.
- Error codes never contain secrets or private paths, so they are safe to
  report.

## Cluster access

Piceli's release, observe, operator and artifact commands use only the
kubeconfig file and context they are given (in the spec or with
`--kubeconfig`/`--context`). They never read `~/.kube/config`, `KUBECONFIG` or
the current context: `--context` is required wherever `--kubeconfig` is
accepted (`observe`, `operator`, `artifacts deliver --via-forward`), and a
missing context is a usage error (exit `2`), never a fallback to the file's
`current-context`. Do not change the kubeconfig or context an owner has set
in a spec, and do not pick a context yourself: ask the owner which one to use.

Never add or change `allow_exec`, `exec_sha256` or `exec_pass_env` in a spec,
or pass `--allow-exec`/`--exec-sha256`, yourself: allowing an exec credential plugin runs a program with the owner's
cloud login ({doc}`managed_clusters`). On `exec-auth-not-allowed`,
`exec-pin-mismatch` or `auth-provider-refused`, report the code and wait for
the owner. `exec-plugin-failed` usually means the owner must log in again.
