# Contributing to Piceli

Thanks for your interest in Piceli! Bug reports, documentation fixes, new
templates and larger features are all welcome. For anything beyond a small fix,
please open an issue first so the design can be discussed.

The full guide lives in the documentation:
<https://docs.pynenc.org/projects/piceli/en/latest/contributing/index.html>

## Quick start

Piceli uses [uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
git clone https://github.com/pynenc/piceli.git
cd piceli
make install              # uv sync --all-extras
make install-pre-commit   # ruff, mypy, uv-lock and commit-message hooks
make test                 # unit + acceptance tests, no cluster needed
```

Run `make help` to see every target (`lint`, `typecheck`, `test-integration`,
`coverage`, `docs`, `build`).

## Pull requests

1. Fork the repository and create a branch from `main`.
2. Add tests for your change, and update the docs and `docs/changelog.md` when
   behaviour changes.
3. Make sure `make lint` and `make test` pass.
4. Use [Conventional Commits](https://www.conventionalcommits.org/) for commit
   messages (`feat:`, `fix:`, `docs:` …). The commit-msg hook checks this.
5. Open the pull request against `main`.

## Code of conduct

Be kind and constructive. We want Piceli to be a welcoming project for everyone.
