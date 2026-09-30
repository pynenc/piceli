"""Bounded shared Kubernetes list/watch state and transient scoped notifications."""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from piceli.services.contracts import Event
from piceli.services.registration import Registration

Key = tuple[str, str, str]
logger = logging.getLogger(__name__)


def _cache_row(raw: dict[str, Any], kind: str) -> dict[str, Any]:
    """Keep only the identity fields needed for sensitive object observation."""
    if kind not in {"Secret", "ConfigMap"}:
        return raw
    metadata = raw.get("metadata") or {}
    return {
        "apiVersion": raw.get("apiVersion"),
        "kind": raw.get("kind"),
        "metadata": {
            key: metadata[key]
            for key in (
                "name",
                "namespace",
                "uid",
                "resourceVersion",
                "generation",
                "ownerReferences",
            )
            if key in metadata
        },
    }


@dataclass
class _Entry:
    registration: Registration
    scopes: set[str]
    rows: dict[str, dict[str, Any]]
    version: str
    last_access: float
    stale: bool = False
    stop: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None


class ObservationHub:
    """One physical watch per active scope/kind, with bounded idle ownership."""

    def __init__(
        self,
        reader_factory: Callable[[Registration], Any],
        *,
        max_watches: int = 40,
        idle_seconds: float = 60,
        replay_limit: int = 1000,
    ) -> None:
        self.reader_factory = reader_factory
        self.max_watches = max_watches
        self.idle_seconds = idle_seconds
        self.replay_limit = replay_limit
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._entries: dict[Key, _Entry] = {}
        self._scope_keys: dict[tuple[str, str, str], Key] = {}
        self._loading: set[Key] = set()
        self._events: deque[Event] = deque(maxlen=replay_limit)
        self._sequence = 0
        self._closed = False

    def cursor(self) -> str:
        with self._lock:
            return str(self._sequence)

    def events(
        self, scope: str, after: str | None, *, limit: int = 100
    ) -> tuple[list[Event], str, bool]:
        if not 1 <= limit <= 1000 or (
            after is not None
            and (len(after) > 20 or not after.isascii() or not after.isdecimal())
        ):
            raise ValueError("invalid observation cursor")
        with self._lock:
            current = self._sequence if after is None else int(after)
            oldest = int(self._events[0].cursor) if self._events else self._sequence + 1
            if current > self._sequence or current < oldest - 1:
                return [], str(self._sequence), True
            events = [
                event
                for event in self._events
                if int(event.cursor) > current and event.scope == scope
            ]
            if len(events) > limit:
                return [], str(self._sequence), True
            return events, events[-1].cursor if events else str(self._sequence), False

    def _emit(self, scope: str, kind: str, subject: str | None) -> None:
        self._sequence += 1
        self._events.append(
            Event(
                cursor=str(self._sequence),
                scope=scope,
                kind=kind,  # type: ignore[arg-type]
                subject_id=subject,
            )
        )

    def _emit_entry(self, entry: _Entry, kind: str, subject: str | None) -> None:
        for scope in sorted(entry.scopes):
            self._emit(scope, kind, subject)

    @staticmethod
    def _key(registration: Registration, api_version: str, kind: str) -> Key:
        target = registration.target
        try:
            stat = target.kubeconfig.stat()
            file_identity = (
                f"{stat.st_dev}:{stat.st_ino}:{stat.st_mtime_ns}:{stat.st_size}"
            )
        except OSError:
            file_identity = "unavailable"
        credential_boundary = "\0".join(
            str(value)
            for value in (
                target.kubeconfig.absolute(),
                file_identity,
                target.context,
                target.namespace,
                target.cluster_uid,
                target.namespace_uid,
                target.transport,
                target.allow_exec,
                target.exec_sha256,
                target.exec_pass_env,
                target.exec_timeout_seconds,
            )
        )
        return (
            hashlib.sha256(credential_boundary.encode()).hexdigest(),
            api_version,
            kind,
        )

    def observe(
        self,
        registration: Registration,
        reader: Any,
        api_version: str,
        kind: str,
        *,
        fresh: bool = False,
    ) -> tuple[list[dict[str, Any]], bool]:
        key = self._key(registration, api_version, kind)
        scope_key = registration.id, api_version, kind
        with self._lock:
            previous = self._scope_keys.get(scope_key)
            if previous is not None and previous != key:
                old = self._entries.get(previous)
                if old is not None:
                    old.scopes.discard(registration.id)
                    if not old.scopes:
                        old.stop.set()
            self._scope_keys[scope_key] = key
        watchable = registration.target.transport != "loopback-http" and callable(
            getattr(reader, "watch", None)
        )
        if not watchable:
            return [
                _cache_row(raw, kind)
                for raw in reader.list(api_version, kind, registration.target.namespace)
            ], False
        with self._condition:
            entry = self._entries.get(key)
            if entry is not None and not fresh and not entry.stale:
                entry.scopes.add(registration.id)
                entry.last_access = time.monotonic()
                return list(entry.rows.values()), False
            while key in self._loading:
                if not self._condition.wait(timeout=10):
                    raise TimeoutError("observation list remained busy")
                entry = self._entries.get(key)
                if entry is not None and not fresh and not entry.stale:
                    entry.scopes.add(registration.id)
                    entry.last_access = time.monotonic()
                    return list(entry.rows.values()), False
            self._loading.add(key)
        try:
            rows = [
                _cache_row(raw, kind)
                for raw in reader.list(api_version, kind, registration.target.namespace)
            ]
            version = getattr(reader, "last_versions", {}).get(
                (api_version, kind, registration.target.namespace), ""
            )
            mapped = {
                row["metadata"]["name"]: row
                for row in rows
                if isinstance(row.get("metadata", {}).get("name"), str)
            }
            if len(mapped) != len(rows):
                raise ValueError("invalid observation identity")
            with self._condition:
                entry = self._entries.get(key)
                if (
                    entry is None
                    and len(self._entries) < self.max_watches
                    and not self._closed
                ):
                    entry = _Entry(
                        registration,
                        {registration.id},
                        mapped,
                        version,
                        time.monotonic(),
                    )
                    self._entries[key] = entry
                    entry.thread = threading.Thread(
                        target=self._watch,
                        args=(key, entry),
                        name="piceli-observe-" + kind.lower(),
                        daemon=True,
                    )
                    entry.thread.start()
                elif entry is not None:
                    entry.scopes.add(registration.id)
                    entry.rows = mapped
                    entry.version = version
                    entry.stale = False
                    entry.last_access = time.monotonic()
            return rows, False
        finally:
            with self._condition:
                self._loading.discard(key)
                self._condition.notify_all()

    def _watch(self, key: Key, entry: _Entry) -> None:
        registration = entry.registration
        api_version, kind = key[1:]
        namespace = registration.target.namespace
        reader = None
        failures = 0
        try:
            reader = self.reader_factory(registration)
            identity = getattr(reader, "identity", None)
            if identity is not None and any(
                pin is not None and pin != actual
                for pin, actual in (
                    (registration.target.cluster_uid, identity.cluster_uid),
                    (registration.target.namespace_uid, identity.namespace_uid),
                )
            ):
                raise ValueError("observed target changed")
            while not entry.stop.is_set():
                with self._lock:
                    if time.monotonic() - entry.last_access > self.idle_seconds:
                        break
                    version = entry.version
                try:
                    if not version:
                        rows = [
                            _cache_row(raw, kind)
                            for raw in reader.list(api_version, kind, namespace)
                        ]
                        version = getattr(reader, "last_versions", {}).get(
                            (api_version, kind, namespace), ""
                        )
                        if len(rows) > 1000 or not version:
                            raise ValueError("watch list unbounded or unversioned")
                        with self._lock:
                            entry.rows = {row["metadata"]["name"]: row for row in rows}
                            entry.version = version
                            entry.stale = False
                            self._emit_entry(entry, "reset", None)
                    for action, raw in reader.watch(
                        api_version, kind, namespace, version
                    ):
                        if entry.stop.is_set():
                            break
                        metadata = raw.get("metadata") or {}
                        name = metadata.get("name")
                        current = metadata.get("resourceVersion")
                        if action == "BOOKMARK" and isinstance(current, str):
                            with self._lock:
                                entry.version = current
                            continue
                        if action == "ERROR":
                            raise ValueError("watch reset requested")
                        if (
                            action not in {"ADDED", "MODIFIED", "DELETED"}
                            or not isinstance(name, str)
                            or metadata.get("namespace") != namespace
                            or raw.get("apiVersion") != api_version
                            or raw.get("kind") != kind
                        ):
                            continue
                        with self._lock:
                            if action == "DELETED":
                                entry.rows.pop(name, None)
                            else:
                                entry.rows[name] = _cache_row(raw, kind)
                            if len(entry.rows) > 1000:
                                raise ValueError("watch exceeded object limit")
                            if isinstance(current, str):
                                entry.version = current
                            entry.stale = False
                            self._emit_entry(
                                entry,
                                "delete" if action == "DELETED" else "upsert",
                                f"{api_version}/{kind}/{name}",
                            )
                        failures = 0
                    failures = 0
                except Exception as error:
                    failures += 1
                    if failures in {1, 2, 4, 8}:
                        logger.warning(
                            "UI watch reset for %s %s/%s: %s status=%s",
                            registration.id,
                            api_version,
                            kind,
                            type(error).__name__,
                            getattr(error, "status", None),
                        )
                    with self._lock:
                        entry.version = ""
                        entry.stale = True
                        self._emit_entry(entry, "reset", None)
                    if entry.stop.wait(min(30, 2 ** min(failures, 5))):
                        break
        except Exception as error:
            logger.warning(
                "UI watch unavailable for %s %s/%s: %s status=%s",
                registration.id,
                api_version,
                kind,
                type(error).__name__,
                getattr(error, "status", None),
            )
            with self._lock:
                entry.stale = True
                self._emit_entry(entry, "reset", None)
        finally:
            if reader is not None:
                try:
                    reader.close()
                except Exception:
                    pass
            with self._lock:
                if self._entries.get(key) is entry:
                    del self._entries[key]

    def close(self) -> None:
        with self._lock:
            self._closed = True
            entries = list(self._entries.values())
            for entry in entries:
                entry.stop.set()
        deadline = time.monotonic() + 16
        for entry in entries:
            if entry.thread is not None:
                entry.thread.join(max(0, deadline - time.monotonic()))
