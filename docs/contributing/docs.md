# Building the Documentation

The documentation uses [Sphinx](https://www.sphinx-doc.org/) with
[MyST Markdown](https://myst-parser.readthedocs.io/), the
[Furo](https://pradyunsg.me/furo/) theme and
[sphinx-autodoc2](https://sphinx-autodoc2.readthedocs.io/) for the API reference.
It is published on Read the Docs at
[docs.pynenc.org/projects/piceli](https://docs.pynenc.org/projects/piceli/en/stable/), as a
subproject of the Pynenc documentation.

## Build locally

From the repository root:

```bash
make docs
```

This runs `uv run --group docs sphinx-build -W --keep-going -b html docs docs/_build/html`.
Warnings are treated as errors, the same as in CI and on Read the Docs. Then open
`docs/_build/html/index.html` in a browser.

The API reference under `docs/apidocs/` is regenerated from docstrings on every
build, so do not edit those files by hand.

## Writing guidelines

- Pages are Markdown (MyST). Link to other pages with `` {doc}`path/to/page` ``,
  using a path relative to the current file and no `.md` extension.
- Link to files outside `docs/`, such as examples and tests, with a full GitHub
  URL. Relative paths that leave `docs/` do not resolve on Read the Docs.
- Keep examples generic: use names such as `my-app` or `example`, never names
  from a real environment, cluster or user.
- Aim for a warning-free build. Sphinx warnings usually mean a broken link or
  an unknown role.

```{tip}
See the [MyST syntax guide](https://myst-parser.readthedocs.io/en/latest/syntax/typography.html)
and the [sphinx-design components](https://sphinx-design.readthedocs.io/) used
for cards and grids.
```

## Generated reference pages

`docs/reference/errors.md` and `docs/reference/cli.md` are generated from code
(`piceli/errors.py` and `piceli help-json`). Regenerate them with
`make docs-reference` after adding an error code, a command or an option; a
test fails when they are stale.

## Maturity labels

Every feature page starts with its maturity, and the feature table in
{doc}`../roadmap` must agree:

````md
```{admonition} Maturity: preview
:class: note

One sentence on what may still change. See the {doc}`roadmap` for every
feature's status.
```
````

Use `:class: tip` for `stable`, `note` for `preview` and `warning` for
`experimental`.

## Command contract section template

Every how-to that introduces a command ends with a contract section. Most of
it is generated in {doc}`../reference/cli`; link to it and add what only the
page can explain:

````md
## Contract: `piceli <group> <command>`

- **Arguments:** see [the command reference](reference/cli.md#cli-<group>-<command>)
  for types and defaults.
- **Reads / writes:** files and state it touches.
- **Cluster:** none, reads, or writes (always through an explicit kubeconfig
  and context).
- **Approval:** what must be approved (plan hash, digest) and how to get it.
- **Retry and resume:** whether a re-run is safe, and how to resume.
- **Exit codes:** `0` success, `1` ran but did not succeed, `2` rejected,
  `3` approval required.
- **JSON output:** the schema in `docs/schemas/`, with one example.
- **Error codes:** the codes it can print, each linked to
  {doc}`../reference/errors`.
````

A new command also needs an entry in `COMMANDS` in `piceli/cli_contract.py`,
and new error codes an entry in `piceli/errors.py`; list new safe or
approval-required commands in {doc}`../agents` and `llms.txt`.
