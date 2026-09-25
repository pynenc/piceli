# Deploy from CI with an approval step

This page shows how to run `piceli deploy` from GitHub Actions: on every push
the pushed commit is planned, a reviewer approves that exact plan, and a
second job applies it, from the same commit, with the same inputs, **on any
runner**: the jobs share no disk and no build cache, only the cluster (where
the deployment state lives, see {doc}`state`) and the plan file.

```{admonition} Maturity: preview
:class: note

The recipe uses `piceli deploy --ref` (preview, see {ref}`deploy-ref`). The
sample workflow is run by Piceli's own tests against a fake cluster, so its
commands match the CLI of this version.
```

## How it works

| Job | Runs | Approval |
| --- | --- | --- |
| `plan` | `piceli deploy "$PIPELINE" --ref "$GITHUB_SHA" --plan --out deploy-plan.json --json`; publishes the plan (job summary and a `deploy-plan` artifact with the plan file) and its `combined_hash` as a job output | None: `--plan` never changes the cluster, a registry or a node (it writes the shared state) |
| `apply` | `piceli deploy --apply deploy-plan.json --approve "$COMBINED_HASH" --json` | A protected GitHub environment: required reviewers approve after reading the plan |
| `resume` (manual) | `piceli deploy "$PIPELINE" --resume --json` | The same environment |

The approval is bound twice. GitHub lets `apply` start only after a reviewer
approved the `production` environment for this workflow run, and Piceli runs
it only when the plan file's hash is the approved one **and** planning again
on the apply runner, against live state, gives **the same combined hash**. The hash covers the commit (`--ref` pins it by SHA), the
staged build inputs, the builders, the delivery, the release's actions (or
the rendered model while the images are not built yet), the checks and the
target. When anything changed in between (a new object in the cluster,
another commit, a different model), `apply` is refused with
`pipeline-plan-changed` (exit `2`) and nothing runs: push again or re-run
the workflow to get a new plan.

When the commit's images were never built, the plan still shows the
release as a **preview** with placeholder images: what it would create,
adopt, replace or delete, and any blocking objects (see
{ref}`deploy-preview`). The hash then binds the build inputs, the rendered
model and the preview's adopt/replace/delete set. The real release plan is
computed after the build and delivery, under the same approval, and it is
refused (`pipeline-preview-changed`) if it would adopt, replace or delete
anything the reviewed preview did not show.

## Prerequisites

- A pipeline module whose builds declare their sources in an `inputs.toml`
  (see {doc}`source_identity`), for example `deploy/app.py`, with **shared
  state** (`state="cluster"`). It reads the kubeconfig path and the state
  directory (a per-job working copy) from the environment and names its
  context explicitly:

  ```python
  import os

  from piceli import Pipeline, Target

  target = Target.kubeconfig(
      os.environ.get("DEPLOY_KUBECONFIG", "cluster.kubeconfig"),
      context="my-cluster",  # explicit; never the current context
      namespace="my-app",
  )
  pipeline = Pipeline(
      app,
      target,
      build=images,
      deliver=...,
      state="cluster",  # journal, catalog, secrets and lock in the namespace
      state_dir=os.environ.get("DEPLOY_STATE_DIR", ".piceli-deploy"),
  )
  ```

- **Self-hosted runners** that reach the cluster's API server and have
  Docker with `buildx` (and `kubectl` for a node-loopback registry), with a
  label such as `deploy-my-app`. Any of them can run any job.
- **A repository secret** `DEPLOY_KUBECONFIG` with a kubeconfig that holds
  only the context the pipeline declares. The plan needs it too: it reads the
  cluster, runs server-side dry runs (the same rights as the apply) and
  writes the shared state. Besides the release's own objects it needs `get`,
  `create`, `patch` and `delete` on `secrets` and `leases` in the namespace
  (see {doc}`state`).
- **An environment** `production` (Settings → Environments) with required
  reviewers, and "Prevent self-review" if the pusher must not approve alone.

## Steps

1. Copy `examples/ci/github-actions-deploy.yml` to
   `.github/workflows/deploy.yml` in your repository and adapt `PIPELINE`,
   the runner labels and the concurrency group:

   ```{literalinclude} ../examples/ci/github-actions-deploy.yml
   :language: yaml
   ```

