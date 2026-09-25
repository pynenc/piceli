---
name: piceli
description: Deploy and operate Kubernetes apps described as typed Python with Piceli, safely, from an agent. Covers installing piceli, describing an App and a Pipeline, rendering without a cluster, planning and showing the plan to the owner, deploying only with the hash the owner approved (or inside the owner's declared auto_approve policy), checking status and access, diagnosing failures with `piceli explain` and the JSON output contract, resuming interrupted runs and rolling back. Use when a project imports piceli or has a Pipeline or release.toml, or when asked to deploy, plan, roll back or debug a Piceli release.
license: MIT
metadata:
  piceli-version: "0.8"
---

# Piceli

Piceli models Kubernetes infrastructure as typed Python: an `App` declares
workloads, services and secrets; a `Pipeline` deploys it from source with
`piceli deploy` (inputs → build → deliver → plan → apply → checks). Every
change is **planned first** and runs only with the owner's approval of that
plan's **hash**. Every command in this file runs in CI against the released
wheel (with a fake Kubernetes API), so the commands and fields shown here
exist in the version below.

## Rules (never break these)

1. **Never use `~/.kube/config`, `KUBECONFIG` or the current context.** Use
   only the kubeconfig file and context the owner names (in the pipeline's
   `Target` or with `--kubeconfig`/`--context`). Do not pick a context.
2. **Never apply without the owner.** Run `deploy --approve <hash>`,
   `release apply --approve <hash>` and `release rollback … --approve <hash>`
   only with the hash the owner approved after seeing that plan. Never add
   `--auto-approve` unless the owner said this run is an unattended CI job for
   this exact pipeline or spec.
3. **Never pass `--allow-exec`/`--exec-sha256` or add `allow_exec`,
   `exec_sha256`, `exec_pass_env` to a target**: that runs a program with the
   owner's cloud login. On `exec-auth-not-allowed`, report it and wait.
4. **Never print, read or copy secret values**: not from the state
   directory, not with `kubectl get secret -o yaml`, not with `--reveal`.
   Plans, receipts and errors carry digests; error codes are safe to report.
5. **Never add or widen an `auto_approve` policy**, and never add `adopt=`,
   `replace=`, `--adopt`, `--replace` or `--skip-checks` yourself. Show the
   owner the refusal and let them decide.

## Requirements

- `piceli` **0.8.x**, CPython 3.12+: `pip install "piceli>=0.8,<0.9"`.
- The owner gives you a kubeconfig **file** and a **context** (here through
  `SHOP_KUBECONFIG` and `SHOP_CONTEXT`). A pipeline with `Build` objects also
  needs Docker; the example below uses pinned images only.

```sh
python scripts/check_install.py
```

It checks that `piceli --version` is 0.8.x and that `piceli help-json` has
every command and option this skill uses. `piceli help-json` is the full
reference: every command's side effects, whether it needs approval and
whether it is safe to retry.

## How it fits together

- **Output contract.** stdout carries machine output (one JSON object, or
  one JSON line per event with `--json`); stderr carries human text (the
  summary and the command to approve). Exit codes: `0` success, `1` ran but
  did not succeed, `2` rejected before any change, `3` approval required
  (nothing was executed). A refusal is
  `{"state": "rejected", "reason": "<code>", ...}`.
- **Plan, then approve the hash.** `piceli deploy MODULE:ATTR --plan` prints
  a `combined_hash` covering every stage, the pipeline (including its
  approval policy) and the commits. `--approve <hash>` runs exactly that plan;
  anything that changed since is refused (`pipeline-plan-changed`).
- **State.** The pipeline's `state_dir` holds the run journal, receipts, the
  release catalog and the private secret store. Do not open or edit it.
- The scripts in `scripts/` wrap these commands and print only what a person
  needs; run any `piceli` command yourself when you need more (`--json`).

## 1. Describe an App and a Pipeline

[`examples/shop.py`](examples/shop.py) is a complete pipeline: two Deployments
from pinned images, a generated secret, services, and the owner's approval
policy. Its target comes from the owner:

```python
target = Target.kubeconfig(
    KUBECONFIG,
    context=CONTEXT,
    namespace=os.environ.get("SHOP_NAMESPACE", "shop"),
    # "loopback-http" only for a local test API (piceli.testing); default https.
    transport=os.environ.get("SHOP_TRANSPORT", "https"),
)
```

Images are pinned by digest (`repo@sha256:…`) or built with
`Build.spec("build.toml")` and used as `images["name"]`; a pipeline with a
build also needs `deliver=` (`NodeLoopbackRegistry()`, `NodeImport()` or
`Registry(url)`). Secrets are generated from `Secrets(...)` and referenced with
`secrets.ref(...)`; their values never appear in plans.

Render the manifests without a cluster (reads no kubeconfig):

```sh
piceli render examples/shop.py:pipeline
```

## 2. Plan and ask the owner

```sh
python scripts/plan.py examples/shop.py:pipeline
```

It runs `piceli deploy examples/shop.py:pipeline --plan --json`, which writes
only the state directory, and prints every release change
(`create`/`apply`/`adopt`/`replace`/`delete`; the last three and
cluster-scoped objects are flagged), the `combined_hash` and the exact
approval command. Before images exist the release part is a **preview with
placeholder images**; never approve its `preview_hash`.

Show the owner the whole summary (stderr has the build, delivery and check
lines too) and **wait for them to approve that hash**.

## 3. Deploy with the approved hash

```sh
python scripts/deploy.py examples/shop.py:pipeline --approve <combined hash>
```

Exit `0`: the release is ready and its checks passed. Exit `2` with
`pipeline-plan-changed`: something changed since the plan; plan again and ask
again. Exit `1`: a stage ran but failed; see section 6.

With `--env NAME` (a pipeline with one target per environment) or
`--ref SOURCE=REV`, repeat exactly what the plan's approval command shows.

## 4. Status and access

```sh
python scripts/status.py examples/shop.py:pipeline
```

It runs `piceli status examples/shop.py:pipeline --json`: `state` is `up`,
`degraded`, `down` or `unknown`, one line per workload with its health, and
each declared access URL. Exit `1` means not up. `piceli access MODULE:ATTR`
forwards the declared ports to `127.0.0.1` (a long-running process: ask the
owner first). When a port is taken by Piceli's own leftover forward or server
for the same app, the conflict says so (`holder`); `piceli access stop --stale
MODULE:ATTR` stops only those processes: ask the owner first.

## 5. The owner's approval policy

The owner may declare in the pipeline which plans may run without them:

```python
pipeline = Pipeline(
    ...,
    auto_approve=ApprovalPolicy(allow={"create", "apply", "no-op"}, max_objects=10),
)
```

(`[release] auto_approve = { allow = [...], deny = [...], max_objects = N }`
in a `release.toml`.) It is reviewed code and part of the plan hash. Then:

```sh
python scripts/deploy.py examples/shop.py:pipeline --approve-if-policy
```

runs `piceli deploy … --approve-if-policy`: the plan runs only when every
action is inside the policy (the result has `"approved_by": "policy"`).
Otherwise nothing runs, the exit code is `3` with
`"reason": "approval-policy-exceeded"` and `policy.violations` listing each
action outside it; show the plan to the owner and ask for the hash. `delete`,
`replace` and `adopt` are never inside a policy; `cluster_scoped` objects and
`drift` are outside unless the owner allowed them. There is no command-line
flag that sets or widens a policy (`approval-policy-missing` without one).
`piceli release apply --spec … --approve-if-policy` does the same for a
release.

## 6. Diagnose a failure

Every refusal or failure names a fixed code in `reason`:

```sh
python scripts/diagnose.py <code>
```

It runs `piceli explain <code> --json` and prints the `cause`, the `fix` and
whether re-running the same command is safe (`retry_safe`). If `retry_safe`
is true, re-run once; otherwise apply the fix or report the code to the
owner. `scripts/deploy.py` and the other scripts do this automatically on a
failure. `python scripts/diagnose.py --result out.json` reads a saved result
(with its `blocking` objects and policy violations). An unknown code should
not happen: report the whole JSON object.

A workload that cannot start (crash loop, image pull error, bad config)
fails the apply at once with `pipeline-apply-crashloop` (`apply-crashloop`
from `piceli release`) and a `diagnosis`: per workload, each container's
reason, exit code, restarts and a redacted log tail. The scripts print one
line per cause; report them to the owner and do not retry unchanged.
`piceli release status --spec MODULE:ATTR --run <run id> --json` shows them
again later. Every deploy run also writes a summary (`summary.json` in the
result; `piceli runs MODULE:ATTR --json` lists past runs): read it to learn
what a run did or why it failed.

Common codes: `pipeline-plan-changed` (plan again), `pipeline-locked` (another
deploy runs; wait), `immutable-field-changed` (a replace is needed: ask the
owner), `pipeline-registry-takeover-required` (ask the owner),
`exec-auth-not-allowed` (ask the owner), `check-failed` (report the failed
checks; an automatic rollback may already have run).

## 7. Resume an interrupted or failed run

```sh
python scripts/deploy.py examples/shop.py:pipeline --resume
```

It runs `piceli deploy … --resume`: the latest interrupted or failed run
continues at its failed stage with the approval it already had; finished
stages are reused and an interrupted apply resumes with the same grant.
Never plan and approve a new run instead of resuming one. Nothing to resume is
`pipeline-nothing-to-resume`.

## 8. Roll back

```sh
python scripts/rollback.py examples/shop.py:pipeline
```

It runs `piceli release rollback previous --spec examples/shop.py:pipeline`:
the rollback is planned against the live cluster (exit `3`, nothing
applied) and its changes and `plan_hash` are printed. Show them to the owner;
after they approve:

```sh
python scripts/rollback.py examples/shop.py:pipeline --approve <plan hash>
```

A rollback restores what the release declares (the recorded images and
objects), never data or external side effects. A pipeline with
`rollback_on_failed_checks=True` rolls back by itself when checks fail.

## Safe to run without asking

`piceli explain`, `piceli help-json`, `piceli --version`, `piceli render`,
`piceli deploy … --plan`, `piceli status … --json`, `piceli release status`,
`piceli release plan|diff`, `piceli runs … --json`, `piceli cache status`,
`piceli doctor`, `python scripts/check_install.py`,
`python scripts/plan.py …`, `python scripts/status.py …`,
`python scripts/diagnose.py …`, `python scripts/rollback.py …` without
`--approve` (it only plans).

## Never do without the owner's approval

`piceli deploy … --approve`, `--resume` of a run the owner did not approve,
`piceli release apply|rollback … --approve`, `piceli release check` on a spec
you did not write, `piceli access`, `piceli access stop --stale`,
`piceli publish … --approve` (pushes manifests for Flux or Argo CD),
`piceli cache prune`, `piceli state import`, and every
`artifacts deliver|build-spec run|execute-command|import-local`. Full rules:
<https://docs.pynenc.org/projects/piceli/en/stable/agents.html>.
