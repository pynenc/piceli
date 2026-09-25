# Deploy from CI with an approval step

This page shows how to run `piceli deploy` from GitHub Actions: on every push
the pushed commit is planned, a reviewer approves that exact plan, and a
second job applies it, from the same commit, with the same inputs.

```{admonition} Maturity: preview
:class: note

The recipe uses `piceli deploy --ref` (preview, see {ref}`deploy-ref`). The
sample workflow is run by Piceli's own tests against a fake cluster, so its
commands match the CLI of this version.
```

## How it works

| Job | Runs | Approval |
| --- | --- | --- |
| `plan` | `piceli deploy "$PIPELINE" --ref "$GITHUB_SHA" --plan --json`; publishes the plan (job summary and a `deploy-plan` artifact) and its `combined_hash` as a job output | None: `--plan` never changes the cluster, a registry or a node |
| `apply` | `piceli deploy "$PIPELINE" --ref "$GITHUB_SHA" --approve "$COMBINED_HASH" --json` | A protected GitHub environment: required reviewers approve after reading the plan |
| `resume` (manual) | `piceli deploy "$PIPELINE" --resume --json` | The same environment |

The approval is bound twice. GitHub lets `apply` start only after a reviewer
approved the `production` environment for this workflow run, and Piceli runs
it only when the new plan has **the same combined hash** as the one the
reviewer read. The hash covers the commit (`--ref` pins it by SHA), the
staged build inputs, the builders, the delivery, the release's actions (or
the rendered model while the images are not built yet), the checks and the
target. When anything changed in between (a new object in the cluster,
another commit, a different model), `apply` is refused with
`pipeline-plan-changed` (exit `2`) and nothing runs: push again or re-run
the workflow to get a new plan.

When the commit's images were never built, the plan lists the release as
`after delivery`: the hash then binds the build inputs and the rendered
model, and the release's object changes are planned after the build and
delivery, under the same approval (see {doc}`deploy`).

## Prerequisites

- A pipeline module whose builds declare their sources in an `inputs.toml`
  (see {doc}`source_identity`), for example `deploy/app.py`. It reads the
  kubeconfig path and the state directory from the environment and names its
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
      state_dir=os.environ.get("DEPLOY_STATE_DIR", ".piceli-deploy"),
  )
  ```

- **A self-hosted runner** that reaches the cluster's API server and has
  Docker with `buildx` (and `kubectl` for a node-loopback registry), with a
  label such as `deploy-my-app`. Both jobs run on it.
- **A persistent state directory** on that runner, outside the checkout
  (`DEPLOY_STATE_DIR`, for example `/var/lib/piceli/my-app`). It holds the
  run journal, the release catalog and the private secret store; a fresh
  directory on every run would generate new secrets and lose the release
  history. Back it up like any other deployment state.
- **A repository secret** `DEPLOY_KUBECONFIG` with a kubeconfig that holds
  only the context the pipeline declares. The plan needs it too: it reads the
  cluster and runs server-side dry runs, which need the same rights as the
  apply.
- **An environment** `production` (Settings → Environments) with required
  reviewers, and "Prevent self-review" if the pusher must not approve alone.

## Steps

1. Copy `examples/ci/github-actions-deploy.yml` to
   `.github/workflows/deploy.yml` in your repository and adapt `PIPELINE`,
   `DEPLOY_STATE_DIR`, the runner labels and the concurrency group:

   ```{literalinclude} ../examples/ci/github-actions-deploy.yml
   :language: yaml
   ```

2. Push to `main`. The `plan` job's summary shows the plan
   (`inputs   ref shop: <sha> = commit <sha>`, the builds, the deliveries,
   the release's changes and the checks) and the combined hash; the
   `deploy-plan` artifact holds `plan.json` (the result object, with
   `combined_hash`, `refs` and every stage's plan) and `plan.txt`.
3. A reviewer reads the summary and approves the `production` deployment of
   the run. The `apply` job plans again from the same commit, checks the
   hash, and runs the stages. Its JSON lines are kept as the `deploy-result`
   artifact.
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
- Run `plan` and `apply` on **the same runner** (one label, one machine):
  they share the local image store (a build is reused when its staged files
  are unchanged) and the state directory. A plan job on another machine sees
  different local images and would produce a different combined hash.
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
a lock on the state directory: a second `piceli deploy` of the same
pipeline is refused with `pipeline-locked` (exit `2`) rather than racing the
first. Use one group per target (cluster, namespace and app).

## Resume

When `apply` fails at a stage (a registry was down, the release did not
become ready), fix the cause and run the workflow by hand with **resume**
checked (Actions → deploy → Run workflow). The `resume` job runs
`piceli deploy "$PIPELINE" --resume --json`: it continues the same run at its
failed stage with the approved plan and **the commits the run recorded**, not
the branch's newer head. It needs no new plan, but the environment approval
still applies. Re-running the failed `apply` job instead plans again and
usually stops with `pipeline-plan-changed`, because the first attempt already
built or delivered something.

## If it fails

| Output | Meaning | Next step |
| --- | --- | --- |
| `apply` exit `2`, `pipeline-plan-changed` | Something changed between the plan and the approval | Re-run the workflow (new plan, new approval) |
| `apply` exit `1` with `reason` and `stage` | A stage ran but did not succeed | Fix it, then run the workflow with **resume** |
| `pipeline-locked` | Another deploy of the same state directory runs | Wait; keep one concurrency group per target |
| `deploy-ref-unknown` | The runner's clone does not have the commit | Check the checkout step (`ref`, `fetch-depth`) |
| `deploy-ref-model-differs` | The checkout's pipeline module is not the commit's | Check out `$GITHUB_SHA` (the default) |
| `pipeline-nothing-to-resume` | The last run finished; nothing to resume | Push or re-run the workflow for a new plan |

Every code is explained by `piceli explain <code>` and in
{doc}`reference/errors`.
