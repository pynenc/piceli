# Using Piceli from an agent

This page tells a coding agent or CI bot how to drive Piceli safely: which
commands only read, which need the owner's approval, how to read the output and
the errors, how to resume, and what never to print.

```{admonition} Maturity: preview
:class: note

The output contract below is new in this release. Commands marked `partial` in
{doc}`reference/cli` do not follow it fully yet. See the {doc}`roadmap` for
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
  for streaming commands. Parse it; do not scrape stderr.
- **stderr** carries human text: summaries, hints and the plan hash to approve.
- A refusal prints `{"state": "rejected", "reason": "<code>"}` and changes
  nothing. The code is stable and explained by `piceli explain <code>`.

| Exit code | Meaning | What to do |
| --- | --- | --- |
| `0` | Success | Continue. |
| `1` | The operation ran but did not succeed (not ready, drift, build failed) | Read the JSON result; do not retry blindly. |
| `2` | Rejected before any change | Read `reason`, run `piceli explain <reason> --json`, apply its `fix`. |
| `3` | Approval required; nothing was executed | Show the plan to the owner and wait. |

Commands whose contract is `conforms` in {doc}`reference/cli` follow these rules
exactly (`explain`, `help-json`). Commands marked `partial` print JSON on stdout
but still differ in how they refuse:

- `piceli release …` prints `{"state": "refused", "reason": "<sentence>"}` on
  stdout. Rely on the exit code `2`; see
  [If `plan` refuses](release_cli.md#if-plan-refuses) for the next step.
- `piceli artifacts …` (including `build-spec`) prints the rejection object on
  **stderr** with a registered code.
- `piceli inputs …` prints `{"state": "rejected", "reason": "<sentence>"}` on
  stderr.

These will converge on the contract in a later release.

## Commands an agent may run without asking

These never change a cluster, registry or node. Some write local files, as
noted.

- `piceli explain`, `piceli help-json`
- `piceli release plan` and `piceli release preview`: read the cluster, store
  a pending plan and secret candidates in the spec's `state_dir`, and print the
  plan hash. They never write to the cluster.
- `piceli release status`: reads local state only.
- `piceli inputs record` and `piceli inputs verify`: read git; `record --out`
  writes the lock file.
- `piceli artifacts preview`, `piceli artifacts inspect`, `piceli artifacts pin`,
  `piceli artifacts preview-command`, `piceli artifacts build-spec preview`
- `piceli observe status`, `piceli operator status`: read the cluster through an
  explicit `--kubeconfig`.
- `piceli observe forward-list`, `piceli observe forward-command`,
  `piceli observe logs-command`: print what would run, start nothing.
- `piceli model list`, `piceli deploy plan`: read local model files only.
- `piceli artifacts build`: assembles an OCI layout in `--output` without
  running any code.
- `piceli observe forward-save`, `piceli operator backup`: write a local
  preferences file or backup archive.

## Commands that need the owner's approval

Ask before running these, and show the owner what will happen first.

| Command | Changes | Approve with |
| --- | --- | --- |
| `piceli release apply` | The cluster | `--approve <plan hash>` from `release plan`, after the owner reviewed that plan |
| `piceli release rollback` | The cluster | `--approve <plan hash>` from `release rollback <target>` without `--approve` |
| `piceli release resume` | The cluster (continues an approved execution) | The owner's go-ahead to continue |
| `piceli release stop` | Local journal (cancels an execution) | The owner's go-ahead |
| `piceli artifacts deliver` | A registry or node | `--approve-digest <config digest>` |
| `piceli artifacts build-spec run` | Runs a build, writes outputs and images | `--approve-builder <digest>` and `--approve-plan <hash>` |
| `piceli artifacts execute-command` | Runs a pinned tool | `--approve-plan <hash>` |
| `piceli artifacts import-local` | The local Docker image store | `--approve-digest <digest>` |
| `piceli operator approve`, `piceli operator promote`, `piceli operator restore` | Operator state, catalog or files | The owner's go-ahead |
| `piceli observe serve`, `piceli operator serve`, `piceli observe forward-run`, `piceli observe forwards apply`, `piceli observe logs-run` | Long-running local processes and ports | The owner's go-ahead |

Never run `piceli deploy run` from an agent: it uses the current kube context
and deletes and recreates objects without a plan to approve. Use
`piceli release` instead.

Never add `--auto-approve` unless the owner has said that this run is an
unattended CI job for this exact spec.

## The approval workflow

1. Run `piceli release plan --spec release.toml`. Keep the JSON from stdout.
2. Show the owner the summary from stderr: every `create`, `adopt`, `delete`
   and `drift` line, and the adoption notes (a takeover removes fields other
   clients wrote).
3. Wait for the owner to approve **that plan hash**. A plan expires after
   `approval_window_seconds`; if it did, plan again and ask again.
4. Run `piceli release apply --spec release.toml --approve <hash>`.
5. Exit `0` means the release is ready. Exit `1` means it ran but did not
   become ready: report `execution.failure_category`, and offer
   `piceli release rollback previous` (which needs its own approval).

## When something fails

1. Read `reason` from the JSON.
2. Run `piceli explain <reason> --json`. The entry has `cause`, `fix` and
   `retry_safe`.
3. If `retry_safe` is `true`, re-run the same command once. Otherwise apply the
   `fix` (it names the flag or command) or report it to the owner.
4. An unknown code (`piceli explain` exits `2` with `unknown-error-code`) means
   the reason is a sentence from a `partial` command: report it verbatim.

## Resuming interrupted work

- **`release apply` was interrupted** (timeout, crash, lost connection): run
  `piceli release status --spec release.toml`, then
  `piceli release resume --spec release.toml`. Resume reuses the approved
  grant and operation ids; do not plan and apply a new release instead.
  Re-applies and rollbacks are not resumable: plan them again.
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
