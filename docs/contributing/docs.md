# Building the Documentation

The documentation uses [Sphinx](https://www.sphinx-doc.org/) with
[MyST Markdown](https://myst-parser.readthedocs.io/), the
[Furo](https://pradyunsg.me/furo/) theme and
[sphinx-autodoc2](https://sphinx-autodoc2.readthedocs.io/) for the API reference.
It is published on Read the Docs at
[docs.pynenc.org/projects/piceli](https://docs.pynenc.org/projects/piceli/), as a
subproject of the Pynenc documentation.

## Build locally

From the repository root:

```bash
uv venv --python 3.12 .venv-docs
uv pip install --python .venv-docs/bin/python -e . \
  "sphinx>=8,<10" "myst-parser>=4" "furo>=2024" sphinx-copybutton \
  "sphinx-design>=0.6" sphinx-inline-tabs "sphinx-autodoc2>=0.5"
.venv-docs/bin/python -m sphinx -b html docs docs/_build/html
```

Then open `docs/_build/html/index.html` in a browser.

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
