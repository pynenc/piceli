# Using Piceli from an agent

This page tells a coding agent or CI bot how to drive Piceli safely: which
commands only read, which need the owner's approval, how to read the output and
the errors, how to resume, and what never to print.

```{admonition} Maturity: preview
:class: note

The output contract below is new in 0.3.0 and, since 0.4.0, covers every
command (marked `conforms` in {doc}`reference/cli`) except `render`,
`release check` and `release diff`, which are still `partial`. See the
{doc}`roadmap` for every feature's status.
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

(agents-contract-changes)=
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
  a cluster or reads secret values. `piceli render MODULE:pipeline` renders a
  `Pipeline` with its target's namespace and declared nodes, and build images
  as placeholders; it reads no kubeconfig. `--env NAME` renders one
  environment; `--env A --diff-env B` prints their typed difference (use it
  to show the owner what differs before deploying another environment; see
  {doc}`environments`).
- `piceli codegen crd FILE` (reads the file) and `piceli codegen crd
  --from-cluster --kubeconfig F --context C --crd NAME` (one read of the CRD
  through the explicit context): generate typed models for a custom resource
  (see {doc}`crds`). `--out` writes that one file and replaces it only when
  Piceli generated it. The output depends only on the schema. Never pick the
  kubeconfig or context yourself.
- `piceli release plan` and `piceli release preview`: read the cluster, store
  a pending plan and secret candidates in the spec's `state_dir`, and print the
  plan hash. They never write to the cluster.
- `piceli release diff`: reads the cluster and sends only server-side dry runs
  (`dryRun=All`); prints what `plan` would change, field by field. Writes no
  local state.
- `piceli release status`: reads local state only. Like every `release`
  command it takes `--spec release.toml` or `--spec MODULE:ATTR` (a
  pipeline; see {ref}`agents-pipeline-release`).
- `piceli --version`: prints `piceli <version>` (the JSON form is the
  `version` field of `piceli help-json`).
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
  pushes or applies. Before the images exist it also previews the release
  with placeholder images (`stages.plan.preview`), and refuses with the
  `blocking` objects when the release would need adoption or replacement. With `--ref SOURCE=REV` it also checks the commit out
  into a temporary git worktree, removed before it exits.
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

Never add `--auto-approve` unless the owner has said that this run is an
unattended CI job for this exact spec.

## The approval workflow

1. Run `piceli release plan --spec release.toml`. Keep the JSON from stdout.
2. Show the owner the summary from stderr: every `create`, `adopt`, `delete`
   and `drift` line, the changed fields under each `apply` (all of them are in
   `diffs` in the JSON), and the adoption notes (a takeover removes fields
   other clients wrote). Point out every action with `"cluster_scoped": true`
   (`[cluster-scoped]` in the text): a ClusterRole or ClusterRoleBinding
   grants permissions across the whole cluster. A plan whose `summary` has
   only `no-op` changes nothing.
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
   delivered and which third-party images are mirrored (`deliver  mirror …`,
   `stages.deliver.mirrors` in the JSON), the registry's `adopt`/`replace`
   lines when a live node-loopback registry is taken over (and whether its
   data is kept, `stages.deliver.registry.existing`), the release's
   `create`/`adopt`/`replace`/`apply`/`delete` lines and the checks. While
   the images are not built or delivered, those lines come from `stages.plan.preview`, computed with placeholder images
   (`approvable: false`). The combined hash then approves the build, the
   delivery and a release that adopts, replaces or deletes at most what the
   preview showed. Never pass the preview's `preview_hash` to `--approve`
   (refused with `pipeline-preview-not-approvable`). If the owner wants to
   see the real release plan first, approve `--until deliver`, then plan
   again. A refused plan with `"preview"` and `blocking` means nothing was
   built: report each object's `suggest` flags to the owner.
3. After the owner approves **that combined hash**, run
   `piceli deploy MODULE:ATTR --approve <hash> --json`. Each stdout line is
   one stage event; the last one is the result.
4. Exit `0` means the release is ready and its checks passed. Exit `1`
   means a stage ran but did not succeed (`reason` names it; with
   `rollback_on_failed_checks` the result's `checks.rollback` says what was
   restored). Exit `2` with `pipeline-plan-changed` means something changed
   since the plan: plan and ask again. Exit `2` with
   `pipeline-preview-changed` means the release plan computed after delivery
   adopts, replaces or deletes an object the approved preview did not show;
   nothing was applied: plan again (build and delivery are skipped), show the
   owner the real release plan and ask again.

5. `pipeline-registry-takeover-required` means a registry already runs on
   the node. Never add `adopt=` or `replace=` to the pipeline yourself: tell
   the owner which Deployment holds the port and let them choose
   (`adopt` keeps it and its data in place; `replace` backs it up, deletes
   and recreates it). Registry credentials for `mirror_credentials=` are files
   the owner provides; never create, read or print them.

**Deploying an environment.** When the pipeline declares one target per
environment, every `deploy` and `release --spec MODULE:ATTR` command needs
`--env NAME` (`environment-required` otherwise). Plan, show and approve each
environment separately: the combined hash covers the environment's name and
override values, so one environment's hash never approves another
(`pipeline-plan-changed`). Use exactly the approval command `--plan`
prints (it repeats `--env`). Never choose the environment for the owner.

**Deploying a commit.** When the working tree is shared or dirty, or the
owner asked for a specific commit, add `--ref SOURCE=REV` (or a bare
`--ref REV` when all sources are one repository) to the `--plan` command.
The result's `refs` maps each source to the resolved SHA; show it to the
owner with the plan. Approve with exactly the command `--plan` prints: it
repeats `--ref` with the **SHA**, not the branch, so a branch that moved
after the approval is refused (`pipeline-plan-changed`) instead of deployed.
`--resume` takes no `--ref` (it reuses the run's commits). With `--ref`, the
pipeline module's Python files must match the commit
(`deploy-ref-model-differs`): report it, never commit or stash the owner's
changes yourself. In CI, the approval is a protected environment and the
apply job passes the plan job's `combined_hash` (see {doc}`ci`); an agent
never approves that environment on the owner's behalf.

(agents-pipeline-release)=
### Operating a pipeline's release

The release a pipeline deployed is operated with the ordinary `piceli release`
commands, passing the pipeline as `--spec MODULE:ATTR` (for example
`piceli release status --spec deploy/app.py:pipeline`). They use the same
state directory, release name, target and composition as `piceli deploy` and
never build. The approval rules above apply unchanged:

1. `piceli release status --spec MODULE:ATTR` and
   `piceli release secret show NAME --spec MODULE:ATTR --json` (metadata only)
   need no approval. Never pass `--reveal` unless the owner asked for the
   value, and never log it.
2. To roll back, run `piceli release rollback previous --spec MODULE:ATTR`
   (exit `3`, prints the plan hash; the plan's `source` is the recorded
   `oci-set` image set), show the owner the plan, and after approval run it
   again with `--approve <hash>`.
3. `plan`/`diff`/`apply` with a pipeline refuse with `pipeline-not-delivered`
   when the current sources were not built and delivered yet: use
   `piceli deploy` instead. `pipeline-locked` means a `piceli deploy` of the
   same state directory is running: wait.

## When something fails

1. Read `reason` from the JSON.
2. Run `piceli explain <reason> --json`. The entry has `cause`, `fix` and
   `retry_safe`.
3. If `retry_safe` is `true`, re-run the same command once. Otherwise apply the
   `fix` (it names the flag or command) or report it to the owner.
4. An unknown code (`piceli explain` exits `2` with `unknown-error-code`)
   should not happen for a `conforms` command: report the whole JSON object
   verbatim.
5. `immutable-field-changed` (a Job's pod template, or a StatefulSet's
   service name, pod management, selector or claim templates would change):
   do not add `--replace` yourself. Show the owner the `blocking` entry;
   replacing deletes and recreates the object (a Job runs again). Plan with
   the suggested `--replace Kind/name` only when the owner asks for it, and
   have them approve that plan's hash.

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
- External secret sources (`sops`, `vault`, `aws-secrets-manager` in
  `[secrets.*]`, or `Sops`/`Vault`/`AwsSecret` in a pipeline) are
  configured by the owner. Never read, create, copy or configure their
  credentials yourself: no Vault token files or `VAULT_TOKEN`, no `~/.aws`,
  AWS profiles or keys, no age/PGP keys or `~/.config/sops`, and do not run
  `sops`, `vault` or `aws` to look at a value. `plan` reads the sources
  itself; when it refuses with `secret-source-auth-failed`,
  `secret-source-tool-missing` or `secret-source-not-found`, report the code
  and ask the owner. `secret-source-timeout` and `secret-source-failed` are
  safe to retry once. Do not use `--rotate` on an external source (refused
  with `secret-rotation-refused`): the owner rotates it at the source.
- Error codes never contain secrets or private paths, so they are safe to
  report.
- Never put a secret in a build's smoke check (`smoke.env`, `command`,
  `entrypoint`): the smoke table is printed by `preview` and recorded in the
  receipt and the plan hash. Values that look like secret references are
  refused (`smoke-env-secret`). A `smoke-output-mismatch` excerpt appears on
  stderr and in the build log only; report the code and the `unmatched`
  streams, and quote the excerpt only if the owner asks.

## Cluster access

Piceli's release, observe, operator and artifact commands use only the
kubeconfig file and context they are given (in the spec or with
`--kubeconfig`/`--context`). They never read `~/.kube/config`, `KUBECONFIG` or
the current context: `--context` is required wherever `--kubeconfig` is
accepted (`observe`, `operator`, `artifacts deliver --via-forward`), and a
missing context is a usage error (exit `2`), never a fallback to the file's
`current-context`. Do not change the kubeconfig or context an owner has set
in a spec, and do not pick a context yourself: ask the owner which one to use.

An app with `cluster_rules` (typed RBAC, {doc}`typed_apps`) needs a
kubeconfig user that may list and write ClusterRoles and ClusterRoleBindings.
When planning fails because that discovery is denied, report it to the owner;
do not switch to a more privileged context yourself.

Never add or change `allow_exec`, `exec_sha256` or `exec_pass_env` in a spec
or in a pipeline's `Target`, or pass `--allow-exec`/`--exec-sha256`, yourself: allowing an exec credential plugin runs a program with the owner's
cloud login ({doc}`managed_clusters`). On `exec-auth-not-allowed`,
`exec-pin-mismatch` or `auth-provider-refused`, report the code and wait for
the owner. `exec-plugin-failed` usually means the owner must log in again.
