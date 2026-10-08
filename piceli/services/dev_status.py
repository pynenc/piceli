"""The UI's Development builds page (0.18.0): runs, queue and cache use.

Reads the ConfigMap ``piceli-dev-status`` the development-run queue
publishes in ``piceli-system`` (:mod:`piceli.dev.scheduler`) through the
UI's own grant, and returns its public fields only.

Importing this module is side-effect free.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from typing import Any

from piceli.gitops import GitOpsError
from piceli.services.query import QueryError, QueryService

SYSTEM_NAMESPACE = "piceli-system"
STATUS_CONFIGMAP = "piceli-dev-status"
SCHEMA = "piceli.ui-dev-builds.v1"
_RUN_FIELDS = (
    "run", "requester", "priority", "profile", "created_at", "started_at",
    "finished_at", "waited_seconds", "position", "state", "exit_code", "reason",
    "durations", "tests",
)  # fmt: skip
_CACHE_FIELDS = ("lineage", "warm", "crates_compiled", "crates_locked", "hit_ratio")


def _run(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, Mapping):
        return None
    found = {key: item.get(key) for key in _RUN_FIELDS if key in item}
    cache = item.get("cache")
    if isinstance(cache, Mapping):
        found["cache"] = {key: cache.get(key) for key in _CACHE_FIELDS if key in cache}
    return found


def public(raw: Mapping[str, Any] | None, now: float) -> dict[str, Any]:
    """The page's document from the published status (``None``: not installed)."""
    from piceli.dev.scheduler import alive

    if not isinstance(raw, Mapping):
        return {"schema": SCHEMA, "state": "not-installed"}
    found = raw.get("cache")
    cache: Mapping[str, Any] = found if isinstance(found, Mapping) else {}
    return {
        "schema": SCHEMA,
        "state": "running" if alive(raw, now) else "stale",
        "node": raw.get("node"),
        "slots": raw.get("slots"),
        "scheduler_at": raw.get("scheduler_at"),
        "running": [r for r in map(_run, raw.get("running") or ()) if r],
        "queued": [r for r in map(_run, raw.get("queued") or ()) if r],
        "recent": [r for r in map(_run, raw.get("recent") or ()) if r],
        "cache": {
            "used_bytes": cache.get("used_bytes"),
            "max_bytes": cache.get("max_bytes"),
            "at": cache.get("at"),
        },
    }


class DevStatusControl:
    """Reads the development-run status for one installed UI scope."""

    def __init__(
        self,
        query: QueryService,
        application_id: str,
        reader: Callable[[], Mapping[str, Any] | None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.query = query
        self.application_id = application_id
        self.reader = reader
        self.clock = clock
        self._installed: tuple[float, bool] | None = None

    def _read(self) -> Mapping[str, Any] | None:
        if self.reader is not None:
            return self.reader()
        from piceli.gitops.install import connect

        target = self.query.registration(self.application_id, action="inspect").target
        with connect(
            target.kubeconfig,
            target.context,
            transport=target.transport,
            exec_policy=target.exec_policy,
        ) as api:
            found = api.call(
                f"/api/v1/namespaces/{SYSTEM_NAMESPACE}/configmaps/{STATUS_CONFIGMAP}",
                "GET",
            )
        text = (
            ((found or {}).get("data") or {}).get("status.json")
            if isinstance(found, Mapping)
            else None
        )
        value = json.loads(text) if isinstance(text, str) else None
        return value if isinstance(value, Mapping) else None

    def installed(self) -> bool:
        """Whether a queue ever published (cached for a minute; the nav entry)."""
        now = self.clock()
        if self._installed is None or now - self._installed[0] > 60:
            try:
                found = self._read() is not None
            except (GitOpsError, OSError, ValueError):
                found = False
            self._installed = (now, found)
        return self._installed[1]

    def status(self) -> dict[str, Any]:
        try:
            raw = self._read()
        except (GitOpsError, OSError, ValueError):
            raise QueryError("ui-observation-unavailable", 503) from None
        return public(raw, self.clock())
