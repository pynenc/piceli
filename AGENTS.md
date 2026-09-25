# Working on Piceli

This file is for coding agents and new contributors changing Piceli itself.
To *use* Piceli from an agent (which commands are safe, how approvals work,
how to read errors), read [docs/agents.md](docs/agents.md) instead.

Piceli is a public, consumer-agnostic library: Kubernetes infrastructure as
typed Python that you model, plan, apply and observe. It depends on no
particular deployment.

## Setup and checks

Piceli uses [uv](https://docs.astral.sh/uv/). Always run tools through the
locked environment.

```sh
make install              # uv sync --all-extras (creates .venv from uv.lock)
make test                 # unit + acceptance tests; never contacts a cluster
make typecheck            # mypy on piceli/
make lint                 # every pre-commit hook: ruff check + format, uv lock, YAML/TOML
make docs                 # Sphinx with warnings as errors
make docs-reference       # regenerate docs/reference/{errors,cli}.md from code
make evals-check          # self-tests of the cross-model eval harness (evals/)
make help                 # every target
```

Before you commit, all of these must pass:

```sh
uv run --frozen pytest
uv run --frozen mypy
uv run --frozen ruff check . && uv run --frozen ruff format --check .
uv run --frozen --group docs sphinx-build -W --keep-going -b html docs docs/_build/html
```

`make test-integration` runs the kind tests against the cluster named by
`PICELI_KIND_KUBECONFIG` and `PICELI_KIND_CONTEXT` (plus `PICELI_KIND_NODE`,
the node container, for the node-delivery tests); without them they are
skipped. They never use the current context. Create a disposable cluster with
a supported node image (see `.github/kind-nodes.json`), for example
`kind create cluster --name piceli-it --kubeconfig /tmp/it.kubeconfig`, then
`PICELI_KIND_KUBECONFIG=/tmp/it.kubeconfig PICELI_KIND_CONTEXT=kind-piceli-it
PICELI_KIND_NODE=piceli-it-control-plane make test-integration`. Never point
them at a shared cluster.

Commits follow [Conventional Commits](https://www.conventionalcommits.org/)
(`feat:`, `fix:`, `docs:`, `test:` …). Behaviour changes get a line in
`docs/changelog.md`. A new command, option or error code also needs
`make docs-reference`, and an entry in `docs/agents.md` and `llms.txt` when it
is safe to run or needs approval. A change to the public Python API or the CLI
also needs `uv run --frozen python evals/run.py api-surface --write` (the eval
harness checks model answers against that snapshot; `make evals-check` fails
until it is refreshed).

## Invariants

These are enforced by tests or review. Do not weaken them.

1. **Imports are side-effect free.** Importing any `piceli` module must not
   read a kubeconfig, build a client, open a network connection, spawn a
   process or write a file. Work happens in functions that receive explicit
   inputs.
2. **Never an ambient kube context.** Library code and new commands take an
   explicit kubeconfig file and context; they never fall back to
   `~/.kube/config`, `KUBECONFIG` or `current-context`.
3. **Plan before apply.** Anything that changes a cluster, registry, node or
   runs code first produces a plan (or preview) with a hash or digest, and
   executes only with an explicit approval of that exact hash.
4. **Fixed error codes, never private detail.** Refusals print
   `{"state": "rejected", "reason": "<code>"}`. Codes never include paths,
   secret values, process output or server messages. Every code is registered
   in `piceli/errors.py`; a test scans the source and fails on an unregistered
   code. New codes need `code`, `title`, `cause`, `fix`, `retry_safe` and
   `area`, then `make docs-reference`.
5. **CLI output contract.** Machine output (one JSON object, or JSON lines) goes
   to stdout; human text goes to stderr. Exit codes: `0` success, `1` ran but
   did not succeed, `2` rejected, `3` approval required. New commands use
   `piceli/cli_contract.py` (`emit_json`, `say`, `reject`) and get an entry in
   its `COMMANDS` table (side effects, approval, retry safety); a test fails
   when a command has none.
6. **No consumer names.** Never name a real deployment, cluster, company or
   person in code, tests, examples or docs. Use generic names: `my-app`,
   `my-cluster`, `shop`, `api`, `cache`, `example`.
7. **Secrets are never printed.** Plans, receipts, logs and errors carry
   digests and pointers, not values.

## Tests

- `tests/unit`: pure logic. `tests/acceptance`: the real Kubernetes client
  against an in-process fake API server with fault injection (the public
  `piceli.testing`; `tests/acceptance/fake_api.py` re-exports it). Both run in
  `make test` and must not need a cluster.
- `tests/conftest.py` has an **autouse cluster guard**: outside
  `tests/integration`, `KUBECONFIG` points at a missing file and
  `load_kube_config` / `load_incluster_config` raise. A test that reaches for a
  real cluster fails loudly; mock the client or use the fake API instead.
- `tests/integration`: real `kind` cluster via an explicit kubeconfig.
- Every behaviour change comes with a test. Docs generated from code
  (`docs/reference/*.md`) have a staleness test.

## Where things live

| Concern | Location |
| --- | --- |
| CLI entry point | `piceli/__main__.py` (Typer app in `piceli/k8s/cli/`, argparse `artifacts` in `piceli/artifacts/cli.py`) |
| CLI contract, command metadata, `help-json` | `piceli/cli_contract.py`, `piceli/k8s/cli/contract.py` |
| Error-code registry, `explain` | `piceli/errors.py` |
| Generated reference pages | `piceli/reference_docs.py` → `docs/reference/` |
| Templates (typed object model) | `piceli/k8s/templates/` |
| Discovery, plans, executor, journal, sessions | `piceli/k8s/ops/` |
| Releases from a spec | `piceli/k8s/release_spec.py`, `release_runner.py`, `release_secrets.py`, `piceli/k8s/cli/release.py` |
| Deploy from source (`Pipeline`, `piceli deploy`, plan files) | `piceli/pipeline/` |
| Shared state, release lock, `piceli state` | `piceli/state/`, `piceli/k8s/cli/state.py` |
| Post-deploy checks (`Checks`, `release check`) | `piceli/checks/` |
| Builds, source identity, image delivery | `piceli/artifacts/` |
| Typed apps (`App`, `app.override`) | `piceli/app/` |
| Import live objects or YAML as a typed module (`piceli import`) | `piceli/importing/`, `piceli/k8s/cli/importing.py` |
| Public fake Kubernetes API for tests | `piceli/testing/` |
| Observe / operator UI and REST API | `piceli/k8s/observe*.py`, `piceli/k8s/operator*.py` |
| Access and status (`piceli access`, `piceli status`) | `piceli/app/access.py`, `piceli/k8s/access.py`, `piceli/k8s/port_owner.py`, `piceli/k8s/cli/access.py` |
| Examples (run in CI where possible) | `examples/` |
| Cross-model eval of agents using Piceli (tasks, grader, sandbox, baselines) | `evals/` (see `evals/README.md`) |
| Docs (Sphinx + MyST) | `docs/`; agent entry points `llms.txt`, `docs/agents.md` |
| First-run path for new users | `README.md` quick start (included from `examples/readme/`: edit the files, then `python scripts/readme_examples.py sync`; run against the fake API by `tests/acceptance/test_readme_quickstart.py`), `docs/getting_started/index.md` (includes the same `app.py`) and the taste in `docs/index.md`; keep the three in sync |
| Same app in Helm, Kustomize, cdk8s and Pulumi | `examples/comparisons/`, `tests/unit/comparisons/` (CI job `comparisons`), `docs/comparisons.md`, `docs/when_to_use.md` |
| Human contributor guide | `CONTRIBUTING.md`, `docs/contributing/` |
