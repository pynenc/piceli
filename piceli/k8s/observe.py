"""Read-only, local cluster operations primitives.

This module intentionally separates observation from deployment execution.  It
reconciles the redacted resource identities in a durable Piceli session archive
with live Kubernetes observations and reports objects that are not declared by
that archive.  It never applies, adopts, deletes, or resolves Secret values.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import signal
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from piceli.k8s.ops.discovery import ResourceIdentity
from piceli.k8s.ops.session import DeploymentSessionArchive
from piceli.k8s.port_owner import PortOwner, port_owner
from piceli.k8s.ui_config import HealthProbe, RestartPolicy, UiShortcut, legacy_health

if TYPE_CHECKING:
    from piceli.k8s.ops.exec_credentials import ExecPolicy

_NAME = re.compile(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?")
_FORWARD_TARGET = re.compile(
    r"(?:service|pod|deployment)/[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?"
)
_LOG_TARGET = re.compile(
    r"(?:pod|deployment|statefulset|daemonset|job)/[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?"
)
_COMMON_TYPES = (
    ("v1", "ConfigMap"),
    ("v1", "PersistentVolumeClaim"),
    ("v1", "Pod"),
    ("v1", "Secret"),
    ("v1", "Service"),
    ("v1", "ServiceAccount"),
    ("apps/v1", "DaemonSet"),
    ("apps/v1", "Deployment"),
    ("apps/v1", "StatefulSet"),
    ("batch/v1", "CronJob"),
    ("batch/v1", "Job"),
    ("rbac.authorization.k8s.io/v1", "Role"),
    ("rbac.authorization.k8s.io/v1", "RoleBinding"),
)


@dataclass(frozen=True, order=True)
class ObservationRef:
    """The public identity of a Kubernetes object that Piceli can observe."""

    api_version: str
    kind: str
    namespace: str
    name: str

    @classmethod
    def from_resource(cls, resource: ResourceIdentity) -> ObservationRef:
        """Convert the planning identity without carrying a manifest or secret."""
        return cls(
            api_version=resource.api_version,
            kind=resource.kind,
            namespace=resource.namespace,
            name=resource.name,
        )

    def __post_init__(self) -> None:
        ResourceIdentity(self.api_version, self.kind, self.namespace, self.name)


@dataclass(frozen=True)
class ObservedObject:
    """A redacted live object summary returned by an inventory reader."""

    ref: ObservationRef
    uid: str | None = None
    resource_version: str | None = None
    generation: int | None = None
    phase: str | None = None
    images: tuple[str, ...] = ()
    labels: tuple[tuple[str, str], ...] = ()
    annotations: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if any(not isinstance(value, str) or not value for value in self.images):
            raise ValueError("observed images must be nonempty strings")


class InventoryReader(Protocol):
    """Read-only cluster access required by the local operations lens."""

    def get(self, ref: ObservationRef) -> ObservedObject | None:
        """Return one object, or ``None`` when it is absent."""

    def list(
        self, api_version: str, kind: str, namespace: str
    ) -> Iterable[ObservedObject]:
        """Return objects of one type in a namespace."""


@dataclass(frozen=True)
class InventoryEntry:
    """One declared object and its current availability classification."""

    ref: ObservationRef
    state: str
    observed: ObservedObject | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.state not in {"present", "missing", "unknown"}:
            raise ValueError("invalid inventory entry state")
        if self.state == "present" and self.observed is None:
            raise ValueError("present inventory entry requires an observation")
        if self.state == "unknown" and not self.error:
            raise ValueError("unknown inventory entry requires an error")


@dataclass(frozen=True)
class InventoryReport:
    """Read-only desired-versus-live inventory for one Piceli session archive."""

    session_id: str
    declared: tuple[InventoryEntry, ...]
    undeclared: tuple[ObservedObject, ...]
    scan_errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return public JSON without manifests, opaque references, or Secret data."""
        return {
            "session_id": self.session_id,
            "declared": [_entry_dict(entry) for entry in self.declared],
            "undeclared": [_observed_dict(item) for item in self.undeclared],
            "scan_errors": list(self.scan_errors),
        }


def _observed_dict(observed: ObservedObject) -> dict[str, Any]:
    return {
        "api_version": observed.ref.api_version,
        "kind": observed.ref.kind,
        "namespace": observed.ref.namespace,
        "name": observed.ref.name,
        "uid": observed.uid,
        "resource_version": observed.resource_version,
        "generation": observed.generation,
        "phase": observed.phase,
        "images": list(observed.images),
    }


def _entry_dict(entry: InventoryEntry) -> dict[str, Any]:
    result = {"ref": asdict(entry.ref), "state": entry.state}
    if entry.observed is not None:
        result["observed"] = _observed_dict(entry.observed)
    if entry.error is not None:
        result["error"] = entry.error
    return result


