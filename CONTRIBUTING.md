# Contributing to Piceli

Contributions are welcome: bug reports, documentation fixes, new templates and
larger features. For anything beyond a small fix, please open an
[issue](https://github.com/pynenc/piceli/issues) first so the design can be
discussed. The [roadmap](https://docs.pynenc.org/projects/piceli/en/latest/roadmap.html)
lists the areas where help has the most impact.

## Development setup

Piceli uses [uv](https://docs.astral.sh/uv/) for environments, locking and
builds.

```bash
git clone https://github.com/pynenc/piceli.git
cd piceli
make install              # uv sync --all-extras (creates .venv from uv.lock)
make install-pre-commit   # ruff, mypy, uv-lock and commit-message hooks
make test                 # unit + acceptance tests; never contacts a cluster
```

`make help` lists every target. Before opening a pull request, all of these
must pass:

```bash
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

## Pull requests

1. Fork the repository and create a branch from `main`.
2. Make your change following the guidelines below, and make sure `make lint`
   and `make test` pass.
3. Open the pull request against `main`, with a Conventional Commits title.

Guidelines:

- Add a test with every behaviour change. `make test` must not need a
  cluster: use the fake Kubernetes API in `piceli.testing`.
- Update the documentation and add a line to `docs/changelog.md` when
  behaviour changes. After adding an error code, a command or an option, run
  `make docs-reference`.
- Use [Conventional Commits](https://www.conventionalcommits.org/) for commit
  messages and pull request titles (`feat:`, `fix:`, `docs:`, `test:` …). The
  commit-msg hook checks this.
- Keep examples generic (`my-app`, `my-cluster`, `example`): never name a real
  deployment, cluster, company or person.

The design invariants (side-effect-free imports, no ambient kube context, plan
before apply, fixed error codes, the CLI output contract, secrets never
printed) and a map of the code are in [AGENTS.md](AGENTS.md). The
[contributing guide](https://docs.pynenc.org/projects/piceli/en/latest/contributing/index.html)
in the documentation covers building the docs and page conventions.

## Reporting bugs

Open an [issue](https://github.com/pynenc/piceli/issues) with the Piceli
version (`pip show piceli`), the command you ran, and the
JSON it printed on stdout. Error codes and plans never contain secret values,
so they are safe to share; do not paste kubeconfig files, secret values or
private hostnames.

## Code of conduct

Be kind and constructive. We want Piceli to be a welcoming project for everyone.
