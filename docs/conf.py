import datetime
import importlib.metadata
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.abspath(".."))

DISTRIBUTION_METADATA = importlib.metadata.metadata("Piceli")
# -- Project information -----------------------------------------------------
author = "Jose Diaz"
version = DISTRIBUTION_METADATA["Version"]
current_year = datetime.datetime.now(datetime.UTC).year
project = "piceli"
copyright = f"{current_year}, {author}"
release = version

# -- General configuration ---------------------------------------------------
extensions = [
    "autodoc2",
    "sphinx.ext.extlinks",
    "sphinx.ext.intersphinx",
    "sphinx.ext.mathjax",
    "sphinx.ext.todo",
    # Source links go to GitHub: viewcode rendered every module again (2.5 of
    # the build's 4.5 minutes).
    "sphinx.ext.linkcode",
    "myst_parser",
    "sphinx_copybutton",
    "sphinx_design",
    "sphinx_inline_tabs",
]

# -- Options for extlinks ----------------------------------------------------
extlinks = {
    "pypi": ("https://pypi.org/project/%s/", "%s"),
    # A file of the repository; tests/unit/test_docs_claims.py checks each exists.
    "src": ("https://github.com/pynenc/piceli/blob/main/%s", "%s"),
}

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store", "schemas"]

# -- Options for intersphinx -------------------------------------------------
# A docs build must not fail because another site is down (CI builds with
# -W). Each inventory falls back to a copy committed in _inventories/ (refresh
# with `make docs-inventories`); without a copy, an unreachable site's mapping
# is dropped and its references render as plain text.
_INVENTORIES = {
    "python": "https://docs.python.org/3",
    "pynenc": "https://docs.pynenc.org/en/latest/",
}


def _reachable(base: str) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(base.rstrip("/") + "/objects.inv", timeout=10):
            return True
    except Exception:
        return False


intersphinx_mapping = {}
for _name, _base in _INVENTORIES.items():
    _local = Path(__file__).parent / "_inventories" / f"{_name}.inv"
    if _local.is_file():
        intersphinx_mapping[_name] = (_base, (None, str(_local)))
    elif _reachable(_base):
        intersphinx_mapping[_name] = (_base, None)
    else:
        print(f"intersphinx: {_base} unreachable and no local copy; skipped")

# -- Autodoc settings ---------------------------------------------------
autodoc2_render_plugin = "myst"
autodoc2_packages = [{"path": "../piceli"}]
# Only the public modules (piceli/public_api.py); the rest is internal. A
# public module documents what its ``__all__`` exports, so the modules those
# names are defined in are documented too (piceli.infra's BuildHost comes from
# piceli.dev.model).
import importlib as _importlib  # noqa: E402

from piceli.public_api import PUBLIC_MODULES  # noqa: E402


def _documented() -> list[str]:
    names = set(PUBLIC_MODULES)
    for name in PUBLIC_MODULES:
        module = _importlib.import_module(name)
        for export in getattr(module, "__all__", ()):
            home = getattr(getattr(module, export, None), "__module__", None) or ""
            if home.startswith("piceli."):
                names.add(home)
    # autodoc2 does not look inside a skipped package: keep the parents too.
    for name in list(names):
        parts = name.split(".")
        names.update(".".join(parts[:end]) for end in range(2, len(parts)))
    return sorted(names)


_DOCUMENTED = _documented()


def _exact(names: list[str]) -> str:
    return "^(?:" + "|".join(name.replace(".", r"\.") for name in names) + ")$"


autodoc2_skip_module_regexes = ["^(?!" + _exact(_DOCUMENTED)[1:] + ")piceli\\..+"]
# Only where ``__all__`` names are imported statically: autodoc2 reads the
# source. A module that loads its exports lazily through a module
# ``__getattr__`` (the piceli/_lazy.py facades, piceli.testing) has them
# listed in the API index instead, linked to where they are defined.
autodoc2_module_all_regexes = [
    _exact(
        [
            name
            for name in PUBLIC_MODULES
            if hasattr(module := _importlib.import_module(name), "__all__")
            and "__getattr__" not in vars(module)
        ]
    )
]


