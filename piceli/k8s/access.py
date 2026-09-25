"""Access and status from the model: resolve a target, check ports, report health.

``piceli access`` and ``piceli status`` take one TARGET:

- a ``release.toml`` path: the target cluster comes from ``[target]`` and the
  access declarations from the typed :class:`~piceli.app.App` that
  ``[release] composition`` returns (a composition function may return the
  App itself); or
- ``module:attr`` (or ``file.py:attr``) of an object with ``.app`` (a piceli
  App) and ``.target`` (with ``.kubeconfig``, ``.context`` and
  ``.namespace``), such as a pipeline. A :class:`~piceli.pipeline.Pipeline`
  brings the release state ``piceli deploy`` keeps. Optional hooks on other
  objects: ``.release_spec`` (a ``ReleaseSpec`` or a path to
  ``release.toml``) for the release state, and ``.last_checks()`` returning
  the latest checks result.

Everything here is explicit: the kubeconfig is a named file and the context
is named; nothing reads ``KUBECONFIG``, ``~/.kube/config`` or the current
context. Resolving a target never contacts a cluster; :func:`collect_status`
reads the cluster only through the reader it is given. Importing this module
is side-effect free.
"""

from __future__ import annotations

import os
import re
import signal
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol

from piceli.k8s.observe import local_port_in_use, probe_endpoint
from piceli.k8s.port_owner import (
    OTHER,
    UNKNOWN,
    Holder,
    PortOwner,
    is_piceli_forward,
    port_owner,
    recognise,
)
from piceli.k8s.ui_config import UiComponent, UiConfig, UiShortcut, UiTier

STATUS_SCHEMA = "piceli.status.v1"
WORKLOAD_KINDS = ("Deployment", "StatefulSet", "DaemonSet")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
# Waiting reasons that are part of a normal start, not a problem.
_NORMAL_WAITING = frozenset({"ContainerCreating", "PodInitializing"})
# Command lines read to verify a forward is Piceli's (not shown past MAX_COMMAND).
_OWNER_LIMIT = 4096


