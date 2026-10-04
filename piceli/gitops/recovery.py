"""What a GitOps controller finds when it starts after dying mid-step (0.16.0).

A controller killed during a deploy (an OOM, a node drain, a crash) leaves
its run journal ``running`` and its environment's record ``in_progress``:
both looked as if the step went on forever. When the controller starts again
it marks them: the runs become ``interrupted`` (reason
:data:`INTERRUPTED`, ``finished_at`` when the restart found them) and the
record loses ``in_progress`` and gains ``interrupted`` (``action``,
``since``, ``at``) until its next step. The environment is retried as
before (its state was not changed by the step that died).

Importing this module is side-effect free.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

#: The registered reason of a run or step the controller died under.
INTERRUPTED = "gitops-run-interrupted"
#: How deep under ``pipelines/`` a pipeline's state directory can be.
_MAX_DEPTH = 8


def run_roots(state_dir: Path) -> Iterator[Path]:
    """Every pipeline state directory (one with ``runs/``) of the controller."""
    roots = [state_dir / "pipelines"]
    clusters = state_dir / "clusters"
    if clusters.is_dir():
        roots += sorted(item / "pipelines" for item in clusters.iterdir())
    for root in roots:
        if not root.is_dir():
            continue
        base = len(root.parts)
        for directory, children, _files in os.walk(root):
            here = Path(directory)
            if "runs" in children:
                yield here
            # Never into the journals themselves, nor too deep (caches).
            children[:] = (
                []
                if len(here.parts) - base >= _MAX_DEPTH
                else sorted(name for name in children if name != "runs")
            )


def interrupt_runs(state_dir: Path) -> list[str]:
    """Mark every run journal still ``running`` as ``interrupted``; their ids."""
    from piceli.pipeline.journal import mark_interrupted

    marked: list[str] = []
    for root in run_roots(state_dir):
        try:
            marked += mark_interrupted(root, INTERRUPTED)
        except OSError:  # never fail a start on one journal
            continue
    return marked


def interrupted_steps(
    envs: Mapping[str, dict[str, Any]], at: str
) -> list[tuple[str, dict[str, Any]]]:
    """Records with ``in_progress`` (their step died): moved to ``interrupted``.

    Returns each ``(name, in_progress)``.
    """
    found = []
    for name, record in sorted(envs.items()):
        step = record.pop("in_progress", None)
        if not isinstance(step, Mapping):
            continue
        record["interrupted"] = {
            "action": step.get("action"),
            "since": step.get("since"),
            "at": at,
        }
        found.append((name, dict(step)))
    return found