def archive_resources(archive: DeploymentSessionArchive) -> tuple[ObservationRef, ...]:
    """Read declared public resource identities from a canonical session archive."""
    refs: set[ObservationRef] = set()
    for component in archive.to_dict()["composition"]:
        items = component.get("resources", [component])
        for resource in items:
            reference = resource.get("resource", resource)
            if (
                isinstance(reference, Mapping)
                and "kind" in reference
                and "name" in reference
            ):
                refs.add(
                    ObservationRef(
                        api_version=str(reference.get("api_version", "v1")),
                        kind=str(reference["kind"]),
                        namespace=str(reference.get("namespace", "")),
                        name=str(reference["name"]),
                    )
                )
    return tuple(sorted(refs))


def observe_session(
    archive: DeploymentSessionArchive,
    reader: InventoryReader,
    *,
    include_common_types: bool = True,
) -> InventoryReport:
    """Reconcile a session archive with a reader without invoking mutations.

    Reader failures are reported as ``unknown`` rather than being treated as an
    absent object.  An undeclared object means only that it is not in this
    archive, not that Piceli is entitled to adopt or delete it.
    """
    refs = archive_resources(archive)
    entries: list[InventoryEntry] = []
    for ref in refs:
        try:
            observed = reader.get(ref)
        except Exception as error:  # Reader implementations own transport detail.
            entries.append(InventoryEntry(ref, "unknown", error=type(error).__name__))
            continue
        if observed is None:
            entries.append(InventoryEntry(ref, "missing"))
        else:
            entries.append(InventoryEntry(ref, "present", observed=observed))

    declared = set(refs)
    types = {(ref.api_version, ref.kind) for ref in refs}
    if include_common_types:
        types.update(_COMMON_TYPES)
    undeclared: set[ObservedObject] = set()
    errors: list[str] = []
    namespaces = {ref.namespace for ref in refs if ref.namespace}
    for namespace in sorted(namespaces):
        for api_version, kind in sorted(types):
            try:
                # Dynamic Kubernetes readers can defer decoding until iteration.
                # Materialize inside this boundary so one malformed or
                # cluster-scoped object becomes an honest scan warning rather
                # than taking down the complete local dashboard.
                objects = tuple(reader.list(api_version, kind, namespace))
            except Exception as error:  # Access boundaries remain visible to callers.
                errors.append(f"{api_version}/{kind}: {type(error).__name__}")
                continue
            undeclared.update(item for item in objects if item.ref not in declared)
    return InventoryReport(
        session_id=archive.session_id,
        declared=tuple(entries),
        undeclared=tuple(sorted(undeclared, key=lambda item: item.ref)),
        scan_errors=tuple(sorted(errors)),
    )


class KubernetesDynamicInventoryReader:
    """Lazy Kubernetes-client adapter for explicit local kubeconfig observation.

    The client comes from the provider factory: an explicit kubeconfig file and
    named context (never ``current-context``), the factory's refusals (proxy,
    insecure TLS, auth-provider), and exec plugins only with ``exec_policy``.
    """

    def __init__(
        self,
        *,
        kubeconfig: Path,
        context: str,
        exec_policy: ExecPolicy | None = None,
    ) -> None:
        from kubernetes.dynamic import DynamicClient

        from piceli.k8s.ops.provider_factory import api_client_from_kubeconfig

        self._client = DynamicClient(
            api_client_from_kubeconfig(kubeconfig, context, exec_policy=exec_policy)
        )

    @staticmethod
    def _resource(api_version: str, kind: str, client: Any) -> Any:
        return client.resources.get(api_version=api_version, kind=kind)

    @staticmethod
    def _summary(value: Mapping[str, Any], fallback: ObservationRef) -> ObservedObject:
        metadata = value.get("metadata", {})
        status = value.get("status", {})
        spec = value.get("spec", {})
        templates = [spec]
        template = spec.get("template") if isinstance(spec, Mapping) else None
        if isinstance(template, Mapping):
            templates.append(template.get("spec", {}))
        images = sorted(
            {
                str(container["image"])
                for candidate in templates
                if isinstance(candidate, Mapping)
                for key in ("containers", "initContainers")
                for container in candidate.get(key, [])
                if isinstance(container, Mapping)
                and isinstance(container.get("image"), str)
            }
        )
        phase = status.get("phase") if isinstance(status, Mapping) else None
        raw_labels = metadata.get("labels") or {}
        lbls = tuple(sorted((str(k), str(v)) for k, v in raw_labels.items()))
        raw_ann = metadata.get("annotations") or {}
        anns = tuple(sorted((str(k), str(v)) for k, v in raw_ann.items()))
        return ObservedObject(
            ref=fallback,
            uid=_string(metadata.get("uid")),
            resource_version=_string(metadata.get("resourceVersion")),
            generation=_integer(metadata.get("generation")),
            phase=_string(phase),
            images=tuple(images),
            labels=lbls,
            annotations=anns,
        )

    def get(self, ref: ObservationRef) -> ObservedObject | None:
        from kubernetes.client.exceptions import ApiException

        resource = self._resource(ref.api_version, ref.kind, self._client)
        try:
            result = resource.get(name=ref.name, namespace=ref.namespace or None)
        except ApiException as error:
            if error.status == 404:
                return None
            raise
        return self._summary(result.to_dict(), ref)

    def list(
        self, api_version: str, kind: str, namespace: str
    ) -> Iterable[ObservedObject]:
        resource = self._resource(api_version, kind, self._client)
        result = resource.get(namespace=namespace)
        items = result.to_dict().get("items", [])
        for value in items:
            metadata = value.get("metadata", {})
            name = metadata.get("name")
            if isinstance(name, str):
                yield self._summary(
                    value,
                    ObservationRef(api_version, kind, namespace, name),
                )


