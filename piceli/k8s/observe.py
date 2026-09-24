"""Read-only, local cluster operations primitives.

This module intentionally separates observation from deployment execution.  It
reconciles the redacted resource identities in a durable Piceli session archive
with live Kubernetes observations and reports objects that are not declared by
that archive.  It never applies, adopts, deletes, or resolves Secret values.
"""

from __future__ import annotations

import json
import http.client
import os
import re
import signal
import socket
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from piceli.k8s.ops.discovery import ResourceIdentity
from piceli.k8s.ops.session import DeploymentSessionArchive
from piceli.k8s.ui_config import UiShortcut


_NAME = re.compile(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?")
_FORWARD_TARGET = re.compile(r"(?:service|pod)/[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?")
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
    """Lazy Kubernetes-client adapter for explicit local kubeconfig observation."""

    def __init__(self, *, kubeconfig: Path, context: str | None = None) -> None:
        from kubernetes import config
        from kubernetes.dynamic import DynamicClient

        self._client = DynamicClient(
            config.new_client_from_config(config_file=str(kubeconfig), context=context)
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
            raise ValueError("forward target must be service/NAME or pod/NAME")
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
        result = [kubectl, "--kubeconfig", str(kubeconfig)]
        if context:
            result.extend(["--context", context])
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


@dataclass(frozen=True)
class ForwardStatus:
    """Public runtime state for one locally owned loopback port forward."""

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

    def __post_init__(self) -> None:
        if self.state not in {"stopped", "running", "degraded", "backoff", "failed"}:
            raise ValueError("invalid port-forward state")


@dataclass
class _ManagedForward:
    """One process and its bounded restart bookkeeping, owned by this supervisor."""

    forward: PortForward
    process: subprocess.Popen[bytes] | None = None
    restarts: int = 0
    next_start: float = 0.0
    error: str | None = None
    started_at: float = 0.0


class ForwardSupervisor:
    """Restore and bound explicitly saved loopback ``kubectl port-forward`` processes.

    The supervisor owns only processes it starts in a fresh process group. It
    never discovers, adopts, or kills an ambient ``kubectl`` process. A failed
    preference retries with an exponential delay capped at 30 seconds.
    """

    def __init__(
        self,
        *,
        preferences: PreferenceStore,
        user: str,
        kubeconfig: Path,
        context: str | None = None,
        kubectl: str = "kubectl",
        shortcuts: Iterable[UiShortcut] = (),
        namespace: str | None = None,
    ) -> None:
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
                    self._forwards[name] = _ManagedForward(forward)
            self._ensure_locked()
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._watch,
                    name="piceli-port-forward-supervisor",
                    daemon=True,
                )
                self._thread.start()

    def statuses(self) -> tuple[ForwardStatus, ...]:
        """Return local process state without exposing kubeconfig contents."""
        with self._lock:
            result = []
            for name, managed in sorted(self._forwards.items()):
                fwd = managed.forward
                process = managed.process
                if process is not None and process.poll() is None:
                    reachable = self._probe(managed.forward)
                    result.append(
                        ForwardStatus(
                            name=name,
                            state="running" if reachable else "degraded",
                            pid=process.pid,
                            restarts=managed.restarts,
                            error=None
                            if reachable
                            else "loopback endpoint is unavailable",
                            target=fwd.target,
                            local_port=fwd.local_port,
                            remote_port=fwd.remote_port,
                            namespace=fwd.namespace,
                            reachable=reachable,
                        )
                    )
                elif managed.next_start > time.monotonic():
                    result.append(
                        ForwardStatus(
                            name=name,
                            state="backoff",
                            restarts=managed.restarts,
                            error=managed.error,
                            target=fwd.target,
                            local_port=fwd.local_port,
                            remote_port=fwd.remote_port,
                            namespace=fwd.namespace,
                        )
                    )
                elif managed.error:
                    result.append(
                        ForwardStatus(
                            name=name,
                            state="failed",
                            restarts=managed.restarts,
                            error=managed.error,
                            target=fwd.target,
                            local_port=fwd.local_port,
                            remote_port=fwd.remote_port,
                            namespace=fwd.namespace,
                        )
                    )
                else:
                    result.append(
                        ForwardStatus(
                            name=name,
                            state="stopped",
                            restarts=managed.restarts,
                            target=fwd.target,
                            local_port=fwd.local_port,
                            remote_port=fwd.remote_port,
                            namespace=fwd.namespace,
                        )
                    )
            return tuple(result)

    def start(self, name: str) -> None:
        """Start one saved forward now; unknown names are rejected unless a configured shortcut."""
        with self._lock:
            if name not in self._forwards:
                if name not in self._shortcuts:
                    raise ValueError(f"unknown saved port forward: {name}")
                self._forwards[name] = _ManagedForward(self._shortcut_forward(name))
            managed = self._forwards[name]
            managed.next_start = 0.0
            managed.error = None
            self._start_locked(managed)

    def quick_start(self, shortcut_id: str, namespace: str | None = None) -> None:
        """Start a configured shortcut by id, automatically registering if absent."""
        with self._lock:
            if shortcut_id not in self._forwards:
                if shortcut_id not in self._shortcuts:
                    raise ValueError(f"unknown shortcut: {shortcut_id}")
                self._forwards[shortcut_id] = _ManagedForward(
                    self._shortcut_forward(shortcut_id, namespace)
                )
            managed = self._forwards[shortcut_id]
            managed.next_start = 0.0
            managed.error = None
            self._start_locked(managed)

    def _shortcut_namespace(self, shortcut_id: str, namespace: str | None) -> str:
        """A shortcut's pinned namespace wins, then the caller's, then the supervisor's."""
        return self._shortcuts[shortcut_id].namespace or namespace or self._namespace or ""

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
                    self._forwards[forward.name] = _ManagedForward(forward)
            else:
                self._forwards[forward.name] = _ManagedForward(forward)
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._watch,
                    name="piceli-port-forward-supervisor",
                    daemon=True,
                )
                self._thread.start()
        if persist:
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
        if persist:
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
                ns = (
                    managed.forward.namespace
                    if managed
                    else self._shortcut_namespace(sc_id, namespace)
                )
                state = "stopped"
                pid = None
                restarts = 0
                error = None
                if managed is not None:
                    restarts = managed.restarts
                    process = managed.process
                    if process is not None and process.poll() is None:
                        reachable = self._probe(managed.forward)
                        state = "running" if reachable else "degraded"
                        pid = process.pid
                        error = (
                            None if reachable else "loopback endpoint is unavailable"
                        )
                    elif managed.next_start > time.monotonic():
                        state = "backoff"
                        error = managed.error
                    elif managed.error:
                        state = "failed"
                        error = managed.error
                results.append(
                    {
                        "id": sc_id,
                        "label": sc.label,
                        "description": sc.description,
                        "target": sc.target,
                        "local_port": sc.local_port,
                        "remote_port": sc.remote_port,
                        "namespace": ns,
                        "state": state,
                        "pid": pid,
                        "restarts": restarts,
                        "error": error,
                        "reachable": state == "running",
                        "url": sc.url,
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
            self._thread.join(timeout=2)

    def _watch(self) -> None:
        while not self._stopped.wait(1):
            with self._lock:
                self._ensure_locked()

    def _ensure_locked(self) -> None:
        for managed in self._forwards.values():
            process = managed.process
            if process is not None and process.poll() is None:
                # A running kubectl process is not sufficient evidence that the
                # local browser can reach its upstream. Give a just-spawned
                # forward a brief bind grace period, then restart only this
                # supervisor-owned process when its loopback endpoint fails.
                if time.monotonic() - managed.started_at >= 3.0 and not self._probe(
                    managed.forward
                ):
                    self._stop_process(process)
                    managed.process = None
                    managed.restarts += 1
                    managed.error = "loopback endpoint is unavailable"
                    managed.next_start = time.monotonic() + min(
                        30.0, float(2 ** min(managed.restarts, 5))
                    )
                continue
            if process is not None:
                managed.process = None
                managed.restarts += 1
                managed.error = f"kubectl exited with {process.returncode}"
                delay = min(30.0, float(2 ** min(managed.restarts, 5)))
                managed.next_start = time.monotonic() + delay
            if managed.process is None and time.monotonic() >= managed.next_start:
                self._start_locked(managed)

    def _start_locked(self, managed: _ManagedForward) -> None:
        if managed.process is not None and managed.process.poll() is None:
            return
        if not self._port_available(managed.forward.local_port):
            # An ambient loopback listener is not ours to inspect, adopt, or
            # terminate. Leave it alone and make the conflict visible while
            # periodically retrying in case its owner intentionally stops it.
            managed.error = "loopback port is occupied by an external process"
            managed.next_start = time.monotonic() + 1.0
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
            managed.error = None
            managed.started_at = time.monotonic()
        except OSError as error:
            managed.process = None
            managed.restarts += 1
            managed.error = type(error).__name__
            managed.next_start = time.monotonic() + min(
                30.0, float(2 ** min(managed.restarts, 5))
            )

    def _stop_locked(self, name: str) -> None:
        managed = self._forwards.get(name)
        if managed is None:
            return
        process = managed.process
        managed.process = None
        managed.next_start = float("inf")
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
            except OSError:
                pass

    @staticmethod
    def _probe(forward: PortForward) -> bool:
        """Check the owned loopback endpoint without sending credentials or cluster traffic."""
        try:
            if forward.health_path is None:
                with socket.create_connection(
                    ("127.0.0.1", forward.local_port), timeout=0.5
                ):
                    return True
            connection = http.client.HTTPConnection(
                "127.0.0.1", forward.local_port, timeout=0.75
            )
            try:
                connection.request("GET", forward.health_path)
                # Authentication challenges and redirects still prove that the
                # forward reaches a live owned upstream. Server errors do not.
                return connection.getresponse().status < 500
            finally:
                connection.close()
        except (OSError, http.client.HTTPException):
            return False

    @staticmethod
    def _port_available(port: int) -> bool:
        """Return whether a loopback port can be safely owned by this supervisor."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


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
    result = [kubectl, "--kubeconfig", str(kubeconfig)]
    if context:
        result.extend(["--context", context])
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
