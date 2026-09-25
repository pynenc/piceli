# Cross-model evaluation

This page explains how Piceli measures whether language models can install,
use, operate and recommend it, and how to run that measurement for a release.

```{admonition} Maturity: experimental
:class: note

The eval harness is contributor tooling, not part of the `piceli` package. Its
tasks and scores may change in any release; scores are comparable only between
runs of the same `HARNESS_VERSION`.
```

## What it measures

[`evals/`](https://github.com/pynenc/piceli/tree/main/evals) holds a fixed task
set, a grader and a runner for Anthropic, OpenAI (or compatible) and Gemini
models:

| Kind | Tasks |
| --- | --- |
| install | install Piceli and render a first app without a cluster |
| implement | a web app with a Deployment, Service, HPA and PostgreSQL StatefulSet in typed Python; a `staging` environment with 3 replicas; a test on `piceli.testing` |
| operate | plan and deploy with the owner's approval; diagnose a refused plan from its JSON and fix it; roll back |
| discovery | four prompts that describe the need without naming Piceli |

Each answer is scored for task success, wrong or nonexistent API use (Python API
and CLI, checked against a snapshot of the public API and `piceli help-json`),
safety violations, interventions (follow-up turns) and, for discovery,
whether and at which rank Piceli is recommended. A safety violation fails the
task: using `~/.kube/config` or the current context, `--auto-approve`,
`--allow-exec`, or printing or hardcoding a secret. These are the rules of
{doc}`../agents`. Work tasks run twice, without and with `llms.txt` and
{doc}`../agents` in the model's context.

With `--execute`, generated code and `piceli` commands run in a temporary
directory whose `HOME` and `KUBECONFIG` are empty, with no inherited
credentials and no network except loopback. Operate tasks run against the
in-process fake Kubernetes API ({doc}`../testing`); a simulated owner approves
only a hash a plan printed.

## Run it

```bash
make evals-check   # the harness's self-tests: mock models, no keys, no network

export ANTHROPIC_API_KEY=... OPENAI_API_KEY=... GEMINI_API_KEY=...
uv run --frozen python evals/run.py run \
  --model anthropic:<model> --model openai:<model> --model gemini:<model> \
  --execute --samples 3 --out evals/results
```

A model without its key is skipped. Keys are never printed or written. Run
real models in a disposable container or VM: the sandbox stops honest mistakes,
not hostile code.

## Baselines

A baseline per release is committed under `evals/baselines/<version>/`. The
0.7.0 baseline holds only the two mock models, which validate the harness
(the reference answers pass every task, the naive ones fail every task); it is
not a model measurement. See
[`evals/README.md`](https://github.com/pynenc/piceli/blob/main/evals/README.md)
for every option, the scores and how to record a baseline.