def _string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


@dataclass(frozen=True)
class PortForward:
    """One non-secret local forwarding preference for a user profile."""

    name: str
    namespace: str
    target: str
    local_port: int
    remote_port: int
    health_path: str | None = None

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.name) or not _NAME.fullmatch(self.namespace):
            raise ValueError("invalid forward name or namespace")
        if not _FORWARD_TARGET.fullmatch(self.target):
            raise ValueError(
                "forward target must be service/NAME, deployment/NAME or pod/NAME"
            )
        for port in (self.local_port, self.remote_port):
            if (
                not isinstance(port, int)
                or isinstance(port, bool)
                or not 1 <= port <= 65535
            ):
                raise ValueError("forward ports must be between 1 and 65535")
        if self.health_path is not None and (
            not self.health_path.startswith("/")
            or "\\r" in self.health_path
            or "\\n" in self.health_path
        ):
            raise ValueError("forward health path must be a safe absolute path")

    def command(
        self, *, kubectl: str, kubeconfig: Path, context: str | None
    ) -> list[str]:
        """Build the explicit, shell-free kubectl command for this preference."""
        result = kubectl_target(kubectl, kubeconfig, context)
        return [
            *result,
            "--namespace",
            self.namespace,
            "port-forward",
            self.target,
            f"{self.local_port}:{self.remote_port}",
            "--address",
            "127.0.0.1",
        ]

    def public_dict(self) -> dict[str, Any]:
        """Return stable non-secret preference JSON without optional unset fields."""
        value: dict[str, Any] = {
            "name": self.name,
            "namespace": self.namespace,
            "target": self.target,
            "local_port": self.local_port,
            "remote_port": self.remote_port,
        }
        if self.health_path is not None:
            value["health_path"] = self.health_path
        return value


@dataclass(frozen=True)
class UserPreferences:
    """Local user choices; never a credential, cluster secret, or deployment grant."""

    user: str
    forwards: tuple[PortForward, ...] = ()

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.user):
            raise ValueError("invalid user preference identity")
        names = [forward.name for forward in self.forwards]
        if len(names) != len(set(names)):
            raise ValueError("duplicate forward preference")


class PreferenceStore:
    """Mode-restricted JSON store for local, non-secret operations preferences."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path.home() / ".config" / "piceli" / "observe.json"

    def load(self) -> dict[str, UserPreferences]:
        """Load validated preferences, treating a missing file as an empty store."""
        if not self.path.exists():
            return {}
        value = json.loads(self.path.read_text())
        if not isinstance(value, dict) or set(value) != {"schema_version", "users"}:
            raise ValueError("invalid Piceli preference store")
        if value["schema_version"] != 1 or not isinstance(value["users"], list):
            raise ValueError("unsupported Piceli preference store")
        users: dict[str, UserPreferences] = {}
        for item in value["users"]:
            if not isinstance(item, dict) or set(item) != {"user", "forwards"}:
                raise ValueError("invalid Piceli user preference")
            forward_values = item["forwards"]
            if not isinstance(forward_values, list):
                raise ValueError("invalid Piceli forwards")
            preference = UserPreferences(
                user=item["user"],
                forwards=tuple(PortForward(**forward) for forward in forward_values),
            )
            if preference.user in users:
                raise ValueError("duplicate Piceli user preference")
            users[preference.user] = preference
        return users

    def save(self, preferences: Mapping[str, UserPreferences]) -> None:
        """Atomically store validated local preferences with owner-only permissions."""
        if set(preferences) != {item.user for item in preferences.values()}:
            raise ValueError("preference keys must match their users")
        value = {
            "schema_version": 1,
            "users": [
                {
                    "user": item.user,
                    "forwards": [asdict(forward) for forward in item.forwards],
                }
                for item in sorted(preferences.values(), key=lambda item: item.user)
            ],
        }
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=".observe-", dir=self.path.parent
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w") as handle:
                json.dump(value, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def replace_user(self, preference: UserPreferences) -> None:
        """Replace one user's complete non-secret forwarding preference set."""
        preferences = self.load()
        preferences[preference.user] = preference
        self.save(preferences)