class AccessTargetError(ValueError):
    """The TARGET cannot be resolved; ``code`` is a registered error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AccessTarget:
    """A resolved TARGET: where the app runs and how to reach it.

    :param name: The app name (or the release name when there is no App).
    :param source: ``"release-spec"`` or ``"pipeline"``.
    :param shortcuts: The declared forwards, namespace pinned.
    :param workloads: ``(kind, name)`` of every workload the app declares.
    :param spec: The release spec, when known (for the release state).
    :param checks: Returns the latest checks result, when the target has one.
    :param exec_policy: The target's explicit exec credential plugin opt-in
        (``[target] allow_exec`` or ``Target(allow_exec=True)``), if any.
    """

    name: str
    namespace: str
    kubeconfig: Path
    context: str
    source: str
    transport: str = "https"
    shortcuts: tuple[UiShortcut, ...] = ()
    workloads: tuple[tuple[str, str], ...] = ()
    spec: Any = None
    checks: Callable[[], Any] | None = None
    app: Any = None
    exec_policy: Any = None

    def shortcut(self, ident: str) -> UiShortcut | None:
        return next((item for item in self.shortcuts if item.id == ident), None)


class _AnyNode(dict[str, Any]):
    """Node aliases resolve to themselves: lists workloads without verified nodes."""

    def __contains__(self, key: object) -> bool:
        return True

    def __getitem__(self, key: str) -> Any:
        return SimpleNamespace(name=key)


def _workloads_of(composition: Any) -> tuple[tuple[str, str], ...]:
    return tuple(
        (resource.ref.kind, resource.ref.name)
        for component in composition.components
        for resource in component.resources
        if resource.ref.kind in WORKLOAD_KINDS
    )


def _app_workloads(app: Any, namespace: str) -> tuple[tuple[str, str], ...]:
    from piceli.app.model import Deployment

    try:
        return _workloads_of(app.render(namespace, nodes=_AnyNode()))
    except ValueError:
        return tuple(
            ("Deployment", item.name)
            for item in app.objects
            if isinstance(item, Deployment)
        )


def _find_app(value: Any, context: Any) -> tuple[Any, Any]:
    """``(app, composition)`` from an App, a composition, or a function of ctx."""
    from piceli.app.app import App
    from piceli.k8s.ops.plan import DeploymentComposition

    if isinstance(value, App):
        return value, None
    if isinstance(value, DeploymentComposition):
        return None, value
    if callable(value):
        result = value(context)
        if isinstance(result, App):
            return result, None
        if isinstance(result, DeploymentComposition):
            return None, result
    raise AccessTargetError(
        "access-target-invalid",
        "the release composition must be an App, a DeploymentComposition or a "
        "function of the release context returning one",
    )


def _from_spec(path: Path) -> AccessTarget:
    from piceli.app.render import spec_context
    from piceli.k8s.release_spec import ReleaseSpec, ReleaseSpecError

    try:
        spec = ReleaseSpec.from_toml(path)
        entry = spec.load_composition()
        from piceli.app.app import App

        if isinstance(entry, App):
            app, composition = entry, None
        else:
            _, context = spec_context(path)
            app, composition = _find_app(entry, context)
    except AccessTargetError:
        raise
    except (ReleaseSpecError, OSError) as error:
        raise AccessTargetError("access-target-invalid", str(error)) from None
    except (ValueError, TypeError) as error:
        raise AccessTargetError("access-target-invalid", str(error)) from None
    model = spec.model
    namespace = model.target.namespace
    try:
        policy = spec.kubeconfig_target().exec_policy
    except ValueError as error:
        raise AccessTargetError("access-target-invalid", str(error)) from None
    if app is not None:
        shortcuts = app.shortcuts(namespace)
        workloads = _app_workloads(app, namespace)
        name = app.name
    else:
        shortcuts, workloads, name = (), _workloads_of(composition), model.release.name
    return AccessTarget(
        name=name,
        namespace=namespace,
        kubeconfig=spec.resolve(model.target.kubeconfig),
        context=model.target.context,
        transport=model.target.transport,
        source="release-spec",
        shortcuts=tuple(shortcuts),
        workloads=workloads,
        spec=spec,
        app=app,
        exec_policy=policy,
    )


def _pipeline_release_spec(value: Any) -> Any:
    """The release state ``piceli deploy`` keeps for a Pipeline, if it has one.

    Built from the pipeline's receipts like ``piceli release --spec
    MODULE:ATTR``; ``None`` for any other object, or before anything was
    delivered (then there is no release either).
    """
    from piceli.pipeline import Pipeline, PipelineError

    if not isinstance(value, Pipeline):
        return None
    from piceli.pipeline.operate import operations_spec

    if value.state.backend == "cluster":
        # Shared state: refresh the working copy first (reads only, no lock).
        from piceli.state import StateError, session
        from piceli.state.scopes import pipeline_scope

        try:
            with session(pipeline_scope(value), write=False):
                pass
        except (StateError, ValueError, OSError):
            pass  # best effort: the working copy as it is
    try:
        return operations_spec(value, current=False)
    except (PipelineError, ValueError, OSError):
        return None


def _from_pipeline(entry: str, base: Path) -> AccessTarget:
    from piceli.app.app import App
    from piceli.app.render import RenderError, load_target

    try:
        value = load_target(entry, base)
    except RenderError as error:
        raise AccessTargetError("access-target-invalid", str(error)) from None
    except Exception as error:  # the user's module raised while importing
        raise AccessTargetError(
            "access-target-invalid",
            f"importing {entry!r} failed: {type(error).__name__}",
        ) from None
    if getattr(value, "needs_environment", False):
        raise AccessTargetError(
            "environment-required",
            f"{entry!r} deploys one target per environment; status and access "
            "take a pipeline with one target (select the environment in a "
            "module attribute: pipeline.for_environment(name))",
        )
    app = getattr(value, "app", None)
    target = getattr(value, "target", None)
    if not isinstance(app, App) or target is None:
        raise AccessTargetError(
            "access-target-invalid",
            f"{entry!r} must be an object with .app (a piceli App) and .target "
            "(.kubeconfig, .context, .namespace), or pass a release.toml path",
        )
    kubeconfig = getattr(target, "kubeconfig", None)
    context = getattr(target, "context", None)
    namespace = getattr(target, "namespace", None)
    if not kubeconfig or not context or not namespace:
        raise AccessTargetError(
            "access-target-invalid",
            f"{entry!r}: .target needs an explicit kubeconfig, context and namespace",
        )
    spec = getattr(value, "release_spec", None)
    if spec is None:
        spec = _pipeline_release_spec(value)
    if isinstance(spec, str | Path):
        from piceli.k8s.release_spec import ReleaseSpec, ReleaseSpecError

        try:
            spec = ReleaseSpec.from_toml(Path(spec))
        except ReleaseSpecError as error:
            raise AccessTargetError("access-target-invalid", str(error)) from None
    checks = getattr(value, "last_checks", None)
    policy = getattr(target, "exec_policy", None)
    return AccessTarget(
        name=app.name,
        namespace=str(namespace),
        kubeconfig=Path(kubeconfig).expanduser(),
        context=str(context),
        transport=str(getattr(target, "transport", "https") or "https"),
        source="pipeline",
        shortcuts=app.shortcuts(str(namespace)),
        workloads=_app_workloads(app, str(namespace)),
        spec=spec,
        checks=checks if callable(checks) else None,
        app=app,
        exec_policy=policy() if callable(policy) else None,
    )


def resolve_target(entry: str, base: Path | None = None) -> AccessTarget:
    """Resolve TARGET (``release.toml`` path or ``module:attr``); pure, no cluster.

    :raises AccessTargetError: ``access-target-invalid`` with a message.
    """
    base = base or Path.cwd()
    path = Path(entry).expanduser()
    if not path.is_absolute():
        path = base / path
    if entry.endswith(".toml") or (":" not in entry and path.is_file()):
        if not path.is_file():
            raise AccessTargetError(
                "access-target-invalid", f"release spec not found: {entry}"
            )
        return _from_spec(path)
    return _from_pipeline(entry, base)


def select_shortcuts(
    shortcuts: Sequence[UiShortcut], only: Iterable[str] | None
) -> tuple[tuple[UiShortcut, ...], list[str]]:
    """Keep the ids in ``only`` (all when empty); also return the unknown ids."""
    wanted = list(only or ())
    if not wanted:
        return tuple(shortcuts), []
    known = {item.id for item in shortcuts}
    unknown = sorted(set(wanted) - known)
    return tuple(item for item in shortcuts if item.id in set(wanted)), unknown


# ---------------------------------------------------------------- conflicts


@dataclass(frozen=True)
class PortConflict:
    """A declared local port already served by another process.

    ``holder`` says whether that process is Piceli's own for this target
    (see :func:`~piceli.k8s.port_owner.recognise`); only then is its command
    line kept, anything else is named by its pid alone.
    """

    id: str
    local_port: int
    required: bool
    owner: PortOwner | None = None
    holder: Holder | None = None

    def _holder(self) -> Holder:
        if self.holder is not None:
            return self.holder
        return Holder(UNKNOWN if self.owner is None else OTHER, self.owner)

    def to_dict(self) -> dict[str, Any]:
        public = self._holder().public()
        return {
            "id": self.id,
            "local_port": self.local_port,
            "required": self.required,
            "owner": public["owner"],
            "holder": public["holder"],
        }

    def describe(self) -> str:
        return (
            f"{self.id}: local port {self.local_port} is held by "
            f"{self._holder().describe()}"
        )


def _full_owner(port: int) -> PortOwner | None:
    """The port's owner with full command lines, for the ownership check only."""
    return port_owner(port, limit=_OWNER_LIMIT)


