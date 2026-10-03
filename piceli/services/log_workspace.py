"""One Logs workspace over every readable scope: applications, environments, profiles.

Each scope is a registration with its own explicit kubeconfig, context and
namespace (never an ambient context). A scope is listed only when the session
holds the ``logs`` grant for it, exactly as the per-resource log panel. Reads
stay bounded (lines per container, containers per request, bytes per read)
and every line is redacted before it leaves the service.

Profile scopes (:class:`ProfileScopes`) add read-only namespaces of other
saved credential profiles to a local UI, each with the profile's own
kubeconfig file and context. They are refused by the scoped cluster service
and inside a cluster, where every profile name means the pod's own account.

Importing this module is side-effect free.
"""

from __future__ import annotations

import hashlib
import re
import threading
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from piceli.k8s.ops.diagnosis import redact
from piceli.k8s.ops.provider_factory import KubeconfigTarget, Transport
from piceli.services.contracts import (
    Freshness,
    PartialError,
    WorkloadRef,
    WorkspaceLogBatch,
    WorkspaceLogLine,
    WorkspaceLogSource,
    WorkspaceLogSources,
    WorkspaceLogStream,
    WorkspaceScope,
)
from piceli.services.query import QueryError, QueryService, _now, _resource
from piceli.services.registration import Registration

__all__ = ["LEVELS", "LogWorkspace", "ProfileScopes", "detect_level", "split_time"]

#: Levels a line may be filtered by; ``unknown`` selects lines without one.
LEVELS = ("error", "warn", "info", "debug", "unknown")
_SCOPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_DNS = re.compile(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?")
_CONTAINER = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")
_UID = re.compile(r"[A-Za-z0-9-]{1,64}")
_TIME = re.compile(
    r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d) ?(.*)$",
    re.DOTALL,
)
_LEVEL_FIELD = re.compile(
    r"(?i)\b(?:level|lvl|severity|loglevel)[\"']?\s*[=:]\s*[\"']?"
    r"(trace|debug|info|information|notice|warn|warning|error|err|fatal|critical|crit|panic)\b"
)
_LEVEL_WORD = re.compile(
    r"\b(TRACE|DEBUG|INFO|NOTICE|WARN|WARNING|ERROR|ERR|FATAL|CRITICAL|CRIT|PANIC)\b"
)
_LEVEL_PREFIX = re.compile(r"^\[?(trace|debug|info|warn|warning|error|fatal)\]?[\s:]")
_NORMAL = {
    "trace": "debug",
    "debug": "debug",
    "info": "info",
    "information": "info",
    "notice": "info",
    "warn": "warn",
    "warning": "warn",
    "error": "error",
    "err": "error",
    "fatal": "error",
    "critical": "error",
    "crit": "error",
    "panic": "error",
}
Level = Literal["error", "warn", "info", "debug"]
_HIDDEN_KINDS = frozenset({"Secret", "ConfigMap"})


def split_time(line: str) -> tuple[str | None, str]:
    """The UTC timestamp Kubernetes prefixed to ``line`` (``timestamps=true``), and the rest."""
    found = _TIME.match(line)
    if found is None:
        return None, line
    whole, fraction, zone, rest = found.groups()
    try:
        moment = datetime.fromisoformat(
            f"{whole}.{(fraction or '0')[:6].ljust(6, '0')}"
            f"{'+00:00' if zone == 'Z' else zone}"
        )
    except ValueError:
        return None, line
    stamp = moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")
    # Nanoseconds order lines within one microsecond; never shown as more.
    return f"{stamp}{(fraction or '')[6:9].ljust(3, '0')}Z", rest


def detect_level(text: str) -> Level | None:
    """``error``, ``warn``, ``info`` or ``debug`` when the line states one."""
    found = _LEVEL_FIELD.search(text)
    if found is None:
        found = _LEVEL_WORD.search(text[:80])
    if found is None:
        found = _LEVEL_PREFIX.match(text.lower())
    return _NORMAL.get(found.group(1).lower()) if found is not None else None  # type: ignore[return-value]


def _scrub(text: str) -> str:
    indent = text[: len(text) - len(text.lstrip(" \t"))].replace("\t", "  ")[:16]
    return indent + redact(text, limit=2000)


def _owner_refs(raw: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in (raw.get("metadata") or {}).get("ownerReferences") or []
        if isinstance(item, dict)
        and isinstance(item.get("kind"), str)
        and isinstance(item.get("name"), str)
    ]