_FORWARD_STATES = frozenset(
    {"stopped", "starting", "running", "degraded", "backoff", "failed"}
)
_HEALTH_STATES = frozenset(
    {
        "unknown",
        "stopped",
        "starting",
        "healthy",
        "unhealthy",
        "restarting",
        "conflict",
        "failed",
    }
)
_OCCUPIED = "loopback port is occupied by an external process"


def _iso(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="seconds")


def probe_endpoint(
    port: int, probe: HealthProbe, host: str = "127.0.0.1"
) -> str | None:
    """Probe one loopback endpoint; return ``None`` when healthy, else a short reason.

    The reason is a fixed, non-secret phrase suitable for status JSON.  A TCP
    probe that is accepted and then closed or reset within ``probe.settle``
    seconds fails: that is how a port forward with a dead upstream behaves.
    """
    try:
        if probe.type == "http":
            connection = http.client.HTTPConnection(host, port, timeout=probe.timeout)
            try:
                connection.request("GET", probe.path, headers={"Connection": "close"})
                status = connection.getresponse().status
            finally:
                connection.close()
            low, high = probe.expect_status
            if low <= status <= high:
                return None
            return f"http status {status} outside {low}-{high}"
        with socket.create_connection((host, port), timeout=probe.timeout) as client:
            if probe.settle <= 0:
                return None
            client.settimeout(probe.settle)
            try:
                data = client.recv(1)
            except TimeoutError:
                return None
            return None if data else "connection closed by forward"
    except ConnectionRefusedError:
        return "connection refused"
    except ConnectionResetError:
        return "connection reset"
    except TimeoutError:
        return "timed out"
    except (OSError, http.client.HTTPException) as error:
        return type(error).__name__