def port_conflicts(
    shortcuts: Iterable[UiShortcut],
    *,
    in_use: Callable[[int], bool] = local_port_in_use,
    owner: Callable[[int], PortOwner | None] = _full_owner,
    recognise_owner: Callable[[PortOwner | None], Holder] | None = None,
) -> list[PortConflict]:
    """Every declared forward whose local port is already taken, with its owner.

    ``recognise_owner`` (see :func:`holder_check`) marks Piceli's own
    processes for the target; without it every owner counts as foreign.
    """
    conflicts = []
    for item in shortcuts:
        if not in_use(item.local_port):
            continue
        found = owner(item.local_port)
        holder = recognise_owner(found) if recognise_owner is not None else None
        conflicts.append(
            PortConflict(item.id, item.local_port, item.required, found, holder)
        )
    return conflicts


def holder_check(
    target: AccessTarget, names: Iterable[str] = ()
) -> Callable[[PortOwner | None], Holder]:
    """Recognise Piceli's own processes for ``target`` (see ``recognise``).

    ``names`` are the TARGET arguments a ``piceli access`` for this app may
    have been started with (as typed, and absolute).
    """
    forwards = tuple(
        (
            item.namespace or target.namespace,
            item.target,
            item.local_port,
            item.remote_port,
        )
        for item in target.shortcuts
    )
    known = tuple(names)

    def check(found: PortOwner | None) -> Holder:
        return recognise(
            found,
            kubeconfig=str(target.kubeconfig),
            context=target.context,
            forwards=forwards,
            targets=known,
        )

    return check


