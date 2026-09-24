# Contributing to Piceli

Contributions are welcome: bug reports, documentation fixes, new templates and
larger features. For anything beyond a small fix, please open an issue first so
the design can be discussed. The {doc}`../roadmap` lists the areas where help has
the most impact.

## Development setup

Piceli uses [uv](https://docs.astral.sh/uv/) to manage Python environments.

```bash
git clone https://github.com/pynenc/piceli.git
cd piceli
make local-test-env        # create .venv with hash-locked test dependencies
make test-local-executor   # unit tests + fault-injected API acceptance tests
```

These tests never contact a real cluster. The acceptance suite runs the real
Kubernetes client against an in-process fake API server that injects faults.

To run a subset directly:

```bash
.venv/bin/python -m pytest tests/unit
.venv/bin/python -m pytest tests/acceptance
```

`tests/integration` needs a real, disposable cluster (for example
[kind](https://kind.sigs.k8s.io/) or minikube) reachable through your current
kubeconfig context.

## Guidelines

- Keep imports and planning side-effect free: no clients, kubeconfig access or
  network calls at import time or in pure planning code.
- Add tests with every change. Template changes need unit tests that check the
  generated manifest.
- Public APIs are typed and checked with mypy.
- Use [Conventional Commits](https://www.conventionalcommits.org/) for commit
  messages (`feat:`, `fix:`, `docs:` …).
- Update the documentation and the changelog when behaviour changes.

```{toctree}
:maxdepth: 1

docs
```