def local_port_in_use(port: int) -> bool:
    """Return whether something else already serves ``127.0.0.1:port``.

    Both an accepted connection and a failed bind count as in use.  The bind
    check uses ``SO_REUSEADDR`` so that lingering ``TIME_WAIT`` sockets from a
    process this supervisor just stopped do not look like a conflict.
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", port))
    except OSError:
        return True
    return False


@dataclass(frozen=True)
class ForwardStatus:
    """Public runtime state for one locally owned loopback port forward.

    ``state`` describes the process; ``health`` describes the connection as
    seen by the last probe (``healthy``, ``unhealthy``, ``restarting``,
    ``starting``, ``conflict``, ``failed``, ``stopped`` or ``unknown``).
    """

    name: str
    state: str
    pid: int | None = None
    restarts: int = 0
    error: str | None = None
    target: str = ""
    local_port: int = 0
    remote_port: int = 0
    namespace: str = ""
    reachable: bool | None = None
    health: str = "unknown"
    last_error: str | None = None
    last_probe_at: str | None = None
    consecutive_failures: int = 0
    probe: dict[str, Any] | None = None
    #: The process holding the local port on a ``conflict`` (pid, command).
    owner: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.state not in _FORWARD_STATES:
            raise ValueError("invalid port-forward state")
        if self.health not in _HEALTH_STATES:
            raise ValueError("invalid port-forward health")


@dataclass
class _ManagedForward:
    """One process and its bounded restart bookkeeping, owned by this supervisor."""

    forward: PortForward
    probe: HealthProbe = field(default_factory=HealthProbe)
    policy: RestartPolicy = field(default_factory=RestartPolicy)
    process: subprocess.Popen[bytes] | None = None
    restarts: int = 0
    next_start: float = 0.0
    error: str | None = None
    started_at: float = 0.0
    health: str = "unknown"
    last_error: str | None = None
    last_probe_at: float | None = None
    consecutive_failures: int = 0
    attempts: int = 0
    next_probe: float = 0.0
    given_up: bool = False
    stopped: bool = False
    owner: PortOwner | None = None

    def reset(self) -> None:
        """Clear failure bookkeeping before an explicit (re)start."""
        self.next_start = 0.0
        self.error = None
        self.attempts = 0
        self.given_up = False
        self.stopped = False
        self.owner = None


class ForwardSupervisor:
    """Restore and supervise explicitly requested loopback ``kubectl port-forward`` s.

    The supervisor owns only processes it starts in a fresh process group. It
    never discovers, adopts, or kills an ambient ``kubectl`` process. A
    background thread probes every running forward (TCP or HTTP, see
    :class:`~piceli.k8s.ui_config.HealthProbe`) and restarts one whose probe
    fails ``failure_threshold`` consecutive times, with exponential backoff and
    a bounded number of consecutive restarts
    (:class:`~piceli.k8s.ui_config.RestartPolicy`). A local port that is
    already served by another process is reported as a ``conflict`` with the
    owning process (pid and command) and never spawns a process. A conflict
    is final until the forward is started again explicitly: the supervisor
    never waits for a declared port to become free and silently takes it back
    from whoever holds it (for example another dashboard or ``piceli access``).
    """

    def __init__(
        self,
        *,
        preferences: PreferenceStore | None = None,
        user: str = "",
        kubeconfig: Path,
        context: str | None = None,
        kubectl: str = "kubectl",
        shortcuts: Iterable[UiShortcut] = (),
        namespace: str | None = None,
    ) -> None:
        if not context:
            # kubectl would otherwise fall back to the file's current-context.
            raise ValueError("an explicit kubeconfig context is required")
        self._preferences = preferences
        self._user = user
        self._kubeconfig = kubeconfig
        self._context = context
        self._kubectl = kubectl
        self._shortcuts = {shortcut.id: shortcut for shortcut in shortcuts}
        self._namespace = namespace
        self._forwards: dict[str, _ManagedForward] = {}
        self._lock = threading.RLock()
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None

    def restore(self) -> None:
        """Load one user's saved preferences and begin supervising them."""
        if self._preferences is None or not self._user:
            raise ValueError(
                "restoring saved forwards needs a preference store and user"
            )
        preference = self._preferences.load().get(
            self._user, UserPreferences(self._user)
        )
        with self._lock:
            configured = {forward.name: forward for forward in preference.forwards}
            for name in set(self._forwards) - set(configured):
                self._stop_locked(name)
            for name, forward in configured.items():
                managed = self._forwards.get(name)
                if managed is None or managed.forward != forward:
                    if managed is not None:
                        self._stop_locked(name)
                    self._forwards[name] = self._managed(forward)
            self._ensure_locked()
            self._ensure_thread_locked()

    def _managed(self, forward: PortForward) -> _ManagedForward:
        """Wrap ``forward`` with its shortcut's probe/policy, or the legacy probe."""
        shortcut = self._shortcuts.get(forward.name)
        if (
            shortcut is not None
            and shortcut.target == forward.target
            and shortcut.local_port == forward.local_port
            and shortcut.remote_port == forward.remote_port
        ):
            return _ManagedForward(forward, shortcut.probe, shortcut.restart)
        return _ManagedForward(forward, legacy_health(forward.health_path))

    def _status_locked(self, name: str, managed: _ManagedForward) -> ForwardStatus:
        fwd = managed.forward
        process = managed.process
        pid: int | None = None
        reachable: bool | None = None
        health = managed.health
        error = managed.error
        if process is not None and process.poll() is None:
            pid = process.pid
            if health == "healthy":
                state, reachable = "running", True
            elif health == "unhealthy":
                state, reachable = "degraded", False
            else:
                state = "starting"
        elif managed.given_up:
            state = "failed"
        elif managed.stopped:
            state, health = "stopped", "stopped"
        elif (
            process is not None
            or managed.error
            or managed.next_start > time.monotonic()
        ):
            # Exited but not yet reaped by the watcher, or waiting to restart.
            state = "backoff"
            if process is not None:
                health = "restarting"
        else:
            state = "stopped"
        return ForwardStatus(
            name=name,
            state=state,
            pid=pid,
            restarts=managed.restarts,
            error=error,
            target=fwd.target,
            local_port=fwd.local_port,
            remote_port=fwd.remote_port,
            namespace=fwd.namespace,
            reachable=reachable,
            health=health,
            last_error=managed.last_error,
            last_probe_at=_iso(managed.last_probe_at),
            consecutive_failures=managed.consecutive_failures,
            probe=managed.probe.public_dict(),
            owner=managed.owner.to_dict() if managed.owner is not None else None,
        )

    def statuses(self) -> tuple[ForwardStatus, ...]:
        """Return cached process and connection health without blocking on probes."""
        with self._lock:
            return tuple(
                self._status_locked(name, managed)
                for name, managed in sorted(self._forwards.items())
            )

    def start(self, name: str) -> None:
        """Start one saved forward now; unknown names are rejected unless a configured shortcut."""
        with self._lock:
            if name not in self._forwards:
                if name not in self._shortcuts:
                    raise ValueError(f"unknown saved port forward: {name}")
                self._forwards[name] = self._managed(self._shortcut_forward(name))
            managed = self._forwards[name]
            managed.reset()
            self._start_locked(managed)
            self._ensure_thread_locked()

    def quick_start(self, shortcut_id: str, namespace: str | None = None) -> None:
        """Start a configured shortcut by id, automatically registering if absent."""
        with self._lock:
            if shortcut_id not in self._forwards:
                if shortcut_id not in self._shortcuts:
                    raise ValueError(f"unknown shortcut: {shortcut_id}")
                self._forwards[shortcut_id] = self._managed(
                    self._shortcut_forward(shortcut_id, namespace)
                )
            managed = self._forwards[shortcut_id]
            managed.reset()
            self._start_locked(managed)
            self._ensure_thread_locked()

    def _shortcut_namespace(self, shortcut_id: str, namespace: str | None) -> str:
        """A shortcut's pinned namespace wins, then the caller's, then the supervisor's."""
        return (
            self._shortcuts[shortcut_id].namespace or namespace or self._namespace or ""
        )

    def _shortcut_forward(
        self, shortcut_id: str, namespace: str | None = None
    ) -> PortForward:
        shortcut = self._shortcuts[shortcut_id]
        return PortForward(
            name=shortcut_id,
            namespace=self._shortcut_namespace(shortcut_id, namespace),
            target=shortcut.target,
            local_port=shortcut.local_port,
            remote_port=shortcut.remote_port,
            health_path=shortcut.health_path,
        )

    def add_or_update(self, forward: PortForward, persist: bool = True) -> None:
        """Add or update a forward preference and optionally persist it."""
        with self._lock:
            if forward.name in self._forwards:
                managed = self._forwards[forward.name]
                if managed.forward != forward:
                    self._stop_locked(forward.name)
                    self._forwards[forward.name] = self._managed(forward)
            else:
                self._forwards[forward.name] = self._managed(forward)
            self._ensure_thread_locked()
        if persist and self._preferences is not None and self._user:
            try:
                users = self._preferences.load()
                user_pref = users.get(self._user, UserPreferences(self._user))
                existing = [f for f in user_pref.forwards if f.name != forward.name]
                existing.append(forward)
                self._preferences.replace_user(
                    UserPreferences(
                        self._user, tuple(sorted(existing, key=lambda f: f.name))
                    )
                )
            except Exception:
                pass

    def remove(self, name: str, persist: bool = True) -> None:
        """Stop and remove a forward from supervisor and preferences."""
        with self._lock:
            self._stop_locked(name)
            self._forwards.pop(name, None)
        if persist and self._preferences is not None and self._user:
            try:
                users = self._preferences.load()
                user_pref = users.get(self._user, UserPreferences(self._user))
                existing = [f for f in user_pref.forwards if f.name != name]
                self._preferences.replace_user(
                    UserPreferences(
                        self._user, tuple(sorted(existing, key=lambda f: f.name))
                    )
                )
            except Exception:
                pass

    def shortcuts_status(self, namespace: str | None = None) -> list[dict[str, Any]]:
        """Return current status of all configured quick shortcuts."""
        with self._lock:
            results = []
            for sc_id, sc in self._shortcuts.items():
                managed = self._forwards.get(sc_id)
                if managed is not None:
                    status = self._status_locked(sc_id, managed)
                    ns = managed.forward.namespace
                else:
                    status = ForwardStatus(
                        name=sc_id, state="stopped", probe=sc.probe.public_dict()
                    )
                    ns = self._shortcut_namespace(sc_id, namespace)
                results.append(
                    {
                        "id": sc_id,
                        "label": sc.label,
                        "description": sc.description,
                        "target": sc.target,
                        "local_port": sc.local_port,
                        "remote_port": sc.remote_port,
                        "namespace": ns,
                        "state": status.state,
                        "pid": status.pid,
                        "restarts": status.restarts,
                        "error": status.error,
                        "reachable": status.state == "running",
                        "health": status.health,
                        "last_error": status.last_error,
                        "last_probe_at": status.last_probe_at,
                        "consecutive_failures": status.consecutive_failures,
                        "probe": status.probe,
                        "required": sc.required,
                        "url": sc.url,
                        "owner": status.owner,
                    }
                )
            return results

    def stop(self, name: str) -> None:
        """Stop exactly one process started and owned by this supervisor."""
        with self._lock:
            if name not in self._forwards:
                raise ValueError("unknown saved port forward")
            self._stop_locked(name)

    def close(self) -> None:
        """Stop every owned forward and the monitoring thread."""
        self._stopped.set()
        with self._lock:
            for name in tuple(self._forwards):
                self._stop_locked(name)
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _ensure_thread_locked(self) -> None:
        if self._thread is None and not self._stopped.is_set():
            self._thread = threading.Thread(
                target=self._watch,
                name="piceli-port-forward-supervisor",
                daemon=True,
            )
            self._thread.start()

    def _tick_seconds(self) -> float:
        with self._lock:
            intervals = [managed.probe.interval for managed in self._forwards.values()]
        return max(0.05, min([0.5, *(interval / 5 for interval in intervals)]))

    def _watch(self) -> None:
        with ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="piceli-forward-probe"
        ) as pool:
            while not self._stopped.wait(self._tick_seconds()):
                self.tick(pool)

    def tick(self, pool: ThreadPoolExecutor | None = None) -> None:
        """Run one supervision step: restart exited forwards and probe due ones.

        Probes run outside the lock so a slow upstream never blocks status
        requests; a result is discarded if its process changed meanwhile.
        """
        with self._lock:
            self._ensure_locked()
            now = time.monotonic()
            due = [
                (managed, managed.process)
                for managed in self._forwards.values()
                if managed.process is not None
                and managed.process.poll() is None
                and now >= managed.next_probe
            ]
        if not due:
            return

        def run(item: tuple[_ManagedForward, Any]) -> str | None:
            return probe_endpoint(item[0].forward.local_port, item[0].probe)

        results = list(pool.map(run, due)) if pool else [run(item) for item in due]
        with self._lock:
            for (managed, process), outcome in zip(due, results, strict=True):
                current = self._forwards.get(managed.forward.name)
                if current is managed and managed.process is process:
                    self._record_probe_locked(managed, outcome)

    def _record_probe_locked(
        self, managed: _ManagedForward, outcome: str | None
    ) -> None:
        now = time.monotonic()
        probe = managed.probe
        managed.last_probe_at = time.time()
        if outcome is None:
            managed.health = "healthy"
            managed.consecutive_failures = 0
            managed.attempts = 0
            managed.error = None
            managed.next_probe = now + probe.interval
            return
        managed.last_error = outcome
        if (
            managed.health == "starting"
            and now - managed.started_at < probe.startup_grace
        ):
            # A just-spawned forward may not have bound its port yet.
            managed.next_probe = now + min(probe.interval, 0.5)
            return
        managed.consecutive_failures += 1
        if managed.consecutive_failures < probe.failure_threshold:
            managed.health = "unhealthy"
            managed.error = outcome
            managed.next_probe = now + probe.interval
            return
        process = managed.process
        managed.process = None
        if process is not None:
            self._stop_process(process)
        self._schedule_restart_locked(
            managed,
            f"health probe failed {managed.consecutive_failures}x: {outcome}",
        )

    def _schedule_restart_locked(self, managed: _ManagedForward, reason: str) -> None:
        """Back off before the next start, or give up once the budget is spent."""
        managed.last_error = reason
        managed.consecutive_failures = 0
        managed.attempts += 1
        if managed.attempts > managed.policy.max_restarts:
            managed.given_up = True
            managed.health = "failed"
            managed.error = (
                f"gave up after {managed.policy.max_restarts} restarts: {reason}"
            )
            managed.next_start = float("inf")
            return
        managed.restarts += 1
        managed.health = "restarting"
        managed.error = reason
        managed.next_start = time.monotonic() + managed.policy.delay(managed.attempts)

    def _ensure_locked(self) -> None:
        now = time.monotonic()
        for managed in self._forwards.values():
            process = managed.process
            if process is not None and process.poll() is None:
                continue
            if process is not None:
                managed.process = None
                self._schedule_restart_locked(
                    managed, f"port-forward exited with {process.returncode}"
                )
            if managed.process is None and now >= managed.next_start:
                self._start_locked(managed)

    def _start_locked(self, managed: _ManagedForward) -> None:
        if managed.process is not None and managed.process.poll() is None:
            return
        if not self._port_available(managed.forward.local_port):
            # An ambient loopback listener is not ours to adopt or terminate.
            # Leave it alone, spawn nothing, name its owner, and stop trying:
            # re-checking until the port frees up would silently take a
            # declared port back from whoever owns it now (another dashboard,
            # or ``piceli access`` restarting its forward). Only an explicit
            # start retries.
            owner = self._owner(managed.forward.local_port)
            reason = _OCCUPIED
            if owner is not None:
                reason = f"{_OCCUPIED}: {owner.describe()}"
            managed.owner = owner
            managed.health = "conflict"
            managed.error = reason
            managed.last_error = reason
            managed.given_up = True
            managed.next_start = float("inf")
            return
        command = managed.forward.command(
            kubectl=self._kubectl, kubeconfig=self._kubeconfig, context=self._context
        )
        try:
            managed.process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as error:
            managed.process = None
            self._schedule_restart_locked(managed, type(error).__name__)
            return
        now = time.monotonic()
        managed.error = None
        managed.health = "starting"
        managed.consecutive_failures = 0
        managed.started_at = now
        managed.next_probe = now + min(0.25, managed.probe.interval)

    def _stop_locked(self, name: str) -> None:
        managed = self._forwards.get(name)
        if managed is None:
            return
        process = managed.process
        managed.process = None
        managed.next_start = float("inf")
        managed.stopped = True
        managed.given_up = False
        managed.health = "stopped"
        if process is not None and process.poll() is None:
            self._stop_process(process)

    @staticmethod
    def _stop_process(process: subprocess.Popen[bytes]) -> None:
        """Terminate one fresh process group previously created by this supervisor."""
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                pass

    @staticmethod
    def _probe(forward: PortForward) -> bool:
        """Check the owned loopback endpoint without sending credentials or cluster traffic."""
        return (
            probe_endpoint(forward.local_port, legacy_health(forward.health_path))
            is None
        )

    @staticmethod
    def _port_available(port: int) -> bool:
        """Return whether a loopback port can be safely owned by this supervisor."""
        return not local_port_in_use(port)

    @staticmethod
    def _owner(port: int) -> PortOwner | None:
        """The local process holding ``port``, when it can be determined."""
        return port_owner(port)


