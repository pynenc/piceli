"""Browser views of a composition's GitOps status, and its Sync requests.

Reads the controller's published status (``piceli.gitops-status.v1``, the
ConfigMap ``piceli-gitops-status``) and keeps only the documented fields:

- ``sources.<name>``: ``url``, ``refs`` ``{ref: sha}``, ``last_poll``;
- ``envs.<env>``: ``namespace``, ``state``, ``health``, ``revision``
  ``{source: sha}``, ``last_sync`` (else ``updated_at``) and
  ``components.<name>``: ``source``, ``commit``, ``digest``, ``state``
  (``synced``, ``building``, ``rolling``, ``failed``, ``unchanged``),
  ``health``, ``updated_at``;
- ``envs.<env>.last_action`` and ``verification`` (``state``, ``trigger``,
  ``checks_hash``, ``at`` and the ``check``/``code`` of each failed check;
  never the checks' free-text detail);
- ``envs.<env>.trigger``, ``approval`` (``via``, ``at``) and, while it waits
  for approval, ``pending_plan`` (the plan hash, object counts and changed
  objects by operation, kind and name).

The run history (``piceli.gitops-history.v1``, the ConfigMap
``piceli-gitops-history``) is projected by
:mod:`piceli.services.composition_history`.

A Sync writes the same request as ``piceli gitops sync ENV [--component
NAME]``: a ``sync`` entry in the ConfigMap ``piceli-gitops-requests``. The
browser never supplies a namespace, kubeconfig, URL or command; an
environment or component the status does not list is refused.

Importing this module is side-effect free.
"""

from __future__ import annotations

import hashlib
import re
import threading
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from piceli.gitops import GitOpsError
from piceli.gitops.state import Channel
from piceli.services.query import QueryError, QueryService
from piceli.services.registration import Registration

__all__ = ["COMPONENT_STATES", "CompositionControl", "sync_request"]

