"""Bounded read adapters over explicit Kubernetes targets and release evidence."""

from __future__ import annotations

import hashlib
import json
import secrets
import tempfile
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from piceli.k8s.ops.provider_factory import build_provider
from piceli.services.authority import ScopePolicy, active_principal
from piceli.services.contracts import (
    Application,
    ApplicationPage,
    Capabilities,
    Capability,
    Freshness,
    PartialError,
    Principal,
    Resource,
    ResourceIdentity,
    ResourcePage,
)
from piceli.services.observation import ObservationHub
from piceli.services.registration import Registration


class QueryError(Exception):
    """Fixed public errors; provider exception text never crosses the boundary."""

    def __init__(self, code: str, status: int = 404) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


class Reader(Protocol):
    def list(
        self, api_version: str, kind: str, namespace: str
    ) -> list[dict[str, Any]]: ...

    def logs(
        self,
        pod: str,
        namespace: str,
        container: str,
        *,
        previous: bool,
        tail_lines: int,
    ) -> str: ...

    def close(self) -> None: ...


class KubernetesReader:
    """Real dynamic discovery with namespace/cluster identity verification."""

    def __init__(self, registration: Registration) -> None:
        from kubernetes.dynamic import DynamicClient

        self.binding = build_provider(
            registration.target,
            field_manager="piceli-ui-observer",
            owner_id="piceli-ui-observer",
        )
        try:
            self._cache = tempfile.TemporaryDirectory(prefix="piceli-ui-discovery-")
        except BaseException:
            self.binding.close()
            raise
        try:
            self.client = DynamicClient(
                self.binding.provider.client,
                cache_file=str(Path(self._cache.name) / "discovery.json"),
            )
        except BaseException:
            try:
                self.binding.close()
            finally:
                self._cache.cleanup()
            raise
        self.request_seconds = registration.target.request_seconds
        self.identity = self.binding.identity
        self.last_versions: dict[tuple[str, str, str], str] = {}

    def list(self, api_version: str, kind: str, namespace: str) -> list[dict[str, Any]]:
        resource = self.client.resources.get(api_version=api_version, kind=kind)
        if not resource.namespaced:
            return []
        response = resource.get(
            namespace=namespace,
            limit=1000,
            _request_timeout=self.request_seconds,
        ).to_dict()
        if response.get("metadata", {}).get("continue"):
            # Never present a silently truncated kind as a complete observation.
            raise QueryError("ui-observation-unavailable", 503)
        version = response.get("metadata", {}).get("resourceVersion")
        if isinstance(version, str):
            self.last_versions[(api_version, kind, namespace)] = version
        return list(response.get("items", []))

    def watch(
        self,
        api_version: str,
        kind: str,
        namespace: str,
        resource_version: str,
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        from kubernetes.watch import Watch

        resource = self.client.resources.get(api_version=api_version, kind=kind)
        watcher = Watch()
        try:
            for event in watcher.stream(
                resource.get,
                namespace=namespace,
                resource_version=resource_version,
                timeout_seconds=10,
                _request_timeout=(5, 15),
                serialize=False,
            ):
                raw = event.get("raw_object")
                if isinstance(raw, dict) and isinstance(event.get("type"), str):
                    yield event["type"], raw
        finally:
            watcher.stop()

    def close(self) -> None:
        try:
            self.binding.close()
        finally:
            self._cache.cleanup()

    def logs(
        self,
        pod: str,
        namespace: str,
        container: str,
        *,
        previous: bool,
        tail_lines: int,
    ) -> str:
        from kubernetes.client import CoreV1Api

        result = CoreV1Api(self.binding.provider.client).read_namespaced_pod_log(
            pod,
            namespace,
            container=container,
            previous=previous,
            timestamps=True,
            tail_lines=tail_lines,
            limit_bytes=1_048_576,
            follow=False,
            _request_timeout=self.request_seconds,
            # The generated client turns a text body into "b'...'" when it
            # preloads it; read the bytes and decode them here.
            _preload_content=False,
        )
        try:
            raw = result.read(1_048_576 + 1) if hasattr(result, "read") else result
        finally:
            if hasattr(result, "close"):
                result.close()
            if hasattr(result, "release_conn"):
                result.release_conn()
        return (
            raw.decode("utf-8", errors="replace")
            if isinstance(raw, bytes)
            else str(raw)
        )


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _disabled(reason: str) -> Capability:
    return Capability(allowed=False, reason=reason)


def _health(
    raw: Mapping[str, Any],
) -> Literal["healthy", "progressing", "degraded", "suspended", "unknown"]:
    status = raw.get("status") or {}
    spec = raw.get("spec") or {}
    generation = (raw.get("metadata") or {}).get("generation")
    kind = raw.get("kind")
    conditions = status.get("conditions") or []
    if kind == "CronJob" and spec.get("suspend") is True:
        return "suspended"
    if kind == "Pod":
        if status.get("phase") == "Failed":
            return "degraded"
        if any(
            c.get("type") == "Ready" and c.get("status") == "True" for c in conditions
        ):
            return "healthy"
        return (
            "progressing"
            if status.get("phase") in {"Pending", "Running"}
            else "unknown"
        )
    if kind in {"Deployment", "StatefulSet", "DaemonSet"}:
        observed = status.get("observedGeneration")
        if not isinstance(generation, int) or not isinstance(observed, int):
            return "unknown"
        if observed < generation:
            return "progressing"
        if any(
            c.get("type") == "Progressing" and c.get("status") == "False"
            for c in conditions
        ):
            return "degraded"
        desired = (
            status.get("desiredNumberScheduled")
            if kind == "DaemonSet"
            else spec.get("replicas", 1)
        )
        ready = (
            status.get("numberReady")
            if kind == "DaemonSet"
            else status.get("readyReplicas", 0)
        )
        updated = (
            status.get("updatedNumberScheduled")
            if kind == "DaemonSet"
            else status.get("updatedReplicas", 0)
        )
        if all(isinstance(n, int) for n in (desired, ready, updated)):
            return "healthy" if ready == desired == updated else "progressing"
    return "unknown"


def _resource(registration: Registration, raw: Mapping[str, Any]) -> Resource:
    metadata = raw.get("metadata") or {}
    namespace = metadata.get("namespace")
    if namespace != registration.target.namespace:
        raise ValueError("resource outside registered namespace")
    api_version, kind, name = raw["apiVersion"], raw["kind"], metadata["name"]
    if not all(isinstance(value, str) and value for value in (api_version, kind, name)):
        raise ValueError("invalid resource identity")
    target = registration.public_target()
    identity = ResourceIdentity(
        target_id=target.id,
        api_version=api_version,
        kind=kind,
        namespace=namespace,
        name=name,
        uid=metadata.get("uid"),
    )
    # Logical identity survives pod/resource UID replacement; UID remains explicit.
    resource_id = hashlib.sha256(
        "\0".join((target.id, api_version, kind, namespace, name)).encode()
    ).hexdigest()[:32]
    spec = raw.get("spec") or {}
    pod = spec.get("template", {}).get("spec", spec)
    containers = [
        str(item["name"])
        for key in ("containers", "initContainers")
        for item in pod.get(key, [])
        if isinstance(item.get("name"), str)
    ]
    ports = sorted(
        {
            value
            for value in (
                *(
                    port.get("port")
                    for port in spec.get("ports", [])
                    if isinstance(port, dict)
                ),
                *(
                    port.get("containerPort")
                    for key in ("containers", "initContainers")
                    for item in pod.get(key, [])
                    for port in item.get("ports", [])
                    if isinstance(port, dict)
                ),
            )
            if isinstance(value, int)
            and not isinstance(value, bool)
            and 1 <= value <= 65535
        }
    )
    images = sorted(
        {
            item["image"]
            for key in ("containers", "initContainers")
            for item in pod.get(key, [])
            if isinstance(item.get("image"), str)
        }
    )
    conditions = [
        {
            key: item[key]
            for key in ("type", "status", "lastTransitionTime")
            if isinstance(item.get(key), str)
        }
        for item in (raw.get("status") or {}).get("conditions", [])
        if isinstance(item, dict)
    ]
    # No arbitrary labels, annotations, config data, env or provider messages.
    return Resource(
        id=resource_id,
        identity=identity,
        resource_version=metadata.get("resourceVersion"),
        presence="present",
        ownership="external" if registration.ownership == "external" else "unknown",
        health=_health(raw),
        relation="external" if registration.ownership == "external" else "unknown",
        phase=(raw.get("status") or {}).get("phase"),
        images=images,
        ports=ports,
        containers=containers,
        owner_uids=[
            str(item["uid"])
            for item in metadata.get("ownerReferences", [])
            if item.get("uid")
        ],
        conditions=conditions,
        capabilities={
            "inspect": Capability(allowed=True),
            "conditions": Capability(
                allowed=bool(conditions), reason=None if conditions else "not-observed"
            ),
            "manifest": _disabled("redacted-manifest-unavailable"),
            "logs": _disabled("not-implemented"),
            "access": _disabled("not-implemented"),
        },
    )


class QueryService:
    """Read service with bounded immutable snapshots and opaque paging tokens."""

    def __init__(
        self,
        registrations: Sequence[Registration],
        *,
        reader_factory: Callable[[Registration], Reader] = KubernetesReader,
        page_size: int = 100,
        snapshot_limit: int = 32,
        scope_policy: ScopePolicy | None = None,
    ) -> None:
        if not 1 <= page_size <= 1000 or not 1 <= snapshot_limit <= 256:
            raise ValueError("invalid query bounds")
        self.registrations = {item.id: item for item in registrations}
        if len(self.registrations) != len(registrations):
            raise ValueError("duplicate registration")
        self.reader_factory = reader_factory
        self.page_size = page_size
        self.snapshot_limit = snapshot_limit
        self.scope_policy = scope_policy
        self._snapshots: OrderedDict[
            str, tuple[str, ResourcePage | ApplicationPage]
        ] = OrderedDict()
        self._lock = threading.RLock()
        self.observation = ObservationHub(reader_factory)
        self.delivery_capabilities: (
            Callable[[Registration], dict[str, Capability]] | None
        ) = None
        self.access_capability: (
            Callable[[Registration, Resource | None], Capability] | None
        ) = None
        self.logs_capability: (
            Callable[[Registration, Resource | None], Capability] | None
        ) = None

    def _principal(self) -> Principal:
        if self.scope_policy is None:
            return Principal(id="local", name="Local operator")
        principal = active_principal()
        if principal is None or principal.kind == "local":
            raise QueryError("ui-request-rejected", 403)
        return principal

    def _allowed(self, id: str, action: str = "inspect") -> bool:
        if self.scope_policy is None:
            return True
        return self.scope_policy.allows(self._principal().id, id, action)

    def _snapshot_scope(self, scope: str) -> str:
        principal = self._principal()
        revision = self.scope_policy.revision() if self.scope_policy else 0
        return f"{principal.id}\0{revision}\0{scope}"

    def registration(self, id: str, *, action: str = "inspect") -> Registration:
        if not self._allowed(id, action):
            raise QueryError("ui-not-found")
        try:
            return self.registrations[id]
        except KeyError:
            raise QueryError("ui-not-found") from None

    def register_local(self, registration: Registration) -> None:
        """Expose an operator-derived scope; never accept one in scoped OIDC mode."""
        if self.scope_policy is not None:
            raise ValueError("scoped registrations are fixed at installation")
        with self._lock:
            previous = self.registrations.get(registration.id)
            if previous is not None:
                if (
                    previous.target.kubeconfig != registration.target.kubeconfig
                    or previous.target.context != registration.target.context
                    or previous.target.namespace != registration.target.namespace
                ):
                    raise ValueError("local registration identity changed")
                return  # retain observed target UID pins
            self.registrations[registration.id] = registration

    def remove_local(self, id: str) -> None:
        if self.scope_policy is not None:
            raise ValueError("scoped registrations are fixed at installation")
        with self._lock:
            self.registrations.pop(id, None)

    def close(self) -> None:
        self.observation.close()

    def _pin_target(self, registration: Registration, reader: Reader) -> Registration:
        """Bind the first real observation; refuse a retargeted context thereafter."""
        identity = getattr(reader, "identity", None)
        if identity is None:
            return registration
        with self._lock:
            current = self.registrations[registration.id]
            target = current.target
            if any(
                pin is not None and pin != actual
                for pin, actual in (
                    (target.cluster_uid, identity.cluster_uid),
                    (target.namespace_uid, identity.namespace_uid),
                )
            ):
                raise QueryError("ui-observation-unavailable", 503)
            pinned = replace(
                current,
                target=replace(
                    target,
                    cluster_uid=identity.cluster_uid,
                    namespace_uid=identity.namespace_uid,
                ),
            )
            self.registrations[registration.id] = pinned
            return pinned

    def capabilities(self) -> Capabilities:
        principal = self._principal()
        visible = [r for r in self.registrations.values() if self._allowed(r.id)]
        targets = {r.public_target().id: r.public_target() for r in visible}
        result = Capabilities(
            mode="cluster" if self.scope_policy else "local",
            principal=principal,
            targets=list(targets.values()),
            actions={
                "inspect": Capability(
                    allowed=bool(visible),
                    reason=None if visible else "not-authorized",
                ),
                "activity": Capability(
                    allowed=self.delivery_capabilities is not None
                    and any(self._allowed(r.id, "activity") for r in visible),
                    reason=None
                    if self.delivery_capabilities is not None
                    and any(self._allowed(r.id, "activity") for r in visible)
                    else "operations-not-configured",
                ),
                "plan": _disabled("not-implemented"),
                "deploy": _disabled("not-implemented"),
                "logs": _disabled("not-implemented"),
                "access": _disabled("not-implemented"),
            },
        )
        if self.delivery_capabilities is not None:
            actions = dict(result.actions)
            for name in ("evaluate", "plan", "deploy", "rollback"):
                allowed = any(
                    self.delivery_capabilities(registration)[name].allowed
                    for registration in visible
                    if self._allowed(registration.id, name)
                )
                actions[name] = Capability(
                    allowed=allowed,
                    reason=None if allowed else "source-evaluation-not-configured",
                )
            result = result.model_copy(update={"actions": actions})
        if self.access_capability is not None:
            actions = dict(result.actions)
            allowed = any(
                self.access_capability(registration, None).allowed
                for registration in visible
                if self._allowed(registration.id, "access")
            )
            actions["access"] = Capability(
                allowed=allowed, reason=None if allowed else "local-access-unavailable"
            )
            result = result.model_copy(update={"actions": actions})
        if self.logs_capability is not None:
            actions = dict(result.actions)
            allowed = any(
                self.logs_capability(registration, None).allowed
                for registration in visible
                if self._allowed(registration.id, "logs")
            )
            actions["logs"] = Capability(
                allowed=allowed, reason=None if allowed else "logs-unavailable"
            )
            result = result.model_copy(update={"actions": actions})
        return result

    def _application(self, registration: Registration) -> Application:
        application = Application(
            id=registration.id,
            name=registration.name,
            target=registration.public_target(),
            definition_kind=registration.definition_kind,
            ownership=registration.ownership,
            source=registration.source,
            relation="external" if registration.ownership == "external" else "unknown",
            freshness=Freshness(state="unavailable", reason="not-observed"),
            capabilities={
                "inspect": Capability(allowed=True),
                "activity": Capability(
                    allowed=self.delivery_capabilities is not None
                    and self._allowed(registration.id, "activity"),
                    reason=None
                    if self.delivery_capabilities is not None
                    and self._allowed(registration.id, "activity")
                    else "operations-not-configured",
                ),
                "plan": _disabled("not-implemented"),
                "deploy": _disabled("not-implemented"),
            },
        )
        if self.delivery_capabilities is not None:
            delivery = {
                action: capability
                if self._allowed(registration.id, action)
                else _disabled("not-authorized")
                for action, capability in self.delivery_capabilities(
                    registration
                ).items()
            }
            application = application.model_copy(
                update={
                    "capabilities": {
                        **application.capabilities,
                        **delivery,
                    }
                }
            )
        with self._lock:
            snapshot = next(
                (
                    page
                    for scope, page in reversed(self._snapshots.values())
                    if scope == self._snapshot_scope(registration.id)
                    and isinstance(page, ResourcePage)
                ),
                None,
            )
        if snapshot is None:
            return application
        freshness = snapshot.freshness
        if freshness.observed_at is not None:
            age = datetime.now(UTC) - datetime.fromisoformat(freshness.observed_at)
            if age.total_seconds() > 30:
                freshness = freshness.model_copy(update={"state": "stale"})
        health = "unknown"
        states = {item.health for item in snapshot.items}
        if not snapshot.partial:
            health = next(
                (
                    state
                    for state in ("degraded", "progressing", "suspended", "healthy")
                    if state in states
                ),
                "unknown",
            )
        return application.model_copy(update={"health": health, "freshness": freshness})

    def application(self, id: str) -> Application:
        return self._application(self.registration(id))

    def _remember(self, scope: str, page: ResourcePage | ApplicationPage) -> None:
        with self._lock:
            self._snapshots[page.cursor] = (self._snapshot_scope(scope), page)
            while len(self._snapshots) > self.snapshot_limit:
                self._snapshots.popitem(last=False)

    def _slice(self, scope: str, cursor: str) -> ResourcePage | ApplicationPage:
        try:
            token, raw_offset = cursor.rsplit(".", 1)
            offset = int(raw_offset)
            with self._lock:
                saved_scope, page = self._snapshots[token]
            if (
                saved_scope != self._snapshot_scope(scope)
                or offset < 0
                or offset % self.page_size
            ):
                raise ValueError("invalid page")
        except (ValueError, KeyError):
            raise QueryError("ui-invalid-request", 409) from None
        end = offset + self.page_size
        return page.model_copy(
            update={
                "items": page.items[offset:end],
                "next_page": f"{token}.{end}" if end < len(page.items) else None,
            }
        )

    def applications(self, cursor: str | None = None) -> ApplicationPage:
        self._principal()
        if cursor is None:
            page = ApplicationPage(
                items=[
                    self._application(r)
                    for r in self.registrations.values()
                    if self._allowed(r.id)
                ],
                cursor=secrets.token_urlsafe(24),
            )
            self._remember("applications", page)
            cursor = f"{page.cursor}.0"
        result = self._slice("applications", cursor)
        assert isinstance(result, ApplicationPage)
        return result

    def resources(
        self,
        id: str,
        cursor: str | None = None,
        *,
        fresh: bool = False,
        remember: bool = True,
    ) -> ResourcePage:
        registration = self.registration(id)
        if cursor is None:
            event_cursor = self.observation.cursor()
            items: list[Resource] = []
            partial: list[PartialError] = []
            reader: Reader | None = None
            try:
                reader = self.reader_factory(registration)
                registration = self._pin_target(registration, reader)
                for api_version, kind in registration.kinds:
                    try:
                        rows, stale = self.observation.observe(
                            registration,
                            reader,
                            api_version,
                            kind,
                            fresh=fresh,
                        )
                        for raw in rows:
                            item = _resource(registration, raw)
                            if self.access_capability is not None:
                                item = item.model_copy(
                                    update={
                                        "capabilities": {
                                            **item.capabilities,
                                            "access": self.access_capability(
                                                registration, item
                                            )
                                            if self._allowed(registration.id, "access")
                                            else _disabled("not-authorized"),
                                        }
                                    }
                                )
                            if self.logs_capability is not None:
                                item = item.model_copy(
                                    update={
                                        "capabilities": {
                                            **item.capabilities,
                                            "logs": self.logs_capability(
                                                registration, item
                                            )
                                            if self._allowed(registration.id, "logs")
                                            else _disabled("not-authorized"),
                                        }
                                    }
                                )
                            items.append(item)
                        if stale:
                            partial.append(
                                PartialError(
                                    scope=f"{api_version}/{kind}",
                                    code="ui-observation-unavailable",
                                )
                            )
                    except Exception:
                        partial.append(
                            PartialError(
                                scope=f"{api_version}/{kind}",
                                code="ui-observation-unavailable",
                            )
                        )
            except Exception:
                partial.append(
                    PartialError(scope="target", code="ui-observation-unavailable")
                )
            finally:
                if reader is not None:
                    reader.close()
            page = ResourcePage(
                items=sorted(
                    items, key=lambda item: (item.identity.kind, item.identity.name)
                ),
                cursor=secrets.token_urlsafe(24),
                event_cursor=event_cursor,
                freshness=Freshness(
                    state="unavailable" if partial else "connected",
                    observed_at=_now(),
                    reason="partial-observation" if partial else None,
                ),
                partial=partial,
            )
            if not remember:
                return page
            self._remember(id, page)
            cursor = f"{page.cursor}.0"
        result = self._slice(id, cursor)
        assert isinstance(result, ResourcePage)
        return result

    def resource(self, id: str, resource_id: str) -> Resource:
        page = self.resources(id, fresh=True, remember=False)
        for item in page.items:
            if item.id == resource_id:
                return item
        if page.partial:
            raise QueryError("ui-observation-unavailable", 503)
        raise QueryError("ui-not-found")

    def saved_plans(self, id: str) -> list[dict[str, str]]:
        """Read only public pending-plan metadata, never private discovery bodies.

        These are engine records, not complete service PlanRecords or renewed
        approvals. Full reviewed projections are persisted by the operation layer.
        """
        registration = self.registration(id)
        if registration.release_spec is None:
            return []
        directory = registration.release_spec.state_dir / "plans"
        result = []
        for path in sorted(directory.glob("*.json")):
            if len(path.stem) != 64 or any(
                c not in "0123456789abcdef" for c in path.stem
            ):
                continue
            try:
                if path.stat().st_size > 16_000_000:
                    continue
                value = json.loads(path.read_text())
                row = {key: value[key] for key in ("release", "intent", "expires_at")}
                if all(isinstance(item, str) for item in row.values()):
                    result.append({"digest": path.stem, **row})
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return result