@dataclass(frozen=True)
class AccessPlan:
    """Result of the port-conflict preflight for an access profile."""

    start: tuple[str, ...]
    external: tuple[str, ...]
    errors: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": list(self.start),
            "external": list(self.external),
            "errors": list(self.errors),
        }


def preflight_shortcuts(
    shortcuts: Iterable[UiShortcut],
    *,
    namespace: str | None = None,
    in_use: Callable[[int], bool] = local_port_in_use,
    owner: Callable[[int], PortOwner | None] = port_owner,
) -> AccessPlan:
    """Check that every declared forward can be owned before starting any.

    A required shortcut whose local port is already served by another process,
    or that has no namespace, is an error. An optional (``required = false``)
    shortcut on an occupied port is reported as ``external`` and not started.
    A conflict names the owning process (pid and command) when it can be found.
    """
    start: list[str] = []
    external: list[str] = []
    errors: list[str] = []
    ports: dict[int, str] = {}
    for shortcut in shortcuts:
        if shortcut.local_port in ports:
            errors.append(
                f"{shortcut.id}: local port {shortcut.local_port} is also declared "
                f"by {ports[shortcut.local_port]}"
            )
            continue
        ports[shortcut.local_port] = shortcut.id
        if not (shortcut.namespace or namespace):
            errors.append(f"{shortcut.id}: no namespace (set it or pass --namespace)")
            continue
        if in_use(shortcut.local_port):
            if shortcut.required:
                holder = owner(shortcut.local_port)
                by = f" ({holder.describe()})" if holder is not None else ""
                errors.append(
                    f"{shortcut.id}: local port {shortcut.local_port} is already in "
                    f"use by another process{by}; stop it or change local_port"
                )
            else:
                external.append(shortcut.id)
            continue
        start.append(shortcut.id)
    return AccessPlan(tuple(start), tuple(external), tuple(errors))


