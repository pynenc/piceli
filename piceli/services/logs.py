"""Scoped, bounded workload log reads with explicit pod and container identity."""

from __future__ import annotations

import hashlib
from typing import Any

from piceli.services.contracts import (
    Capability,
    Freshness,
    LogBatch,
    LogSource,
    LogSourcePage,
    PartialError,
    Resource,
)
from piceli.services.query import QueryError, QueryService, _now, _resource
from piceli.services.registration import Registration


class LogService:
    """Keep a workload selection while pods change; never infer a pod from a log URL."""

    def __init__(self, query: QueryService) -> None:
        self.query = query
        query.logs_capability = self.capability

    def capability(
        self, _registration: Registration, resource: Resource | None
    ) -> Capability:
        if resource is None:
            return Capability(allowed=True)
        allowed = (
            resource.identity.kind
            in {"Pod", "Deployment", "StatefulSet", "DaemonSet", "Job", "ReplicaSet"}
            and resource.identity.uid is not None
        )
        return Capability(
            allowed=allowed, reason=None if allowed else "no-container-log-source"
        )

    def _sources(
        self, application_id: str, resource_id: str, resource_uid: str
    ) -> LogSourcePage:
        self.query.registration(application_id, action="logs")
        selected = self.query.resource(application_id, resource_id)
        if selected.identity.uid != resource_uid:
            raise QueryError("ui-observation-unavailable", 409)
        registration = self.query.registration(application_id)
        if not self.capability(registration, selected).allowed:
            raise QueryError("ui-operation-unavailable", 409)
        reader = None
        partial: list[PartialError] = []
        pods: list[dict[str, Any]] = []
        replicas: list[dict[str, Any]] = []
        try:
            reader = self.query.reader_factory(registration)
            registration = self.query._pin_target(registration, reader)
            try:
                pods = reader.list("v1", "Pod", registration.target.namespace)
            except Exception:
                partial.append(
                    PartialError(scope="v1/Pod", code="ui-observation-unavailable")
                )
            if selected.identity.kind == "Deployment":
                try:
                    replicas = reader.list(
                        "apps/v1", "ReplicaSet", registration.target.namespace
                    )
                except Exception:
                    partial.append(
                        PartialError(
                            scope="apps/v1/ReplicaSet",
                            code="ui-observation-unavailable",
                        )
                    )
            replica_uids = {
                row.get("metadata", {}).get("uid")
                for row in replicas
                if selected.identity.uid in _owners(row)
            }
            items = []
            for raw in pods:
                meta = raw.get("metadata") or {}
                owners = _owners(raw)
                match = (
                    meta.get("uid") == selected.identity.uid
                    if selected.identity.kind == "Pod"
                    else bool(
                        selected.identity.uid in owners
                        or (
                            selected.identity.kind == "Deployment"
                            and replica_uids.intersection(owners)
                        )
                    )
                )
                if not match:
                    continue
                try:
                    pod = _resource(registration, raw)
                except (KeyError, TypeError, ValueError):
                    partial.append(
                        PartialError(scope="v1/Pod", code="ui-observation-unavailable")
                    )
                    continue
                items.append(
                    LogSource(
                        pod=pod.identity,
                        containers=pod.containers,
                        phase=pod.phase,
                        started_at=(raw.get("status") or {}).get("startTime"),
                    )
                )
            return LogSourcePage(
                items=sorted(
                    items, key=lambda item: (item.started_at or "", item.pod.name)
                ),
                freshness=Freshness(
                    state="unavailable" if partial else "connected",
                    observed_at=_now(),
                    reason="partial-observation" if partial else None,
                ),
                partial=partial,
            )
        except QueryError:
            raise
        except Exception:
            raise QueryError("ui-observation-unavailable", 503) from None
        finally:
            if reader is not None:
                reader.close()

    def sources(
        self, application_id: str, resource_id: str, resource_uid: str
    ) -> LogSourcePage:
        return self._sources(application_id, resource_id, resource_uid)

    def read(
        self,
        application_id: str,
        resource_id: str,
        *,
        resource_uid: str,
        pod_name: str,
        pod_uid: str,
        container: str,
        previous: bool = False,
        tail_lines: int = 2000,
    ) -> LogBatch:
        if not 1 <= tail_lines <= 5000:
            raise QueryError("ui-invalid-request", 422)
        source_page = self._sources(application_id, resource_id, resource_uid)
        source = next(
            (
                item
                for item in source_page.items
                if item.pod.name == pod_name and item.pod.uid == pod_uid
            ),
            None,
        )
        if source is None:
            raise QueryError(
                "ui-observation-unavailable" if source_page.partial else "ui-not-found",
                503 if source_page.partial else 404,
            )
        if container not in source.containers:
            raise QueryError("ui-invalid-request", 422)
        registration = self.query.registration(application_id)
        reader = None
        try:
            reader = self.query.reader_factory(registration)
            self.query._pin_target(registration, reader)
            # A second live UID check prevents a reused pod name from reading a
            # different incarnation's logs after the source list was returned.
            pods = reader.list("v1", "Pod", registration.target.namespace)
            if not any(
                (row.get("metadata") or {}).get("name") == pod_name
                and (row.get("metadata") or {}).get("uid") == pod_uid
                for row in pods
            ):
                raise QueryError("ui-not-found")
            raw = reader.logs(
                pod_name,
                registration.target.namespace,
                container,
                previous=previous,
                tail_lines=tail_lines,
            )
        except QueryError:
            raise
        except Exception:
            raise QueryError("ui-logs-unavailable", 503) from None
        finally:
            if reader is not None:
                reader.close()
        bounded = raw[-1_048_576:]
        lines = bounded.splitlines()[-tail_lines:]
        cursor = hashlib.sha256(
            "\0".join((pod_uid, container, str(previous), bounded)).encode()
        ).hexdigest()[:24]
        return LogBatch(
            lines=lines,
            cursor=cursor,
            dropped=0,
            gap=len(raw.encode("utf-8")) >= 1_048_576 or len(raw) > len(bounded),
        )


def _owners(raw: dict[str, Any]) -> set[str]:
    return {
        item["uid"]
        for item in (raw.get("metadata") or {}).get("ownerReferences", [])
        if isinstance(item, dict) and isinstance(item.get("uid"), str)
    }