def _workload(
    raw: dict[str, Any], replica_owners: dict[str, WorkloadRef]
) -> WorkloadRef | None:
    owners = _owner_refs(raw)
    owner = next((item for item in owners if item.get("controller")), None)
    owner = owner or (owners[0] if owners else None)
    if owner is None:
        return None
    if owner["kind"] == "ReplicaSet" and owner["name"] in replica_owners:
        return replica_owners[owner["name"]]
    return WorkloadRef(kind=owner["kind"], name=owner["name"])


def _restarts(raw: dict[str, Any]) -> int:
    status = raw.get("status") or {}
    total = 0
    for key in ("containerStatuses", "initContainerStatuses"):
        for item in status.get(key) or []:
            count = item.get("restartCount") if isinstance(item, dict) else None
            if isinstance(count, int) and not isinstance(count, bool) and count > 0:
                total += count
    return total


class ProfileScopes:
    """Read-only namespaces of other saved profiles, added by the local operator."""

    limit = 8

    def __init__(self, query: QueryService, *, transport: Transport = "https") -> None:
        self.query = query
        self.transport = transport
        self._lock = threading.Lock()
        self.names: dict[str, tuple[str, str]] = {}

    @staticmethod
    def identity(profile: str, namespace: str) -> str:
        digest = hashlib.sha256(f"{profile}\0{namespace}".encode()).hexdigest()
        return "profile-" + digest[:24]

    def available(self) -> bool:
        from piceli.profiles import in_cluster

        return self.query.scope_policy is None and not in_cluster()

    def add(self, profile: str, namespace: str) -> Registration:
        """Register ``namespace`` of ``profile`` (idempotent); never an ambient context."""
        from piceli.profiles import ProfileError, load_profile

        if not self.available():
            raise QueryError("ui-operation-unavailable", 409)
        try:
            found = load_profile(profile)
        except ProfileError:
            raise QueryError("profile-not-found", 404) from None
        if not found.kubeconfig.is_file():
            raise QueryError("profile-invalid", 409)
        identity = self.identity(profile, namespace)
        try:
            base = Registration(
                identity,
                f"{profile} · {namespace}",
                KubeconfigTarget(
                    found.kubeconfig.absolute(),
                    found.context,
                    namespace,
                    transport=self.transport,
                ),
            )
        except ValueError:
            raise QueryError("profile-invalid", 409) from None
        registration = Registration(
            base.id,
            base.name,
            base.target,
            kinds=tuple(item for item in base.kinds if item[1] not in _HIDDEN_KINDS),
        )
        with self._lock:
            if identity not in self.names and len(self.names) >= self.limit:
                raise QueryError("ui-operation-conflict", 409)
            try:
                self.query.register_local(registration)
            except ValueError:
                raise QueryError("profile-invalid", 409) from None
            self.names[identity] = (profile, namespace)
        return self.query.registrations[identity]

    def remove(self, identity: str) -> None:
        with self._lock:
            if identity not in self.names:
                raise QueryError("ui-not-found")
            del self.names[identity]
            self.query.remove_local(identity)


