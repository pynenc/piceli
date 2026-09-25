# Contributing to Piceli

Contributions are welcome: bug reports, documentation fixes, new templates and
larger features. For anything beyond a small fix, please open an issue first so
the design can be discussed. The {doc}`../roadmap` lists the areas where help has
the most impact.

## Development setup

Piceli uses [uv](https://docs.astral.sh/uv/) for environments, locking and builds.

```bash
git clone https://github.com/pynenc/piceli.git
cd piceli
make install              # uv sync --all-extras (creates .venv from uv.lock)
make install-pre-commit   # ruff, mypy, uv-lock and commit-message hooks
make test                 # unit + fault-injected API acceptance tests
```

`make test` never contacts a real cluster. The acceptance suite runs the real
Kubernetes client against an in-process fake API server that injects faults.

Other targets (`make help` lists them all):

| Target | What it does |
| --- | --- |
| `make lint` | Every pre-commit hook on all files (ruff lint + format, uv lock check, YAML/TOML) |
| `make typecheck` | mypy on the `piceli` package |
| `make test-unit` / `make test-acceptance` | One test suite |
| `make test-integration` | Integration tests against your **current kubeconfig context**. Use a disposable cluster, e.g. `kind create cluster` |
| `make coverage` | Tests with an HTML coverage report in `htmlcov/` |
| `make docs` | Build the documentation with warnings treated as errors |
| `make build` | Build the sdist and wheel into `dist/` |

Add dependencies with `uv add <package>` (or `uv add --group test <package>` for
development-only tools) so that `pyproject.toml` and `uv.lock` stay in sync.

CI runs the same commands on Python 3.12, 3.13 and 3.14, plus the integration
tests on a [kind](https://kind.sigs.k8s.io/) cluster and a strict docs build.

## Releases

Releases are cut from `main` by `.github/workflows/release.yml` after CI
passes. PyPI, not the git tag, decides whether a version is released
(`scripts/release_state.py`, unit-tested in `tests/unit/release/`): the
workflow uploads the files PyPI does not list yet, with PEP 740 attestations
through trusted publishing, waits until PyPI lists every file, then pushes the
`v<version>` tag and publishes the release notes. Any run can be re-run, and a
new run finishes an interrupted one; a tag is never moved. To release a
version, bump it in `pyproject.toml` and add its changelog section. Pull
requests from this repository publish a pre-release to TestPyPI.

## Guidelines

- Keep imports and planning side-effect free: no clients, kubeconfig access or
  network calls at import time or in pure planning code.
- Add tests with every change. Template changes need unit tests that check the
  generated manifest.
- Code is formatted and linted with ruff, and public APIs are typed and checked
  with mypy. `make lint` runs both.
- Use [Conventional Commits](https://www.conventionalcommits.org/) for commit
  messages (`feat:`, `fix:`, `docs:` …).
- Update the documentation and the changelog when behaviour changes.

```{toctree}
:maxdepth: 1

docs
```
