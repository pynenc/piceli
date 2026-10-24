"""The UI's Builds page over the controller's channel (0.26.0).

The list is :func:`piceli.infra.builds.records` of the published status
and history (the document of ``piceli gitops builds``). The actions write
the requests the CLI writes, through the same functions: "Build now"
(``piceli gitops build``), approve (``piceli gitops approve`` of the digest
the page shows), retry (``piceli gitops sync``) and cancel (``piceli gitops
cancel``). The controller owns every decision: the gate, the plan and the
approval apply as for any other build.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from typing import Any

from piceli.gitops import GitOpsError
from piceli.gitops.state import (
    approve_request,
    build_cancel_request,
    build_request,
    sync_request,
)
from piceli.services.composition_control import CompositionControl
from piceli.services.query import QueryError

_NAME = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")


class BuildsControl:
    def __init__(self, composition: CompositionControl) -> None:
        self.composition = composition

    def builds(
        self, target: str | None = None, state: str | None = None
    ) -> dict[str, Any]:
        """Every build, newest first (``target`` and ``state`` filter it)."""
        from piceli.infra import builds as projection

        self.composition._authorize("inspect")
        if target is not None and not _NAME.fullmatch(target):
            raise QueryError("ui-invalid-request", 422)
        if state is not None and state != "active" and state not in projection.STAGES:
            raise QueryError("ui-invalid-request", 422)
        status = self.composition._status()
        if status is None:
            return {"configured": False, **projection.records(None, now=time.time())}
        history = self.composition._history()
        return {
            "configured": True,
            **projection.records(
                status, history, now=time.time(), target=target, state=state
            ),
            "targets": _targets(status),
        }

    def _write(self, name: str, request: tuple[str, dict[str, Any]]) -> dict[str, Any]:
        key, body = request
        try:
            with self.composition.channel_factory() as channel:
                document = channel.read_status()
                if document is None:
                    raise QueryError("ui-controller-absent", 404)
                if name not in _targets(document):
                    raise QueryError("ui-sync-target-unknown", 404)
                channel.add_request(key, body)
        except GitOpsError:
            raise QueryError("ui-sync-unavailable", 409) from None
        return {"state": "requested", "target": name, "request": key}

    def _check(self, name: str) -> None:
        if not _NAME.fullmatch(name):
            raise QueryError("ui-invalid-request", 422)
        self.composition._authorize("deploy")

    def start(self, name: str, ref: str | None) -> dict[str, Any]:
        """ "Build now": the request ``piceli gitops build NAME --ref REF`` writes."""
        self._check(name)
        try:
            request = build_request(name, ref or None, via="ui")
        except GitOpsError:
            raise QueryError("ui-invalid-request", 422) from None
        return self._write(name, request) | {"ref": ref or None}

    def approve(self, name: str, plan_hash: str) -> dict[str, Any]:
        self._check(name)
        try:
            request = approve_request(name, plan_hash, via="ui")
        except GitOpsError:
            raise QueryError("ui-invalid-request", 422) from None
        return self._write(name, request)

    def retry(self, name: str) -> dict[str, Any]:
        self._check(name)
        return self._write(name, sync_request(name))

    def cancel(self, name: str) -> dict[str, Any]:
        self._check(name)
        return self._write(name, build_cancel_request(name, via="ui"))


def _targets(status: Mapping[str, Any]) -> list[str]:
    """What "Build now" can name: each NixRelease and environment."""
    names = set()
    for key in ("nix_releases", "envs"):
        found = status.get(key)
        if isinstance(found, Mapping):
            names |= {
                name
                for name in found
                if isinstance(name, str) and _NAME.fullmatch(name)
            }
    return sorted(names)