class LogWorkspace:
    """Aggregate bounded, redacted log reads across the session's log scopes."""

    max_scopes = 16
    max_streams = 12
    max_lines = 5000
    max_pods = 500

    def __init__(
        self,
        query: QueryService,
        *,
        profiles: ProfileScopes | None = None,
        active_profile: str | None = None,
        prepare: Callable[[], None] | None = None,
    ) -> None:
        self.query = query
        self.profiles = profiles
        self.active_profile = active_profile
        self.prepare = prepare

    # -------------------------------------------------------------- scopes

    def _prepare(self) -> None:
        if self.prepare is None:
            return
        try:
            self.prepare()  # e.g. register the composition's environment scopes
        except QueryError:
            pass

    def _readable(self, registration: Registration) -> bool:
        capability = self.query.logs_capability
        return (
            capability is not None
            and capability(registration, None).allowed
            and self.query._allowed(registration.id, "logs")
        )

    def scope(self, registration: Registration) -> WorkspaceScope:
        profile = None
        kind: Literal["application", "environment", "profile"]
        if self.profiles is not None and registration.id in self.profiles.names:
            profile, _ = self.profiles.names[registration.id]
            kind = "profile"
        elif registration.id.startswith("env-"):
            kind = "environment"
        else:
            kind = "application"
        public = registration.public_target()
        return WorkspaceScope(
            id=registration.id,
            name=registration.name,
            kind=kind,
            cluster=public.name,
            namespace=public.namespace,
            profile=profile if profile is not None else self.active_profile,
        )

    def scopes(self) -> list[WorkspaceScope]:
        self._prepare()
        return [
            self.scope(registration)
            for registration in list(self.query.registrations.values())
            if self._readable(registration)
        ]

    # ------------------------------------------------------------- sources

    def sources(self, scope_ids: Iterable[str] = ()) -> WorkspaceLogSources:
        scopes = self.scopes()
        wanted = list(dict.fromkeys(scope_ids))
        if any(not _SCOPE.fullmatch(item) for item in wanted):
            raise QueryError("ui-invalid-request", 422)
        known = {item.id: item for item in scopes}
        selected = [known[item] for item in wanted if item in known] or (
            [] if wanted else scopes
        )
        partial: list[PartialError] = []
        if len(selected) > self.max_scopes:
            selected = selected[: self.max_scopes]
            partial.append(
                PartialError(scope="limit/scopes", code="ui-observation-unavailable")
            )
        items: list[WorkspaceLogSource] = []
        for scope in selected:
            try:
                items.extend(self._scope_sources(scope.id, partial))
            except QueryError:
                partial.append(
                    PartialError(
                        scope=f"scope/{scope.id}", code="ui-observation-unavailable"
                    )
                )
        return WorkspaceLogSources(
            scopes=scopes,
            items=items,
            freshness=Freshness(
                state="unavailable" if partial else "connected",
                observed_at=_now(),
                reason="partial-observation" if partial else None,
            ),
            partial=partial,
        )

    def _scope_sources(
        self, scope_id: str, partial: list[PartialError]
    ) -> list[WorkspaceLogSource]:
        registration = self.query.registration(scope_id, action="logs")
        reader = None
        try:
            reader = self.query.reader_factory(registration)
            registration = self.query._pin_target(registration, reader)
            namespace = registration.target.namespace
            pods = reader.list("v1", "Pod", namespace)
            replica_owners: dict[str, WorkloadRef] = {}
            try:
                for row in reader.list("apps/v1", "ReplicaSet", namespace):
                    name = (row.get("metadata") or {}).get("name")
                    owner = _workload(row, {})
                    if isinstance(name, str) and owner is not None:
                        replica_owners[name] = owner
            except Exception:
                partial.append(
                    PartialError(
                        scope=f"scope/{scope_id}/apps/v1/ReplicaSet",
                        code="ui-observation-unavailable",
                    )
                )
            items: list[WorkspaceLogSource] = []
            for raw in pods[: self.max_pods]:
                try:
                    pod = _resource(registration, raw)
                except (KeyError, TypeError, ValueError):
                    partial.append(
                        PartialError(
                            scope=f"scope/{scope_id}/v1/Pod",
                            code="ui-observation-unavailable",
                        )
                    )
                    continue
                if pod.identity.uid is None:
                    continue
                items.append(
                    WorkspaceLogSource(
                        scope_id=scope_id,
                        pod=pod.identity,
                        workload=_workload(raw, replica_owners),
                        containers=pod.containers,
                        phase=pod.phase,
                        started_at=(raw.get("status") or {}).get("startTime"),
                        restarts=_restarts(raw),
                    )
                )
            if len(pods) > self.max_pods:
                partial.append(
                    PartialError(
                        scope=f"limit/pods/{scope_id}",
                        code="ui-observation-unavailable",
                    )
                )
            return sorted(
                items,
                key=lambda item: (
                    item.workload.name if item.workload else item.pod.name,
                    item.started_at or "",
                    item.pod.name,
                ),
            )
        except QueryError:
            raise
        except Exception:
            raise QueryError("ui-observation-unavailable", 503) from None
        finally:
            if reader is not None:
                reader.close()

    # --------------------------------------------------------------- lines

    @staticmethod
    def parse_stream(value: str) -> tuple[str, str, str, str]:
        parts = value.split("/")
        if (
            len(parts) != 4
            or not _SCOPE.fullmatch(parts[0])
            or not _DNS.fullmatch(parts[1])
            or not _UID.fullmatch(parts[2])
            or not _CONTAINER.fullmatch(parts[3])
        ):
            raise QueryError("ui-invalid-request", 422)
        return parts[0], parts[1], parts[2], parts[3]

    def read(
        self,
        streams: Iterable[str],
        *,
        previous: bool = False,
        tail_lines: int = 500,
        since_seconds: int | None = None,
        query: str | None = None,
        levels: Iterable[str] = (),
    ) -> WorkspaceLogBatch:
        requested = list(dict.fromkeys(self.parse_stream(item) for item in streams))
        wanted_levels = set(levels)
        if (
            not requested
            or len(requested) > self.max_streams
            or not 1 <= tail_lines <= 5000
            or (since_seconds is not None and not 1 <= since_seconds <= 7 * 86400)
            or (query is not None and len(query) > 200)
            or not wanted_levels <= set(LEVELS)
        ):
            raise QueryError("ui-invalid-request", 422)
        self._prepare()
        cutoff = (
            (datetime.now(UTC) - timedelta(seconds=since_seconds)).strftime(
                "%Y-%m-%dT%H:%M:%S.%f"
            )
            if since_seconds is not None
            else None
        )
        needle = query.lower() if query else None
        results: list[WorkspaceLogStream] = []
        lines: list[tuple[str, int, int, WorkspaceLogLine]] = []
        digests: list[str] = []
        by_scope: dict[str, list[tuple[int, str, str, str]]] = {}
        for index, (scope, pod, uid, container) in enumerate(requested):
            by_scope.setdefault(scope, []).append((index, pod, uid, container))
            results.append(
                WorkspaceLogStream(
                    scope_id=scope,
                    pod_name=pod,
                    pod_uid=uid,
                    container=container,
                    previous=previous,
                    state="not-found",
                    code="ui-not-found",
                )
            )
        for scope, wanted in by_scope.items():
            for index, raw_text in self._read_scope(
                scope, wanted, previous, tail_lines, results
            ):
                bounded = raw_text[-1_048_576:]
                gap = len(raw_text.encode("utf-8")) >= 1_048_576 or len(raw_text) > len(
                    bounded
                )
                digests.append(hashlib.sha256(bounded.encode()).hexdigest())
                kept = 0
                last: str | None = None
                for order, line in enumerate(bounded.splitlines()[-tail_lines:]):
                    at, text = split_time(line)
                    # A continuation line (a stack trace) keeps its first line's time.
                    at = at or last
                    last = at
                    if cutoff is not None and at is not None and at[:26] < cutoff:
                        continue
                    text = _scrub(text)
                    level = detect_level(text)
                    if wanted_levels and (level or "unknown") not in wanted_levels:
                        continue
                    if needle is not None and needle not in text.lower():
                        continue
                    kept += 1
                    lines.append(
                        (
                            at or "",
                            index,
                            order,
                            WorkspaceLogLine(
                                at=at, stream=index, level=level, text=text
                            ),
                        )
                    )
                results[index] = results[index].model_copy(
                    update={"state": "ok", "code": None, "lines": kept, "gap": gap}
                )
        lines.sort(key=lambda item: (item[0], item[1], item[2]))
        truncated = len(lines) > self.max_lines
        merged = [item[3] for item in lines[-self.max_lines :]]
        cursor = hashlib.sha256(
            "\0".join(
                [
                    *digests,
                    str(previous),
                    str(since_seconds),
                    query or "",
                    ",".join(sorted(wanted_levels)),
                ]
            ).encode()
        ).hexdigest()[:24]
        return WorkspaceLogBatch(
            streams=results, lines=merged, truncated=truncated, cursor=cursor
        )

    def _read_scope(
        self,
        scope: str,
        wanted: list[tuple[int, str, str, str]],
        previous: bool,
        tail_lines: int,
        results: list[WorkspaceLogStream],
    ) -> list[tuple[int, str]]:
        """Read each wanted container of ``scope``; unreadable ones stay explicit."""
        try:
            registration = self.query.registration(scope, action="logs")
        except QueryError:
            return []  # unknown or not granted: reported as not-found
        if not self._readable(registration):
            return []
        reader = None
        found: list[tuple[int, str]] = []
        try:
            reader = self.query.reader_factory(registration)
            registration = self.query._pin_target(registration, reader)
            namespace = registration.target.namespace
            pods = {
                (
                    (row.get("metadata") or {}).get("name"),
                    (row.get("metadata") or {}).get("uid"),
                ): row
                for row in reader.list("v1", "Pod", namespace)
            }
            for index, pod, uid, container in wanted:
                raw = pods.get((pod, uid))
                if raw is None:
                    continue
                try:
                    containers = _resource(registration, raw).containers
                except (KeyError, TypeError, ValueError):
                    continue
                if container not in containers:
                    continue
                try:
                    text = reader.logs(
                        pod,
                        namespace,
                        container,
                        previous=previous,
                        tail_lines=tail_lines,
                    )
                except Exception:
                    results[index] = results[index].model_copy(
                        update={"state": "unavailable", "code": "ui-logs-unavailable"}
                    )
                    continue
                found.append((index, text))
        except QueryError:
            for index, *_ in wanted:
                results[index] = results[index].model_copy(
                    update={
                        "state": "unavailable",
                        "code": "ui-observation-unavailable",
                    }
                )
            return []
        except Exception:
            for index, *_ in wanted:
                results[index] = results[index].model_copy(
                    update={
                        "state": "unavailable",
                        "code": "ui-observation-unavailable",
                    }
                )
            return []
        finally:
            if reader is not None:
                reader.close()
        return found