def stale_hint(conflicts: Iterable[PortConflict], target: str) -> str | None:
    """The ``piceli access stop --stale`` line when Piceli holds one of the ports."""
    ports = [
        item.local_port
        for item in conflicts
        if item.holder is not None and item.holder.piceli
    ]
    if not ports:
        return None
    return f"stop piceli's stale processes with: piceli access stop --stale {target}"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _terminate(pid: object) -> bool:
    """SIGTERM ``pid``; refuses non-integers (test doubles), init and ourselves."""
    if type(pid) is not int or pid <= 1 or pid in {os.getpid(), os.getppid()}:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return True


def stop_stale(
    ports: Iterable[int],
    check: Callable[[PortOwner | None], Holder],
    *,
    in_use: Callable[[int], bool] = local_port_in_use,
    owner: Callable[[int], PortOwner | None] = _full_owner,
    terminate: Callable[[object], bool] = _terminate,
    wait_seconds: float = 5.0,
) -> dict[str, Any]:
    """Stop the Piceli processes for this target that hold ``ports``; never others.

    Each held port's owner is recognised (see :func:`holder_check`). A
    Piceli forward's supervisor (or the orphaned ``kubectl`` itself) and a
    Piceli server for this target get SIGTERM, after a second look confirms
    the same process still holds the port. Any other holder is left alone
    and reported by pid only. Returns ``stopped``, ``left`` (foreign or
    unidentified holders), ``failed`` (Piceli processes still holding their
    port after ``wait_seconds``) and ``free`` ports.
    """
    stopped: list[dict[str, Any]] = []
    left: list[dict[str, Any]] = []
    free: list[int] = []
    signalled: dict[int, list[int]] = {}
    for port in dict.fromkeys(ports):
        if not in_use(port):
            free.append(port)
            continue
        holder = check(owner(port))
        if not holder.piceli or holder.stop is None or holder.owner is None:
            left.append({"port": port, **holder.public()})
            continue
        again = check(owner(port))  # the same process, right before the signal
        if again.owner is None or again.owner.pid != holder.owner.pid:
            left.append({"port": port, **again.public()})
            continue
        if holder.stop not in signalled and not terminate(holder.stop):
            left.append({"port": port, **holder.public()})
            continue
        signalled.setdefault(holder.stop, []).append(port)
        stopped.append({"port": port, "pid": holder.stop, **holder.public()})
    deadline = time.monotonic() + wait_seconds
    waiting = {item["port"] for item in stopped}
    while waiting and time.monotonic() < deadline:
        waiting = {port for port in waiting if in_use(port)}
        if waiting:
            time.sleep(0.1)
    failed = [item for item in stopped if item["port"] in waiting]
    return {
        "stopped": [item for item in stopped if item["port"] not in waiting],
        "failed": failed,
        "left": left,
        "free": free,
    }


