"""Counts for the navigation's alert badges, from the views the session may read.

Each count comes from the same service as its page (composition status,
GitOps status, cluster status, cluster builds, forwards) under that page's
grant. A count the session cannot read, or that cannot be observed now, is
``None`` (no badge), never zero. The summary is cached briefly per principal
so the navigation does not multiply live status reads.

Importing this module is side-effect free.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from piceli.services.contracts import NavigationEnvironment, NavigationSummary
from piceli.services.query import QueryError, QueryService

if TYPE_CHECKING:
    from piceli.services.cluster_build_control import ClusterBuildControl
    from piceli.services.cluster_status import ClusterStatusControl
    from piceli.services.composition_control import CompositionControl
    from piceli.services.environment_control import EnvironmentControl
    from piceli.services.forwards import ForwardWorkspace

__all__ = ["NavigationService", "registry_warnings"]

_DEGRADED_HEALTH = frozenset({"degraded", "failed", "unhealthy"})
_DEGRADED_STATE = frozenset({"failed", "degraded", "retrying"})


def registry_warnings(status: Mapping[str, Any]) -> int:
    """A registry that is not ready, and node mirrors that need attention."""
    registry = status.get("registry")
    if not isinstance(registry, Mapping):
        return 0
    count = 0 if registry.get("state") == "ready" else 1
    for node in status.get("nodes") or []:
        mirror = node.get("mirror") if isinstance(node, Mapping) else None
        if isinstance(mirror, Mapping) and mirror.get("state") in {
            "needs-restart",
            "missing",
        }:
            count += 1
    return count


def _quiet(read: Callable[[], Any]) -> Any:
    try:
        return read()
    except QueryError:
        return None
    except Exception:
        return None


class NavigationService:
    """Assemble :class:`NavigationSummary` from the configured controls."""

    ttl = 15.0

    def __init__(
        self,
        query: QueryService,
        *,
        composition: CompositionControl | None = None,
        environments: EnvironmentControl | None = None,
        cluster_status: ClusterStatusControl | None = None,
        cluster_builds: ClusterBuildControl | None = None,
        forwards: ForwardWorkspace | None = None,
    ) -> None:
        self.query = query
        self.composition = composition
        self.environments = environments
        self.cluster_status = cluster_status
        self.cluster_builds = cluster_builds
        self.forwards = forwards
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, NavigationSummary]] = {}

    def summary(self) -> NavigationSummary:
        principal = self.query._principal().id
        revision = self.query.scope_policy.revision() if self.query.scope_policy else 0
        key = f"{principal}\0{revision}"
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and now - cached[0] < self.ttl:
                return cached[1]
        result = self._build()
        with self._lock:
            self._cache = {key: (now, result)}
        return result

    def _build(self) -> NavigationSummary:
        environments: list[NavigationEnvironment] = []
        approvals: int | None = None
        degraded: int | None = None
        failed: int | None = None
        overview = (
            _quiet(self.composition.overview) if self.composition is not None else None
        )
        if isinstance(overview, Mapping) and overview.get("configured"):
            rows = [
                item
                for item in overview.get("environments") or []
                if isinstance(item, Mapping)
            ]
            environments = [
                NavigationEnvironment(
                    name=str(item.get("name")),
                    state=str(item.get("state") or "unknown"),
                    health=str(item.get("health") or "unknown"),
                    application_id=item.get("application_id")
                    if isinstance(item.get("application_id"), str)
                    else None,
                )
                for item in rows
            ]
            approvals = sum(item.state == "approval-required" for item in environments)
            degraded = sum(
                item.health in _DEGRADED_HEALTH or item.state in _DEGRADED_STATE
                for item in environments
            )
            failed = sum(
                component.get("state") == "failed"
                for item in rows
                for component in item.get("components") or []
                if isinstance(component, Mapping)
            )
        gitops = (
            _quiet(self.environments.gitops_status)
            if self.environments is not None
            and (
                self.environments.controller_target is not None
                or self.environments.channel_factory is not None
            )
            else None
        )
        if isinstance(gitops, Mapping) and gitops.get("configured"):
            waiting = sum(
                isinstance(item, Mapping) and item.get("state") == "approval-required"
                for item in gitops.get("envs") or []
            )
            approvals = (approvals or 0) + waiting
        builds = (
            _quiet(self.cluster_builds.operations)
            if self.cluster_builds is not None
            else None
        )
        if isinstance(builds, Mapping):
            failed = (failed or 0) + sum(
                isinstance(item, Mapping) and item.get("state") == "failed"
                for item in builds.get("items") or []
            )
        status = (
            _quiet(self.cluster_status.status)
            if self.cluster_status is not None
            else None
        )
        warnings = registry_warnings(status) if isinstance(status, Mapping) else None
        stale = (
            _quiet(self.forwards.stale_count)
            if self.forwards is not None and self.forwards.mode != "unavailable"
            else None
        )
        return NavigationSummary(
            environments=environments,
            approvals=approvals,
            degraded_environments=degraded,
            failed_builds=failed,
            stale_forwards=stale,
            registry_warnings=warnings,
        )
