"""Forwards of a composition environment: ``piceli access ENV --cluster infra.py:CLUSTER``.

A composition (``infra.py``) names each environment's namespace and the
:class:`~piceli.infra.Cluster` whose ``credentials`` profile reaches it. The
environment's forwards are:

- its **declared** forwards: an environment that deploys a
  :class:`piceli.Pipeline` (``Environment(pipeline=...)``) forwards what its
  app declares (``app.access.forward(...)`` on a Service or Deployment),
  with the declared local port, id, path, health probe and restart policy;
  a declared Service forward is kept only when the Service is live;
- every other live Service port (contract components' ports live in their
  contracts, in the source repositories): one forward each, on a free
  loopback port (several environments can be reached at once, as branch
  environments are).

The cluster is read with the profile's explicit kubeconfig and context,
never the current context.

:func:`forward_shortcut` is the one place a forward is described, for this
command and for the forwards the web UI starts.

Importing this module is side-effect free.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from piceli.k8s.access import AccessTarget, AccessTargetError
from piceli.k8s.ops.provider_factory import KubeconfigTarget
from piceli.k8s.ui_config import UiShortcut

__all__ = [
    "composition_env_target",
    "declared_forwards",
    "forward_shortcut",
    "service_forwards",
]

_ID = re.compile(r"[^a-z0-9-]+")


def forward_shortcut(
    id: str,
    *,
    kind: str,
    name: str,
    namespace: str,
    local_port: int,
    remote_port: int,
    label: str | None = None,
) -> UiShortcut:
    """One supervised loopback forward to ``kind/name:remote_port``."""
    return UiShortcut(
        id=id,
        label=label or f"{name}:{remote_port}",
        target=f"{kind.lower()}/{name}",
        namespace=namespace,
        local_port=local_port,
        remote_port=remote_port,
    )


def service_forwards(
    namespace: str, services: Iterable[Mapping[str, Any]], *, port: Callable[[], int]
) -> tuple[UiShortcut, ...]:
    """One forward per Service port, ordered by Service name and port."""
    found: list[tuple[str, int]] = []
    for raw in services:
        name = (raw.get("metadata") or {}).get("name")
        if not isinstance(name, str) or not name:
            continue
        for item in (raw.get("spec") or {}).get("ports") or []:
            value = item.get("port") if isinstance(item, Mapping) else None
            if (
                isinstance(value, int)
                and not isinstance(value, bool)
                and 0 < value < 65536
            ):
                found.append((name, value))
    return tuple(
        forward_shortcut(
            _ID.sub("-", f"{name}-{remote}".lower()).strip("-")[:63],
            kind="Service",
            name=name,
            namespace=namespace,
            local_port=port(),
            remote_port=remote,
        )
        for name, remote in sorted(set(found))
    )


def declared_forwards(
    environment: Any, services: Iterable[Mapping[str, Any]]
) -> tuple[UiShortcut, ...]:
    """The forwards ``environment``'s app declares, pinned to its namespace.

    Only an environment that deploys a Pipeline declares forwards; a Service
    forward whose Service is not live is left out.
    """
    pipeline = getattr(environment, "pipeline", None)
    app = getattr(pipeline, "app", None)
    if app is None or not callable(getattr(app, "shortcuts", None)):
        return ()
    live = {
        (raw.get("metadata") or {}).get("name")
        for raw in services
        if isinstance(raw, Mapping)
    }
    return tuple(
        item
        for item in app.shortcuts(environment.namespace)
        if not item.target.startswith("service/")
        or item.target.split("/", 1)[1] in live
    )


def _live_services(target: KubeconfigTarget) -> list[dict[str, Any]]:
    from piceli.services.query import KubernetesReader
    from piceli.services.registration import Registration

    reader = KubernetesReader(Registration("access-env", "access", target))
    try:
        return reader.list("v1", "Service", target.namespace)
    finally:
        reader.close()


def _host(companion: Mapping[str, Any]) -> str | None:
    """The MagicDNS name the operator wrote (never composed by Piceli: a
    stale device may have pushed the new proxy to ``NAME-1``)."""
    ingress = (
        ((companion.get("status") or {}).get("loadBalancer") or {}).get("ingress")
    ) or []
    return next(
        (
            entry.get("hostname") or entry.get("ip")
            for entry in ingress
            if isinstance(entry, Mapping) and (entry.get("hostname") or entry.get("ip"))
        ),
        None,
    )


def _companion_ports(companion: Mapping[str, Any]) -> set[int]:
    spec = companion.get("spec") or {}
    if companion.get("kind") == "Ingress":
        port = (
            ((spec.get("defaultBackend") or {}).get("service") or {}).get("port")
        ) or {}
        return {int(port.get("number") or 0)}
    return {int(port.get("port") or 0) for port in spec.get("ports") or ()}


def tailnet_urls(
    shortcuts: Iterable[UiShortcut], companions: Iterable[Mapping[str, Any]]
) -> tuple[dict[str, Any], ...]:
    """The tailnet URL of each forward whose Service has a tailnet companion.

    A companion (label ``piceli.io/access=tailnet``) is a ``LoadBalancer``
    Service (``http://HOST:PORT/path``) or, with ``Access(https=True)``, a
    Tailscale Ingress (``https://HOST/path``). Its name comes from the
    operator in ``status.loadBalancer.ingress``; until it has one the entry
    is ``ready: False`` and the forward stays the way in.
    """
    from piceli.envs.access import ACCESS_LABEL, SERVICE_LABEL

    by_service: dict[str, list[Mapping[str, Any]]] = {}
    for raw in companions:
        labels = (raw.get("metadata") or {}).get("labels") or {}
        if labels.get(ACCESS_LABEL) == "tailnet" and labels.get(SERVICE_LABEL):
            by_service.setdefault(labels[SERVICE_LABEL], []).append(raw)
    found = []
    for item in shortcuts:
        if not item.target.startswith("service/"):
            continue
        service = item.target.split("/", 1)[1]
        companion = next(
            (
                raw
                for raw in by_service.get(service, ())
                if item.remote_port in _companion_ports(raw)
            ),
            None,
        )
        if companion is None:
            continue
        host = _host(companion)
        path = getattr(item, "path", None) or ""
        secure = companion.get("kind") == "Ingress"
        url = None
        if host:
            url = (
                f"https://{host}{path or '/'}"
                if secure
                else (f"http://{host}:{item.remote_port}{path}")
            )
        found.append(
            {
                "id": item.id,
                "service": service,
                "port": item.remote_port,
                "host": host,
                "ready": host is not None,
                "url": url,
                "companion": (companion.get("metadata") or {}).get("name"),
            }
        )
    return tuple(found)


#: Note codes of ``piceli access`` (``tailnet_notes``; 0.19.0).
TAILNET_MISSING = "access-tailnet-missing"
TAILNET_PENDING = "access-tailnet-pending"
OPERATOR_MISSING = "access-operator-missing"
OPERATOR_NOT_READY = "access-operator-not-ready"
OPERATOR_UNKNOWN = "access-operator-unknown"
_EVENT_CHARS = 300


def tailnet_notes(
    access: Any,
    companions: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]] | None,
    operator: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Why an environment declaring ``Access(tailnet=True)`` is not (yet)
    reachable on the tailnet: no companion in the running release, no ready
    operator, and each companion without a name with the operator's latest
    Event on it (a tag it may not use, an OAuth error). Pure: the reads are
    the caller's (``None``: could not be read)."""
    notes: list[dict[str, Any]] = []
    if not companions:
        notes.append(
            {
                "code": TAILNET_MISSING,
                "detail": "the running release has no tailnet companions: it was "
                "rendered before Access(tailnet=True); deploy the environment again "
                "(its plan's access.notes say why a Service is left out)",
            }
        )
    pending = [c for c in companions if _host(c) is None]
    if pending or not companions:
        namespace = access.operator_namespace
        if operator is None:
            notes.append(
                {
                    "code": OPERATOR_UNKNOWN,
                    "detail": f"the Tailscale operator in {namespace} could not be "
                    "checked (no read access)",
                }
            )
        else:
            deployments = list(operator.get("deployments") or ())
            if not operator.get("ingress_class") and not deployments:
                notes.append(
                    {
                        "code": OPERATOR_MISSING,
                        "detail": f"no Tailscale operator: no Deployment in {namespace} "
                        "and no 'tailscale' IngressClass; the cluster owner installs it",
                    }
                )
            else:
                ready = any(
                    int((d.get("status") or {}).get("readyReplicas") or 0) > 0
                    for d in deployments
                )
                crashing = sorted(
                    str((pod.get("metadata") or {}).get("name"))
                    for pod in operator.get("pods") or ()
                    for status in (pod.get("status") or {}).get("containerStatuses")
                    or ()
                    if ((status.get("state") or {}).get("waiting") or {}).get("reason")
                    in {"CrashLoopBackOff", "Error", "CreateContainerConfigError"}
                )
                if crashing or not ready:
                    notes.append(
                        {
                            "code": OPERATOR_NOT_READY,
                            "detail": (
                                f"the operator in {namespace} is crash-looping "
                                f"({', '.join(crashing)}): read its log (an OAuth "
                                "secret with a trailing newline fails with 401)"
                            )
                            if crashing
                            else f"the operator in {namespace} has no ready replica",
                        }
                    )
    for companion in pending:
        meta = companion.get("metadata") or {}
        kind, name = companion.get("kind") or "Service", meta.get("name")
        about = [
            event
            for event in events or ()
            if (event.get("involvedObject") or event.get("involved_object") or {}).get(
                "name"
            )
            == name
            and (event.get("involvedObject") or event.get("involved_object") or {}).get(
                "kind"
            )
            == kind
        ]
        latest = max(
            about,
            key=lambda e: str(
                e.get("lastTimestamp")
                or e.get("last_timestamp")
                or e.get("eventTime")
                or e.get("event_time")
                or (e.get("metadata") or {}).get("creationTimestamp")
                or ""
            ),
            default=None,
        )
        notes.append(
            {
                "code": TAILNET_PENDING,
                "companion": name,
                "kind": kind,
                "detail": "no tailnet name yet"
                + (
                    f"; the operator's latest event: {latest.get('reason')}: "
                    f"{str(latest.get('message') or '')[:_EVENT_CHARS]}"
                    if latest
                    else ("; no event on it" if events is not None else "")
                ),
            }
        )
    return notes


def _tailnet_reads(
    target: KubeconfigTarget, access: Any, services: Sequence[Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(Ingress companions, notes)``: read-only, every read optional."""
    from piceli.envs.access import ACCESS_LABEL
    from piceli.services.query import KubernetesReader
    from piceli.services.registration import Registration

    reader = KubernetesReader(Registration("access-env", "access", target))
    try:

        def listed(api: str, kind: str, namespace: str) -> list[dict[str, Any]] | None:
            try:
                return reader.list(api, kind, namespace)
            except Exception:  # forbidden or absent: the note says so
                return None

        ingresses = [
            raw
            for raw in listed("networking.k8s.io/v1", "Ingress", target.namespace) or ()
            if ((raw.get("metadata") or {}).get("labels") or {}).get(ACCESS_LABEL)
            == "tailnet"
        ]
        for raw in ingresses:
            raw.setdefault("kind", "Ingress")
        companions = [*services, *ingresses]
        if companions and all(_host(c) for c in companions):
            return ingresses, []
        events = listed("v1", "Event", target.namespace)
        deployments = listed("apps/v1", "Deployment", access.operator_namespace)
        pods = listed("v1", "Pod", access.operator_namespace)
        try:
            reader.client.resources.get(
                api_version="networking.k8s.io/v1", kind="IngressClass"
            ).get(name="tailscale")
            ingress_class: bool | None = True
        except Exception as error:
            ingress_class = False if getattr(error, "status", None) == 404 else None
        operator = (
            None
            if deployments is None and ingress_class is None
            else {
                "deployments": deployments or [],
                "pods": pods or [],
                "ingress_class": bool(ingress_class),
            }
        )
        return ingresses, tailnet_notes(access, companions, events, operator)
    finally:
        reader.close()


def composition_env_target(
    cluster: str,
    env: str,
    root: Path,
    *,
    services: Callable[[KubeconfigTarget], list[dict[str, Any]]] | None = None,
    port: Callable[[], int] | None = None,
    tailnet_reads: Callable[..., tuple[list[dict[str, Any]], list[dict[str, Any]]]]
    | None = None,
) -> AccessTarget:
    """The :class:`AccessTarget` of environment ``env`` of the composition holding ``cluster``.

    :param cluster: ``infra.py:ATTR``, the composition module and its Cluster.
    :raises AccessTargetError: ``access-target-invalid`` (not a Cluster in a
        composition), ``env-not-found`` (no such named environment), or a
        profile's code when its credentials cannot be resolved.
    """
    from piceli.app.render import load_target
    from piceli.envs.model import Environment
    from piceli.envs.ops import free_port
    from piceli.infra import Cluster, CompositionError
    from piceli.infra.composition import load_composition
    from piceli.profiles import ProfileError, resolve

    file, sep, _attr = cluster.rpartition(":")
    if not sep or not file.endswith(".py"):
        raise AccessTargetError(
            "access-target-invalid", "--cluster takes infra.py:CLUSTER"
        )
    try:
        declared = load_target(cluster, root)
        composition = load_composition(file, root)
    except (CompositionError, ImportError, AttributeError, OSError, ValueError):
        raise AccessTargetError(
            "access-target-invalid", f"{cluster} is not a composition's Cluster"
        ) from None
    if not isinstance(declared, Cluster):
        raise AccessTargetError(
            "access-target-invalid", f"{cluster} is not a piceli.infra.Cluster"
        )
    environment = next(
        (
            item
            for item in composition.environments
            if isinstance(item, Environment) and item.name == env
        ),
        None,
    )
    if environment is None:
        raise AccessTargetError(
            "env-not-found",
            f"the composition declares no environment named {env!r}",
        )
    try:
        credentials = resolve(declared.credentials)
    except ProfileError as error:
        raise AccessTargetError(error.code, str(error)) from None
    target = KubeconfigTarget(
        credentials.kubeconfig, credentials.context, environment.namespace
    )
    try:
        live = (services or _live_services)(target)
    except AccessTargetError:
        raise
    except Exception as error:  # provider detail may name paths; keep the code
        code = getattr(error, "code", None)
        raise AccessTargetError(
            code if isinstance(code, str) else "access-target-invalid",
            f"the services of {environment.namespace} could not be read",
        ) from None
    from piceli.envs.access import ACCESS_LABEL

    companions = [
        raw for raw in live
        if ((raw.get("metadata") or {}).get("labels") or {}).get(ACCESS_LABEL) == "tailnet"
    ]  # fmt: skip
    for raw in companions:
        raw.setdefault("kind", "Service")
    live = [raw for raw in live if raw not in companions]
    access = environment.access or getattr(declared, "access", None)
    notes: list[dict[str, Any]] = []
    if access is not None and access.tailnet:
        # 0.19: HTTPS companions (Ingresses), and why nothing is reachable yet.
        try:
            ingresses, notes = (tailnet_reads or _tailnet_reads)(
                target, access, companions
            )
        except Exception:  # diagnostics never stop the forwards
            ingresses, notes = [], []
        companions += ingresses
    declared = declared_forwards(environment, live)
    covered = {(item.target, item.remote_port) for item in declared}
    taken = {item.id for item in declared}
    shortcuts = declared + tuple(
        item
        for item in service_forwards(
            environment.namespace, live, port=port or free_port
        )
        if (item.target, item.remote_port) not in covered and item.id not in taken
    )
    return AccessTarget(
        tailnet=tailnet_urls(shortcuts, companions),
        tailnet_notes=tuple(notes),
        name=f"{composition.name} environment {env}",
        namespace=environment.namespace,
        kubeconfig=credentials.kubeconfig,
        context=credentials.context,
        source="composition",
        shortcuts=shortcuts,
        workloads=tuple(
            dict.fromkeys(
                (
                    "Service" if item.target.startswith("service/") else "Deployment",
                    item.target.split("/", 1)[1],
                )
                for item in shortcuts
            )
        ),
    )