# ---------------------------------------------------------------- dashboard


def access_ui_config(base: UiConfig, target: AccessTarget) -> UiConfig:
    """Dashboard config whose shortcuts are the model's access declarations.

    Entries of ``base`` (an ``--ui-config`` file) win over a model forward
    with the same id. Without tiers in ``base``, one tier lists the app's
    workloads and links each to the forward that reaches it.
    """
    explicit = {item.id for item in base.shortcuts}
    shortcuts = (
        *(item for item in target.shortcuts if item.id not in explicit),
        *base.shortcuts,
    )
    tiers = base.tiers
    if not tiers and target.workloads:
        links: dict[str, str] = {}
        for item in shortcuts:
            links.setdefault(item.target.split("/", 1)[1], item.id)
        tiers = (
            UiTier(
                name=target.name,
                components=tuple(
                    UiComponent(name=name, role=kind, shortcut=links.get(name))
                    for kind, name in target.workloads
                ),
            ),
        )
    return UiConfig(
        topology_subtitle=base.topology_subtitle,
        badges=base.badges,
        shortcuts=tuple(shortcuts),
        tiers=tiers,
        inventory=base.inventory,
    )


# ------------------------------------------------------------------- status


class WorkloadReader(Protocol):
    """Read-only access to workloads and their pods (camelCase API dicts)."""

    def workload(self, kind: str, namespace: str, name: str) -> dict[str, Any] | None:
        """The live object, or ``None`` when it does not exist."""

    def pods(self, namespace: str, labels: Mapping[str, str]) -> list[dict[str, Any]]:
        """Pods matching every label."""


