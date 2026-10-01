"""Reviewed requests for a named GitOps environment.

The controller owns the action and its policy. This adapter only offers refs
already published in status, checks the current status again before writing,
and never accepts a client-supplied target or channel.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from piceli.gitops import GitOpsError
from piceli.gitops.state import approve_request, promote_request
from piceli.services.composition_control import CompositionControl, sync_request
from piceli.services.query import QueryError

_NAME = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,249}")
_SHA = re.compile(r"[0-9a-f]{7,40}")
_HASH = re.compile(r"sha256:[0-9a-f]{64}")


class NamedEnvironmentActions:
    def __init__(self, composition: CompositionControl) -> None:
        self.composition = composition

    def _read(
        self, env: str, action: str
    ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        if not _NAME.fullmatch(env):
            raise QueryError("ui-invalid-request", 422)
        self.composition._authorize(action)
        try:
            with self.composition.channel_factory() as channel:
                document = channel.read_status()
        except GitOpsError:
            raise QueryError("ui-observation-unavailable", 503) from None
        if not isinstance(document, Mapping):
            raise QueryError("ui-controller-absent", 404)
        entry = (document.get("envs") or {}).get(env)
        if not isinstance(entry, Mapping):
            raise QueryError("ui-sync-target-unknown", 404)
        return document, entry

    @staticmethod
    def _refs(document: Mapping[str, Any]) -> list[dict[str, str]]:
        refs: list[dict[str, str]] = []
        sources = document.get("sources") or {}
        if not isinstance(sources, Mapping):
            return refs
        for source, value in sorted(sources.items()):
            if not isinstance(source, str) or not isinstance(value, Mapping):
                continue
            found = value.get("refs") or {}
            if not isinstance(found, Mapping):
                continue
            for branch, commit in sorted(found.items()):
                if (
                    isinstance(branch, str)
                    and isinstance(commit, str)
                    and _REF.fullmatch(branch)
                    and _SHA.fullmatch(commit)
                ):
                    refs.append({"source": source, "branch": branch, "commit": commit})
        return refs

    @staticmethod
    def _promotable(document: Mapping[str, Any], env: str) -> bool:
        controller = document.get("controller") or {}
        if not isinstance(controller, Mapping):
            return False
        rules = controller.get("environments") or []
        return any(
            isinstance(rule, Mapping)
            and rule.get("name") == env
            and rule.get("promote") is True
            for rule in rules
        )

    def options(self, env: str) -> dict[str, Any]:
        document, entry = self._read(env, "inspect")
        refs = self._refs(document)
        promotable = self._promotable(document, env) and bool(refs)
        pending = entry.get("state") == "approval-required"
        plan_hash = entry.get("plan_hash")
        approval = (
            pending and isinstance(plan_hash, str) and bool(_HASH.fullmatch(plan_hash))
        )
        stopped = entry.get("state") == "stopped" and entry.get("reason") == "idle-stop"
        revision = entry.get("revision") or {}
        summary = {
            "namespace": entry.get("namespace")
            if isinstance(entry.get("namespace"), str)
            else None,
            "revision": {
                name: commit
                for name, commit in revision.items()
                if isinstance(name, str)
                and isinstance(commit, str)
                and _SHA.fullmatch(commit)
            }
            if isinstance(revision, Mapping)
            else {},
            "components": sorted(
                name
                for name in (entry.get("components") or {})
                if isinstance(name, str)
            )
            if isinstance(entry.get("components"), Mapping)
            else [],
        }
        return {
            "env": env,
            "promote": {
                "allowed": promotable,
                "reason": None if promotable else "gitops-promote-not-allowed",
            },
            "approve": {
                "allowed": approval,
                "reason": None if approval else "ui-plan-stale",
            },
            "wake": {
                "allowed": stopped,
                "reason": None if stopped else "ui-operation-unavailable",
            },
            "refs": refs if promotable else [],
            "plan_hash": plan_hash if approval else None,
            "summary": summary,
            "stopped_since": entry.get("stopped_at")
            if stopped and isinstance(entry.get("stopped_at"), str)
            else None,
        }

    def promote(self, env: str, branch: str, commit: str) -> dict[str, str]:
        if not _REF.fullmatch(branch) or not _SHA.fullmatch(commit):
            raise QueryError("ui-invalid-request", 422)
        self.composition._authorize("deploy")
        try:
            with self.composition.channel_factory() as channel:
                document = channel.read_status()
                if not isinstance(document, Mapping) or not isinstance(
                    (document.get("envs") or {}).get(env), Mapping
                ):
                    raise QueryError("ui-plan-stale", 409)
                if not self._promotable(document, env):
                    raise QueryError("gitops-promote-not-allowed", 409)
                if {"branch": branch, "commit": commit} not in [
                    {"branch": item["branch"], "commit": item["commit"]}
                    for item in self._refs(document)
                ]:
                    raise QueryError("ui-plan-stale", 409)
                key, body = promote_request(f"{branch}@{commit}", env)
                channel.add_request(key, body)
        except GitOpsError:
            raise QueryError("ui-operation-unavailable", 409) from None
        return {"state": "requested", "env": env, "branch": branch, "commit": commit}

    def approve(self, env: str, plan_hash: str) -> dict[str, str]:
        if not _HASH.fullmatch(plan_hash):
            raise QueryError("ui-invalid-request", 422)
        self.composition._authorize("deploy")
        try:
            with self.composition.channel_factory() as channel:
                document = channel.read_status() or {}
                entry = (document.get("envs") or {}).get(env)
                if (
                    not isinstance(entry, Mapping)
                    or entry.get("state") != "approval-required"
                    or entry.get("plan_hash") != plan_hash
                ):
                    raise QueryError("ui-plan-stale", 409)
                key, body = approve_request(env, plan_hash)
                channel.add_request(key, body)
        except GitOpsError:
            raise QueryError("ui-operation-unavailable", 409) from None
        return {"state": "requested", "env": env, "plan_hash": plan_hash}

    def wake(self, env: str) -> dict[str, str]:
        self.composition._authorize("deploy")
        try:
            with self.composition.channel_factory() as channel:
                document = channel.read_status() or {}
                entry = (document.get("envs") or {}).get(env)
                if (
                    not isinstance(entry, Mapping)
                    or entry.get("state") != "stopped"
                    or entry.get("reason") != "idle-stop"
                ):
                    raise QueryError("ui-plan-stale", 409)
                key, body = sync_request(env)
                channel.add_request(key, body)
        except GitOpsError:
            raise QueryError("ui-sync-unavailable", 409) from None
        return {"state": "requested", "env": env}