2. Push to `main`. The `plan` job's summary shows the plan
   (`inputs   ref shop: <sha> = commit <sha>`, the builds, the deliveries,
   the release's changes and the checks) and the combined hash; the
   `deploy-plan` artifact holds `deploy-plan.json` (the portable plan file:
   hash, commits, stages, target identity, receipts), `plan.json` (the
   result object) and `plan.txt`.
3. A reviewer reads the summary and approves the `production` deployment of
   the run. The `apply` job downloads the artifact, and
   `piceli deploy --apply deploy-plan.json` checks the file against the
   approved hash, plans again from the same commits against live state,
   checks the hash, and runs the stages. Its JSON lines are kept as the
   `deploy-result` artifact.
4. Expected output: the `apply` job ends with
   `deploy ready: release <name>` on stderr and a last JSON line with
   `"state": "ready"`.

## Secrets and the kubeconfig

- The kubeconfig is written with `umask 077` (mode `0600`) to
  `$RUNNER_TEMP`, from an environment variable, never from a command line
  argument; nothing echoes it, and a final `if: always()` step deletes it.
  The model gets only its path (`DEPLOY_KUBECONFIG`).
- Piceli never prints secret values, and its errors carry codes, not server
  messages, so the plan text is safe to publish in a job summary.
- Never pass `--allow-exec` or an ambient `KUBECONFIG`; an exec credential
  plugin must be allowed in the `Target` itself (see {doc}`managed_clusters`).
- Do not run this workflow for pull requests from forks: a self-hosted runner
  with cluster credentials must only run code from branches you trust.

## Self-hosted runners and private clusters

- Put the runner inside the network that reaches the cluster's API server
  (or the node, for `NodeImport` and `NodeLoopbackRegistry`); no inbound
  access to the cluster is needed from GitHub.
- `plan`, `apply` and `resume` may run on **different runners** (ephemeral
  ones too). Each job gets a fresh working copy of the state in
  `$RUNNER_TEMP` (removed at the end of the job); the state itself is in the
  namespace. An image built and delivered by an earlier run is found by its
  delivery receipt and its digest in the registry, so the apply runner does
  not build it again; an image that was never delivered is built by the
  apply job, from the pinned commit.
- `--ref "$GITHUB_SHA"` makes the build independent of the runner's
  workspace: even a runner that reuses a dirty workspace builds the exact
  commit, from a temporary worktree that is removed at the end (also when the
  job is cancelled: `SIGTERM` is handled like `Ctrl-C`). The checkout's
  `deploy/*.py` must equal the commit (`deploy-ref-model-differs` otherwise),
  which `actions/checkout` guarantees.
- A shallow checkout (the default `fetch-depth: 1`) is enough: the commit is
  `$GITHUB_SHA` itself.

## Concurrency

`concurrency: {group: deploy-my-app, cancel-in-progress: false}` queues
deploys of one target instead of cancelling a running one. Piceli also holds
the **release lock** (a Lease in the namespace) for every command: a second
deployer of the same release, from any runner, workflow or laptop, is
refused with `pipeline-locked` (exit `2`, with the holder and when its lease
expires) rather than racing the first. A cancelled job frees the lock; a
runner that died frees it when its lease expires (`state_lease_seconds`,
default 60). Use one group per target (cluster, namespace and app).

## Resume

When `apply` fails at a stage (a registry was down, the release did not
become ready), fix the cause and run the workflow by hand with **resume**
checked (Actions → deploy → Run workflow). The `resume` job runs
`piceli deploy "$PIPELINE" --resume --json`: it continues the same run at its
failed stage with the approved plan and **the commits the run recorded**, not
the branch's newer head, on whichever runner picks it up (the run journal is
in the shared state). It needs no new plan, but the environment approval
still applies. Re-running the failed `apply` job instead plans again and
usually stops with `pipeline-plan-changed`, because the first attempt already
built or delivered something.

## If it fails

| Output | Meaning | Next step |
| --- | --- | --- |
| `apply` exit `2`, `pipeline-plan-changed` | Something changed between the plan and the approval | Re-run the workflow (new plan, new approval) |
| `apply` exit `1` with `reason` and `stage` | A stage ran but did not succeed | Fix it, then run the workflow with **resume** |
| `pipeline-locked` (with `lock.holder`) | Another deploy of the same release runs | Wait; keep one concurrency group per target |
| `deploy-plan-file-mismatch` | `COMBINED_HASH` is not the plan file's hash, or the artifact is from another pipeline | Use the artifact and output of the same workflow run |
| `deploy-plan-target-mismatch` | The kubeconfig reaches another cluster than the plan did | Fix the `DEPLOY_KUBECONFIG` secret, re-run the workflow |
| `state-access-denied` | The kubeconfig may not write the shared state | Grant the Secret and Lease verbs (see {doc}`state`) |
| `deploy-ref-unknown` | The runner's clone does not have the commit | Check the checkout step (`ref`, `fetch-depth`) |
| `deploy-ref-model-differs` | The checkout's pipeline module is not the commit's | Check out `$GITHUB_SHA` (the default) |
| `pipeline-nothing-to-resume` | The last run finished; nothing to resume | Push or re-run the workflow for a new plan |

Every code is explained by `piceli explain <code>` and in
{doc}`reference/errors`.