class KubernetesWorkloadReader:
    """:class:`WorkloadReader` over an explicit kubeconfig file and context."""

    def __init__(
        self,
        *,
        kubeconfig: Path,
        context: str,
        transport: str = "https",
        timeout: float = 10.0,
        exec_policy: Any = None,
    ) -> None:
        from piceli.k8s.ops.provider_factory import api_client_from_kubeconfig

        self._client = api_client_from_kubeconfig(
            kubeconfig,
            context,
            transport=transport,  # type: ignore[arg-type]
            exec_policy=exec_policy,
        )
        self._timeout = timeout

    def workload(self, kind: str, namespace: str, name: str) -> dict[str, Any] | None:
        from kubernetes.client import AppsV1Api
        from kubernetes.client.exceptions import ApiException

        apps = AppsV1Api(self._client)
        read = {
            "Deployment": apps.read_namespaced_deployment,
            "StatefulSet": apps.read_namespaced_stateful_set,
            "DaemonSet": apps.read_namespaced_daemon_set,
        }[kind]
        try:
            value = read(name, namespace, _request_timeout=self._timeout)
        except ApiException as error:
            if error.status == 404:
                return None
            raise
        result: dict[str, Any] = self._client.sanitize_for_serialization(value)
        return result

    def pods(self, namespace: str, labels: Mapping[str, str]) -> list[dict[str, Any]]:
        from kubernetes.client import CoreV1Api

        selector = ",".join(f"{key}={value}" for key, value in sorted(labels.items()))
        value = CoreV1Api(self._client).list_namespaced_pod(
            namespace, label_selector=selector, _request_timeout=self._timeout
        )
        items: list[dict[str, Any]] = self._client.sanitize_for_serialization(value)[
            "items"
        ]
        return items

    def close(self) -> None:
        self._client.close()


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _digest(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = _DIGEST.search(value.rsplit("@", 1)[-1])
    return match.group(0) if match else None


def _replicas(kind: str, live: Mapping[str, Any]) -> dict[str, int]:
    spec = live.get("spec") or {}
    status = live.get("status") or {}
    if kind == "DaemonSet":
        return {
            "desired": _int(status.get("desiredNumberScheduled")),
            "ready": _int(status.get("numberReady")),
            "updated": _int(status.get("updatedNumberScheduled")),
            "available": _int(status.get("numberAvailable")),
            "current": _int(status.get("currentNumberScheduled")),
        }
    desired = spec.get("replicas")
    return {
        "desired": 1 if desired is None else _int(desired),
        "ready": _int(status.get("readyReplicas")),
        "updated": _int(status.get("updatedReplicas")),
        "available": _int(status.get("availableReplicas")),
        "current": _int(status.get("replicas")),
    }


def _pod_problems(pods: Sequence[Mapping[str, Any]]) -> list[str]:
    problems: list[str] = []
    for pod in pods:
        name = (pod.get("metadata") or {}).get("name", "?")
        status = pod.get("status") or {}
        if status.get("phase") in {"Failed", "Unknown"}:
            problems.append(f"{name}: phase {status['phase']}")
        for container in (
            *(status.get("initContainerStatuses") or ()),
            *(status.get("containerStatuses") or ()),
        ):
            state = container.get("state") or {}
            waiting = state.get("waiting") or {}
            reason = waiting.get("reason")
            if reason and reason not in _NORMAL_WAITING:
                problems.append(f"{name}/{container.get('name')}: {reason}")
            restarts = _int(container.get("restartCount"))
            if restarts:
                last = (container.get("lastState") or {}).get("terminated") or {}
                why = f" (last: {last['reason']})" if last.get("reason") else ""
                problems.append(
                    f"{name}/{container.get('name')}: {restarts} restarts{why}"
                )
    return problems


def _images(
    live: Mapping[str, Any], pods: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    template = ((live.get("spec") or {}).get("template") or {}).get("spec") or {}
    running: dict[str, set[str]] = {}
    for pod in pods:
        for container in (pod.get("status") or {}).get("containerStatuses") or ():
            digest = _digest(container.get("imageID"))
            if digest:
                running.setdefault(str(container.get("name")), set()).add(digest)
    result = []
    for container in template.get("containers") or ():
        name = str(container.get("name"))
        image = container.get("image")
        pinned = _digest(image)
        seen = sorted(running.get(name, ()))
        result.append(
            {
                "container": name,
                "image": image,
                "digest": pinned or (seen[0] if len(seen) == 1 else None),
                "pinned": pinned is not None,
                "running": seen,
            }
        )
    return result


def workload_status(
    kind: str,
    name: str,
    live: Mapping[str, Any] | None,
    pods: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Readiness, health, images and problems of one workload.

    ``health`` is ``ready`` (the controller has seen the latest spec, every
    desired replica is updated and ready, and no old replica is left: the
    same rule ``piceli release apply`` waits for), ``idle`` (scaled to zero),
    ``progressing``, ``unavailable`` (no ready replica) or ``missing`` (the
    object does not exist).
    """
    if live is None:
        return {
            "kind": kind,
            "name": name,
            "health": "missing",
            "ready": False,
            "replicas": None,
            "images": [],
            "problems": [],
            "error": None,
        }
    replicas = _replicas(kind, live)
    generation = _int((live.get("metadata") or {}).get("generation"))
    observed = _int((live.get("status") or {}).get("observedGeneration"))
    desired = replicas["desired"]
    current = observed >= generation
    if desired == 0 and current:
        health = "idle"
    elif (
        current
        and replicas["ready"] >= desired
        and replicas["updated"] >= desired
        and replicas["current"] == desired
    ):
        health = "ready"
    elif replicas["ready"] == 0:
        health = "unavailable"
    else:
        health = "progressing"
    return {
        "kind": kind,
        "name": name,
        "health": health,
        "ready": health in {"ready", "idle"},
        "replicas": replicas,
        "images": _images(live, pods),
        "problems": _pod_problems(pods),
        "error": None,
    }


def forward_status(
    shortcut: UiShortcut,
    *,
    context: str | None = None,
    namespace: str | None = None,
    probe: Callable[..., str | None] = probe_endpoint,
    in_use: Callable[[int], bool] = local_port_in_use,
    owner: Callable[[int], PortOwner | None] | None = None,
    owned: Callable[..., bool] | None = None,
) -> dict[str, Any]:
    """Whether one declared forward answers on ``127.0.0.1`` right now.

    ``forward`` is ``up`` (Piceli's ``kubectl port-forward`` for this
    declaration holds the port and the health probe passes), ``unhealthy``
    (Piceli's forward holds the port but the probe fails), ``occupied``
    (another process holds the port: error ``status-port-occupied``; only
    its pid is reported, never its command line) or ``down`` (nothing
    listens). A forward counts as Piceli's only when the listener is the
    ``kubectl`` argv Piceli builds for this context, namespace, target and
    ports, started by a Piceli process (see
    :func:`~piceli.k8s.port_owner.is_piceli_forward`); without ``context``
    nothing can be verified and a listener is ``occupied``. Contacts only
    the loopback address, never the cluster.
    """
    owner = owner or _full_owner
    owned = owned or is_piceli_forward
    outcome = probe(shortcut.local_port, shortcut.probe)
    listening = outcome is None or in_use(shortcut.local_port)
    holder = owner(shortcut.local_port) if listening else None
    mine = (
        listening
        and context is not None
        and owned(
            holder,
            context=context,
            namespace=shortcut.namespace or namespace or "",
            target=shortcut.target,
            local_port=shortcut.local_port,
            remote_port=shortcut.remote_port,
        )
    )
    public_owner: dict[str, Any] | None = None
    if not listening:
        state, error = "down", outcome
    elif not mine:
        state, error = "occupied", "status-port-occupied"
        if holder is not None:
            public_owner = PortOwner(port=holder.port, pid=holder.pid).to_dict()
    else:
        state = "up" if outcome is None else "unhealthy"
        error = outcome
        if holder is not None:
            public_owner = holder.shortened().to_dict()
    return {
        "id": shortcut.id,
        "label": shortcut.label,
        "target": shortcut.target,
        "url": shortcut.url,
        "local_port": shortcut.local_port,
        "remote_port": shortcut.remote_port,
        "required": shortcut.required,
        "forward": state,
        "error": error,
        "owner": public_owner,
        "probe": shortcut.probe.public_dict(),
    }


def release_status(spec: Any) -> dict[str, Any]:
    """The local release state of ``spec`` (catalog and history; no cluster)."""
    from piceli.k8s.release_runner import ReleaseRunner

    name = spec.model.release.name
    try:
        state = ReleaseRunner(spec).status()
    except Exception:  # a malformed or locked state directory is reported, not fatal
        return {"name": name, "state": "unknown", "error": "status-release-unreadable"}
    latest = state.get("latest") or {}
    current = state.get("deployed") or state.get("selected")
    images: Any = next(
        (
            item.get("images", {})
            for item in state.get("releases", ())
            if item.get("name") == current
        ),
        {},
    )
    return {
        "name": name,
        "state": latest.get("state") or "not-deployed",
        "current": current,
        "previous": state.get("previous"),
        "latest": {
            key: latest.get(key)
            for key in ("release", "intent", "state", "at", "execution_id")
        }
        if latest
        else None,
        "images": images,
        "pending_plans": len(state.get("pending_plans", ())),
    }


@dataclass
class _Summary:
    workloads: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _overall(workloads: Sequence[Mapping[str, Any]], errors: Sequence[str]) -> str:
    if errors or any(item["health"] == "unknown" for item in workloads):
        return "unknown"
    if not workloads:
        return "unknown"
    if all(item["ready"] for item in workloads):
        return "up"
    if all(item["health"] in {"missing", "unavailable"} for item in workloads):
        return "down"
    return "degraded"


def _access_state(forwards: Sequence[Mapping[str, Any]]) -> str:
    if not forwards:
        return "none"
    states = {item["forward"] for item in forwards}
    if states == {"up"}:
        return "up"
    return "partial" if "up" in states else "down"


def collect_status(
    target: AccessTarget,
    reader: WorkloadReader | None,
    *,
    reader_error: str | None = None,
    forward: Callable[[UiShortcut], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The ``piceli.status.v1`` document for ``target``.

    :param reader: Live workload reader; ``None`` when it could not be built,
        in which case ``reader_error`` is the error code recorded.
    """
    summary = _Summary()
    if reader_error:
        summary.errors.append(reader_error)
    for kind, name in target.workloads:
        if reader is None:
            summary.workloads.append(
                {
                    "kind": kind,
                    "name": name,
                    "health": "unknown",
                    "ready": False,
                    "replicas": None,
                    "images": [],
                    "problems": [],
                    "error": reader_error,
                }
            )
            continue
        try:
            live = reader.workload(kind, target.namespace, name)
        except Exception:  # transport detail may carry credentials: record a code
            if "status-cluster-unreadable" not in summary.errors:
                summary.errors.append("status-cluster-unreadable")
            summary.workloads.append(
                {
                    "kind": kind,
                    "name": name,
                    "health": "unknown",
                    "ready": False,
                    "replicas": None,
                    "images": [],
                    "problems": [],
                    "error": "status-cluster-unreadable",
                }
            )
            continue
        pods: list[dict[str, Any]] = []
        pods_error = None
        selector = ((live or {}).get("spec") or {}).get("selector") or {}
        labels = selector.get("matchLabels") or {}
        if live is not None and labels:
            try:
                pods = reader.pods(target.namespace, labels)
            except Exception:  # pods only add detail; the workload is still known
                pods_error = "status-cluster-unreadable"
        item = workload_status(kind, name, live, pods)
        if pods_error:
            item["error"] = pods_error
        summary.workloads.append(item)
    if forward is None:

        def forward(item: UiShortcut) -> dict[str, Any]:
            return forward_status(
                item, context=target.context, namespace=target.namespace
            )

    forwards = [forward(item) for item in target.shortcuts]
    checks = None
    if target.checks is not None:
        try:
            value = target.checks()
            checks = dict(value) if isinstance(value, Mapping) else value
        except Exception:
            checks = {"state": "unknown", "error": "status-checks-unreadable"}
    elif target.spec is not None:
        checks = _history_checks(target.spec)
    return {
        "schema": STATUS_SCHEMA,
        "state": _overall(summary.workloads, summary.errors),
        "app": target.name,
        "namespace": target.namespace,
        "context": target.context,
        "source": target.source,
        "release": release_status(target.spec) if target.spec is not None else None,
        "workloads": summary.workloads,
        "access": {"state": _access_state(forwards), "forwards": forwards},
        "checks": checks,
        "errors": summary.errors,
    }


def _history_checks(spec: Any) -> Any:
    """The ``checks`` recorded with the latest execution, when the runner keeps one."""
    import json

    path = spec.state_dir / "history.json"
    try:
        entries = json.loads(path.read_text()).get("entries") or []
    except (OSError, ValueError, AttributeError):
        return None
    for entry in reversed(entries):
        if isinstance(entry, dict) and "checks" in entry:
            return entry["checks"]
    return None
