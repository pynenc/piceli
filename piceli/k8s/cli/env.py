"""``piceli env …``, ``piceli envs`` and ``piceli logs``: per-branch environments.

A pipeline that declares ``envs=EnvConfig(...)`` runs one namespace per Git
branch (see :mod:`piceli.envs` and ``docs/environments.md``). The ``env``
group holds the commands that change an environment (``up``, ``down``,
``seed``; ``push`` lives in its own module and is added to :data:`app`).

Every command takes the pipeline with ``--pipeline MODULE:ATTR`` (or the
``PICELI_PIPELINE`` variable). Importing this module is side-effect free.
"""

from __future__ import annotations

import typer

app = typer.Typer(
    rich_markup_mode=None,
    help=(
        "Per-branch environments of a pipeline that declares envs=EnvConfig(...): "
        "one namespace per Git branch."
    ),
    no_args_is_help=True,
)

PIPELINE_ENV = "PICELI_PIPELINE"
PIPELINE_HELP = (
    "The pipeline: MODULE:ATTR or path/to/file.py:ATTR naming a Pipeline that "
    f"declares envs=EnvConfig(...) (default: ${PIPELINE_ENV})"
)


def register(root: typer.Typer) -> None:
    """Add ``env`` (``up``, ``down``, ``seed`` …), ``envs`` and ``logs`` to the root."""
    root.add_typer(app, name="env")