# The reference is kept out of the global toctree (Furo copies that tree into
# every page's sidebar); docs/api.md links to its index, an orphan page that
# lists every documented module.
def _exports() -> str:
    """Each public module's exports, linked to where they are documented."""
    lines = []
    for name in PUBLIC_MODULES:
        module = _importlib.import_module(name)
        exported = sorted(getattr(module, "__all__", ()))
        if not exported:
            continue
        lines += [f"``{name}``", "^" * (len(name) + 4), ""]
        for export in exported:
            home = getattr(getattr(module, export, None), "__module__", None) or ""
            if home in _DOCUMENTED:
                lines.append(f"- :py:obj:`{export} <{home}.{export}>`")
            else:
                lines.append(f"- ``{export}``")
        lines.append("")
    return "\n".join(lines) + "\n"


autodoc2_index_template = (
    ":orphan:\n\n"
    "API Reference\n"
    "=============\n"
    "\n"
    "The public modules (``piceli/public_api.py``), generated from the\n"
    "docstrings. Import from these.\n"
    "\n"
    ".. toctree::\n"
    "   :titlesonly:\n"
    "\n" + "".join(f"   piceli/{name}\n" for name in PUBLIC_MODULES) + "\n"
    "What each public module exports\n"
    "-------------------------------\n"
    "\n" + _exports() + "\n"
    "Where the names are defined\n"
    "---------------------------\n"
    "\n"
    "Internal modules, which may move in any release: import from the public\n"
    "modules above.\n"
    "\n"
    ".. toctree::\n"
    "   :titlesonly:\n"
    "\n"
    + "".join(
        f"   piceli/{name}\n" for name in _DOCUMENTED if name not in PUBLIC_MODULES
    )
)
# Regex constants render as MyST link syntax and break the -W build.
# Builtins such as `type`/`bytes` in annotations collide with same-named model
# fields (e.g. HealthProbe.type); these ambiguity warnings are not doc errors.
suppress_warnings = ["ref.python"]
autodoc2_hidden_regexes = [
    r"piceli\.k8s\.templates\.auxiliary\.names\..*_REGEX",
    r"piceli\.chart\._(DNS_SUBDOMAIN|HOST)",
]


def linkcode_resolve(domain: str, info: dict[str, str]) -> str | None:
    """The object's lines in the repository on GitHub (``sphinx.ext.linkcode``)."""
    import importlib
    import inspect

    if domain != "py" or not info.get("module"):
        return None
    try:
        target: Any = importlib.import_module(info["module"])
        for part in info.get("fullname", "").split("."):
            target = getattr(target, part)
        target = inspect.unwrap(target)
        path = Path(inspect.getsourcefile(target) or "").resolve()
        lines, start = inspect.getsourcelines(target)
    except Exception:
        return None
    root = Path(__file__).resolve().parent.parent
    if not path.is_relative_to(root):
        return None
    anchor = f"#L{start}-L{start + len(lines) - 1}" if start else ""
    return f"https://github.com/pynenc/piceli/blob/v{version}/{path.relative_to(root)}{anchor}"


# -- MyST settings ---------------------------------------------------

myst_enable_extensions = [
    "dollarmath",
    "amsmath",
    "deflist",
    "fieldlist",
    "html_admonition",
    "html_image",
    "colon_fence",
    "smartquotes",
    "replacements",
    "strikethrough",
    "substitution",
    "tasklist",
    "attrs_inline",
    "attrs_block",
]
myst_heading_anchors = 2
myst_footnote_transition = True
myst_dmath_double_inline = True
myst_enable_checkboxes = True
myst_substitutions = {
    "role": "[role](#syntax/roles)",
    "directive": "[directive](#syntax/directives)",
}


# -- Options for HTML output -------------------------------------------------
html_theme = "furo"
html_title = "Piceli"
html_logo = "_static/logo.webp"
html_favicon = "_static/favicon.png"

language = "en"
html_static_path = ["_static"]
# Served at the site root: the index for language models (see AGENTS.md).
html_extra_path = ["../llms.txt"]
html_theme_options: dict[str, Any] = {
    "navigation_with_keys": True,
    "footer_icons": [
        {
            "name": "GitHub",
            "url": "https://github.com/pynenc/piceli",
            "html": """
                <svg stroke="currentColor" fill="currentColor" stroke-width="0" viewBox="0 0 16 16">
                    <path fill-rule="evenodd" d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0 0 16 8c0-4.42-3.58-8-8-8z"></path>
                </svg>
            """,
            "class": "",
        },
    ],
    "source_repository": "https://github.com/pynenc/piceli/",
    "source_branch": "main",
    "source_directory": "docs/",
    "announcement": (
        f"<b>{version}</b> is out. See the <a href='https://docs.pynenc.org/projects/piceli/en/stable/changelog.html'>changelog</a>"
        " for details. Piceli is pre-alpha: APIs may change between releases."
    ),
}