#: Component states the status may carry (others are shown as ``unknown``).
COMPONENT_STATES = ("synced", "building", "rolling", "failed", "unchanged")
_DNS = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")
_NAME = re.compile(r"[A-Za-z0-9](?:[-A-Za-z0-9_.]{0,126}[A-Za-z0-9])?")
_SHA = re.compile(r"[0-9a-f]{7,64}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_TEXT_LIMIT = 256


def sync_request(env: str, component: str | None = None) -> tuple[str, dict[str, Any]]:
    """The request ``piceli gitops sync ENV [--component NAME]`` writes, and its key.

    One implementation: :func:`piceli.gitops.state.sync_request` (key
    ``sync.<digest>``; a repeated Sync of the same target replaces the
    pending one).
    """
    from piceli.gitops.state import sync_request as shared

    if not isinstance(env, str):
        raise GitOpsError("gitops-request-invalid", "sync takes an environment name")
    return shared(env, component)


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value and len(value) <= _TEXT_LIMIT:
        return value
    return None


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _sha(value: Any) -> str | None:
    return value if isinstance(value, str) and _SHA.fullmatch(value) else None


def _url(value: Any) -> str | None:
    """A source URL without any user or password part (never a credential)."""
    text = _text(value)
    if text is None:
        return None
    if "://" not in text:
        # scp-like ``user@host:path``: keep it, minus anything before ``@``
        # that looks like a credential (``user:token@``).
        user, at, rest = text.rpartition("@")
        return f"{user}@{rest}" if at and ":" not in user else rest if at else text
    parts = urlsplit(text)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def _revision(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        name: sha
        for name, raw in sorted(value.items())
        if isinstance(name, str) and _NAME.fullmatch(name) and (sha := _sha(raw))
    }


def _component(name: str, value: Mapping[str, Any]) -> dict[str, Any]:
    state = _text(value.get("state"))
    digest = value.get("digest")
    return {
        "name": name,
        "source": _text(value.get("source")),
        "commit": _sha(value.get("commit")),
        "digest": digest
        if isinstance(digest, str) and _DIGEST.fullmatch(digest)
        else None,
        "state": state if state in COMPONENT_STATES else "unknown",
        "health": _text(value.get("health")) or "unknown",
        "updated_at": _text(value.get("updated_at")),
        "reason": _text(value.get("reason")),
    }


def _verification(value: Any) -> dict[str, Any] | None:
    """A checks-only verification, without the checks' output or detail."""
    if not isinstance(value, Mapping):
        return None
    failed = value.get("failed")
    return {
        "state": _text(value.get("state")) or "unknown",
        "trigger": _text(value.get("trigger")),
        "checks_hash": _text(value.get("checks_hash")),
        "at": _text(value.get("at")),
        "failed": [
            {"check": _text(item.get("check")), "code": _text(item.get("code"))}
            for item in failed
            if isinstance(item, Mapping)
        ]
        if isinstance(failed, list)
        else [],
    }


def _pending_plan(value: Any, plan_hash: str | None) -> dict[str, Any] | None:
    """What the owner approves: the pending plan's counts and changed objects."""
    from piceli.services.composition_history import _plan

    if not isinstance(value, Mapping) or plan_hash is None:
        return None
    if value.get("plan_hash") != plan_hash:
        return None  # an older plan's review never stands for the current hash
    plan = _plan(value) or {"counts": {}, "changes": [], "changes_total": 0}
    combined = value.get("combined_hash")
    images = value.get("images")
    return {
        "plan_hash": plan_hash,
        "combined_hash": combined
        if isinstance(combined, str) and _DIGEST.fullmatch(combined)
        else None,
        "release": _text(value.get("release")),
        **plan,
        "create_namespace": value.get("create_namespace") is True,
        "stop": [
            item
            for item in value.get("stop") or []
            if isinstance(item, str) and _DNS.fullmatch(item)
        ],
        "images": {
            key: ref
            for key, ref in sorted(images.items())
            if isinstance(key, str) and _NAME.fullmatch(key) and _text(ref)
        }
        if isinstance(images, Mapping)
        else {},
    }


def _approval(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    via = value.get("via")
    return {
        "via": via if via in ("cli", "ui") else "request",
        "at": _text(value.get("at")),
    }


def _environment(name: str, value: Mapping[str, Any]) -> dict[str, Any]:
    components = value.get("components")
    namespace = value.get("namespace")
    plan_hash = (
        value["plan_hash"]
        if isinstance(value.get("plan_hash"), str)
        and _DIGEST.fullmatch(value["plan_hash"])
        else None
    )
    return {
        "name": name,
        "namespace": namespace
        if isinstance(namespace, str) and _DNS.fullmatch(namespace)
        else None,
        "state": _text(value.get("state")) or "unknown",
        "health": _text(value.get("health")) or "unknown",
        "revision": _revision(value.get("revision")),
        "last_sync": _text(value.get("last_sync")) or _text(value.get("updated_at")),
        "reason": _text(value.get("reason")),
        "last_action": _text(value.get("last_action")),
        "verification": _verification(value.get("verification")),
        "plan_hash": plan_hash,
        "trigger": _text(value.get("trigger")),
        "approval": _approval(value.get("approval")),
        "pending_plan": _pending_plan(value.get("pending_plan"), plan_hash)
        if value.get("state") == "approval-required"
        else None,
        "components": [
            _component(key, item)
            for key, item in sorted(components.items())
            if isinstance(key, str)
            and _NAME.fullmatch(key)
            and isinstance(item, Mapping)
        ]
        if isinstance(components, Mapping)
        else [],
    }


def _source(name: str, value: Mapping[str, Any]) -> dict[str, Any]:
    refs = value.get("refs")
    return {
        "name": name,
        "url": _url(value.get("url")),
        "refs": {
            ref: sha
            for ref, raw in sorted(refs.items())
            if isinstance(ref, str)
            and 0 < len(ref) <= _TEXT_LIMIT
            and (sha := _sha(raw))
        }
        if isinstance(refs, Mapping)
        else {},
        "last_poll": _text(value.get("last_poll")),
        "error": _text(value.get("error")),
    }


class CompositionControl:
    """The Environments, Sources and Sync views over the controller's channel.

    :param query: The query service (environment scopes are registered on it,
        so their workloads, pods and logs open in the Resources view).
    :param application_id: The installed scope that authorizes these views.
    :param channel_factory: Opens the controller's status/request channel.
    :param register_environments: Register each environment's namespace as a
        local, read-only scope (in-cluster forward mode).
    """

    def __init__(
        self,
        query: QueryService,
        application_id: str,
        channel_factory: Callable[[], AbstractContextManager[Channel]],
        *,
        register_environments: bool = True,
    ) -> None:
        if application_id not in query.registrations:
            raise ValueError("composition control must name an installed scope")
        self.query = query
        self.application_id = application_id
        self.channel_factory = channel_factory
        self.register_environments = (
            register_environments and query.scope_policy is None
        )
        self._scopes: dict[str, str] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------- reading

    def _authorize(self, action: str) -> None:
        self.query.registration(self.application_id, action=action)

    def _status(self) -> dict[str, Any] | None:
        try:
            with self.channel_factory() as channel:
                return channel.read_status()
        except GitOpsError:
            raise QueryError("ui-observation-unavailable", 503) from None

    def _scope(self, env: dict[str, Any]) -> str | None:
        """Register ``env``'s namespace (once) and return its scope id."""
        namespace = env["namespace"]
        if not self.register_environments or namespace is None:
            return None
        identity = "env-" + hashlib.sha256(env["name"].encode()).hexdigest()[:24]
        base = self.query.registrations[self.application_id]
        registration = Registration(
            id=identity,
            name=env["name"],
            target=replace(
                base.target, namespace=namespace, cluster_uid=None, namespace_uid=None
            ),
            kinds=tuple(
                item for item in base.kinds if item[1] not in {"Secret", "ConfigMap"}
            ),
        )
        with self._lock:
            if self._scopes.get(env["name"]) != namespace:
                self.query.remove_local(identity)
            try:
                self.query.register_local(registration)
            except ValueError:
                return None
            self._scopes[env["name"]] = namespace
        return identity

    def _document(self) -> dict[str, Any]:
        document = self._status()
        if document is None:
            return {
                "configured": False,
                "controller": None,
                "sources": [],
                "environments": [],
            }
        controller = document.get("controller")
        controller = controller if isinstance(controller, Mapping) else {}
        sources = document.get("sources")
        envs = document.get("envs")
        environments = (
            [
                _environment(name, value)
                for name, value in sorted(envs.items())
                if isinstance(name, str)
                and _NAME.fullmatch(name)
                and isinstance(value, Mapping)
            ]
            if isinstance(envs, Mapping)
            else []
        )
        for env in environments:
            env["application_id"] = self._scope(env)
        if self.register_environments:
            listed = {env["name"] for env in environments}
            with self._lock:
                for name in set(self._scopes) - listed:
                    del self._scopes[name]
                    self.query.remove_local(
                        "env-" + hashlib.sha256(name.encode()).hexdigest()[:24]
                    )
        return {
            "configured": True,
            "controller": {
                "state": _text(controller.get("state")),
                "last_poll": _text(controller.get("last_poll")),
                "poll_seconds": _number(controller.get("poll_seconds")),
            },
            "sources": [
                _source(name, value)
                for name, value in sorted(sources.items())
                if isinstance(name, str)
                and _NAME.fullmatch(name)
                and isinstance(value, Mapping)
            ]
            if isinstance(sources, Mapping)
            else [],
            "environments": environments,
        }

    def overview(self) -> dict[str, Any]:
        """Sources and every environment (revision, health, state, last sync)."""
        self._authorize("inspect")
        return self._document()

    def environment(self, name: str) -> dict[str, Any]:
        """One environment with its components."""
        self._authorize("inspect")
        if not _NAME.fullmatch(name):
            raise QueryError("ui-invalid-request", 422)
        document = self._document()
        for env in document["environments"]:
            if env["name"] == name:
                return {
                    "configured": True,
                    "controller": document["controller"],
                    "sources": document["sources"],
                    "environment": env,
                }
        if not document["configured"]:
            raise QueryError("ui-controller-absent", 404)
        raise QueryError("ui-sync-target-unknown", 404)

    # ------------------------------------------------------------ history

    def _history(self) -> dict[str, Any] | None:
        try:
            with self.channel_factory() as channel:
                read = getattr(channel, "read_history", None)
                return read() if callable(read) else None
        except GitOpsError:
            raise QueryError("ui-observation-unavailable", 503) from None

    def history(self, env: str | None = None) -> dict[str, Any]:
        """Each environment's deploy runs, newest first, from the controller's
        published history (one environment with ``env``)."""
        from piceli.services import composition_history as projection

        self._authorize("inspect")
        if env is not None and not _NAME.fullmatch(env):
            raise QueryError("ui-invalid-request", 422)
        status = self._status()
        if status is None:
            if env is not None:
                raise QueryError("ui-controller-absent", 404)
            return {
                "configured": False,
                **projection.published(None),
                "environments": [],
            }
        envs = status.get("envs")
        names = sorted(
            name
            for name in (envs if isinstance(envs, Mapping) else {})
            if isinstance(name, str) and _NAME.fullmatch(name)
        )
        if env is not None:
            if env not in names:
                raise QueryError("ui-sync-target-unknown", 404)
            names = [env]
        document = self._history()
        return {
            "configured": True,
            **projection.published(document),
            "environments": [
                {"name": name, "runs": projection.environment_runs(document, name)}
                for name in names
            ],
        }

    # --------------------------------------------------------------- sync

    def sync(self, env: str, component: str | None = None) -> dict[str, Any]:
        """Ask the controller to sync ``env`` (or one of its components) now."""
        self._authorize("deploy")
        try:
            key, body = sync_request(env, component)
        except GitOpsError:
            raise QueryError("ui-invalid-request", 422) from None
        try:
            with self.channel_factory() as channel:
                document = channel.read_status()
                if document is None:
                    raise QueryError("ui-controller-absent", 404)
                entry = (document.get("envs") or {}).get(env)
                if not isinstance(entry, Mapping) or (
                    component is not None
                    and not isinstance(
                        (entry.get("components") or {}).get(component), Mapping
                    )
                ):
                    raise QueryError("ui-sync-target-unknown", 404)
                channel.add_request(key, body)
        except GitOpsError:
            raise QueryError("ui-sync-unavailable", 409) from None
        return {
            "state": "requested",
            "env": env,
            "component": component,
            "request": key,
        }
