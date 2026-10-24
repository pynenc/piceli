"""The Trains view of the UI: the local merge trains, read-only.

The UI owner starts ``piceli ui serve --trains``; the page shows what
``piceli trains`` shows, built by the same functions
(:mod:`piceli.train.status`): the run in progress with its step, host,
elapsed time and log tail, recent runs, step trends and flaky tests. With
``--train-composition MODULE`` it also shows which run produced what each
environment and NixHost runs (read as ``piceli targets`` reads them, at
most once a minute). It runs nothing and takes no lock.

Importing this module is side-effect free.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

__all__ = ["TrainsControl"]

#: Seconds a read of the targets is reused.
TARGETS_TTL = 60.0


def _strip(document: dict[str, Any]) -> dict[str, Any]:
    """Local paths and process ids are not the page's business."""
    for train in document.get("trains") or ():
        live = train.get("current")
        if live:
            live.pop("pid", None)
            for step in (live.get("step"), *(live.get("steps") or ())):
                if step:
                    step.pop("log", None)
    return document


class TrainsControl:
    """Serves ``GET /api/v1/trains`` and ``GET /api/v1/trains/deployed``.

    :param root: The train state root (default ``$XDG_STATE_HOME/piceli/train``).
    :param composition: A composition module whose targets to join.
    :param targets: Reads the ``piceli.targets.v1`` document (tests pass one).
    """

    def __init__(
        self,
        root: Path | None = None,
        *,
        composition: str | None = None,
        targets: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        from piceli.train.state import default_root

        self.root = root if root is not None else default_root()
        self.composition = composition
        self._targets = targets
        self._cache: tuple[float, dict[str, Any]] | None = None
        self._lock = threading.Lock()

    def status(self) -> dict[str, Any]:
        from piceli.train.status import overview

        return _strip(overview(self.root))

    def _read_targets(self) -> dict[str, Any] | None:
        if self._targets is not None:
            return self._targets()
        if self.composition is None:
            return None
        from piceli.k8s.cli.targets import collect

        document, _body, _reason, _detail = collect(self.composition, None)
        return document

    def deployed(self) -> dict[str, Any]:
        from piceli.train.status import deployed, histories

        if self._targets is None and self.composition is None:
            return {"configured": False, "rows": []}
        with self._lock:
            if self._cache and time.monotonic() - self._cache[0] < TARGETS_TTL:
                targets = self._cache[1]
            else:
                try:
                    targets = self._read_targets() or {}
                except Exception:  # a target that cannot be read never fails the page
                    return {
                        "configured": True,
                        "rows": [],
                        "reason": "ui-observation-unavailable",
                    }
                self._cache = (time.monotonic(), targets)
        return {
            "configured": True,
            "rows": deployed(targets, histories(self.root)),
            "generated_at": targets.get("generated_at"),
            "controller": targets.get("controller"),
        }
