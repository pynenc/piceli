## What and why

<!-- A short summary of the change and the issue it closes. -->

Closes #

## Checklist

- [ ] The PR title follows [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:` …).
- [ ] Tests cover the change; `make test` never needs a cluster.
- [ ] `make typecheck`, `make lint` and `make docs` pass.
- [ ] New or changed error codes are in `piceli/errors.py` and `make docs-reference` was run.
- [ ] New commands have an entry in the `COMMANDS` table of `piceli/cli_contract.py`.
- [ ] Behaviour changes have a line in `docs/changelog.md` and an updated doc page.
- [ ] No real deployment, cluster, company or person names; no secret values.

## How it was tested

<!-- Unit / acceptance / kind integration / manual. For kind, say which tests ran. -->