def run_port_forward(command: list[str]) -> int:
    """Run an explicit saved forward in the foreground and return kubectl's code.

    Foreground execution avoids an unsafe, unowned background process registry.
    The caller owns its terminal and stops the connection with the normal
    interrupt signal; the command always binds only the loopback address.
    """
    if not command or "port-forward" not in command or "--address" not in command:
        raise ValueError("invalid explicit port-forward command")
    address = command.index("--address")
    if command[address + 1 : address + 2] != ["127.0.0.1"]:
        raise ValueError("port forwards must be loopback-only")
    return subprocess.run(command, check=False).returncode


def kubectl_target(kubectl: str, kubeconfig: Path, context: str | None) -> list[str]:
    """``kubectl --kubeconfig FILE --context NAME``; the context is required.

    kubectl would otherwise fall back to the file's ``current-context``.
    """
    if not context:
        raise ValueError("an explicit kubeconfig context is required")
    return [kubectl, "--kubeconfig", str(kubeconfig), "--context", context]


def kubectl_logs_command(
    *,
    kubectl: str,
    kubeconfig: Path,
    context: str | None,
    namespace: str,
    target: str,
    tail: int = 200,
    container: str | None = None,
    previous: bool = False,
) -> list[str]:
    """Build a shell-free, bounded command for one workload's logs."""
    if not _NAME.fullmatch(namespace) or not _LOG_TARGET.fullmatch(target):
        raise ValueError("invalid log namespace or target")
    if not isinstance(tail, int) or isinstance(tail, bool) or not 1 <= tail <= 10_000:
        raise ValueError("log tail must be between 1 and 10000")
    result = kubectl_target(kubectl, kubeconfig, context)
    result.extend(["--namespace", namespace, "logs", target, f"--tail={tail}"])
    if container:
        if not _NAME.fullmatch(container):
            raise ValueError("invalid container name")
        result.extend(["--container", container])
    if previous:
        result.append("--previous")
    return result


def run_logs(command: list[str]) -> int:
    """Run one validated bounded log command in the caller's foreground terminal."""
    if (
        not command
        or "logs" not in command
        or not any(item.startswith("--tail=") for item in command)
    ):
        raise ValueError("invalid bounded log command")
    return subprocess.run(command, check=False).returncode
