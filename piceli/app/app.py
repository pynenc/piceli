"""The :class:`App` builder: typed declarations that render to resource intents."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from piceli.app.access import Access, Forward
from piceli.app.model import (
    Config,
    Container,
    ContainerPort,
    Deployment,
    EnvValue,
    Labels,
    Mount,
    Name,
    NetworkPolicy,
    Probe,
    Resources,
    Secret,
    Service,
    ServicePort,
    Volume,
)
from piceli.k8s.ops.plan import (
    DeploymentComponent,
    DeploymentComposition,
    ResourceIntent,
)

if TYPE_CHECKING:
    from piceli.k8s.ops.secret_versions import SecretVersionRef
    from piceli.k8s.ui_config import UiShortcut

_NAMESPACE = re.compile(r"[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?")


class NodeLike(Protocol):
    """A verified node, such as :class:`~piceli.k8s.release_spec.NodeRef`."""

    @property
    def name(self) -> str: ...


class ContextLike(Protocol):
    """What :meth:`App.composition` reads from a ``ReleaseContext``."""

    @property
    def namespace(self) -> str: ...

    @property
    def nodes(self) -> Mapping[str, NodeLike]: ...


class ComponentSource(Protocol):
    """A typed template that yields a component, such as ``NodeLocalRegistry``."""

    def component(self, namespace: str) -> DeploymentComponent: ...


Declared = Config | Secret | Deployment | Service | NetworkPolicy
Handle = Declared | str


def _pointer(key: str) -> str:
    return "/data/" + key.replace("~", "~0").replace("/", "~1")


class App(BaseModel):
    """A typed application: workloads, configuration and policies in one namespace.

    Declare objects with :meth:`deployment`, :meth:`service`, :meth:`config`,
    :meth:`secret` and :meth:`network_policy`, order components with
    :meth:`depends`, and render with :meth:`composition` (from a release
    context) or :meth:`render` (from a namespace). Rendering is pure: it never
    contacts a cluster.

    :param name: Application name, a DNS label.
    :param owner: The release owner expected to manage this app. It is
        recorded for tooling and never rendered: ``piceli release`` stamps
        ``piceli.io/owner`` from ``release.toml`` at apply time.
    :param labels: Labels on every object. Defaults to
        ``{"app.kubernetes.io/part-of": name}``. Workloads add their selector
        labels on top.

    Components: every object belongs to a component (the unit of ordering and
    readiness in a release). A Deployment's default component is its own name;
    a Service or NetworkPolicy joins its Deployment's component; a config or
    secret defaults to its own name. A Deployment depends on the components of
    every config and secret of this app that it reads; add other edges with
    :meth:`depends`.

    Example::

        app = App("shop")
        api = app.deployment(
            "api", image=ctx.image("api"), ports=[8080],
            ready=app.probe.http("/healthz", 8080),
        )
        app.service(api, port=8080)
        composition = app.composition(ctx)
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Probe factories: ``app.probe.http(...)``, ``.tcp(...)``, ``.exec(...)``.
    probe: ClassVar[type[Probe]] = Probe
    #: Access factories: ``app.access.forward(local=..., path=..., health=...)``.
    access: ClassVar[type[Access]] = Access

    name: Name
    owner: str | None = Field(default=None, min_length=1, max_length=253)
    labels: Labels | None = None

    _objects: list[Declared] = PrivateAttr(default_factory=list)
    _extra: list[DeploymentComponent | ComponentSource] = PrivateAttr(
        default_factory=list
    )
    _edges: list[tuple[str, str]] = PrivateAttr(default_factory=list)

    def __init__(self, name: str, /, **data: Any) -> None:
        super().__init__(name=name, **data)

    # ----------------------------------------------------------- declarations

    @property
    def object_labels(self) -> dict[str, str]:
        """Labels on every object of this app."""
        if self.labels is None:
            return {"app.kubernetes.io/part-of": self.name}
        return dict(self.labels)

    @property
    def objects(self) -> tuple[Declared, ...]:
        """Everything declared so far, in declaration order."""
        return tuple(self._objects)

    def _declare[T: Declared](self, item: T) -> T:
        kind = type(item).__name__
        for existing in self._objects:
            if type(existing) is type(item) and existing.name == item.name:
                raise ValueError(f"{kind} {item.name!r} is already declared")
        forward = _forward(item)
        if forward is not None:
            ident = forward.name or item.name
            for other in self._objects:
                declared = _forward(other)
                if declared is None:
                    continue
                if (declared.name or other.name) == ident:
                    raise ValueError(f"access forward {ident!r} is already declared")
                if declared.local == forward.local:
                    raise ValueError(
                        f"access forward {ident!r}: local port {forward.local} is "
                        f"already used by {declared.name or other.name!r}"
                    )
        self._objects.append(item)
        return item

    def shortcuts(self, namespace: str | None = None) -> tuple[UiShortcut, ...]:
        """The declared access forwards as access-profile entries.

        These are the same :class:`~piceli.k8s.ui_config.UiShortcut` objects an
        ``--ui-config`` TOML file declares, so ``piceli access``, the forward
        supervisor and the dashboard consume them unchanged. Pure: no cluster.

        :param namespace: Pinned on every entry (``None`` leaves it to the caller).
        """
        result = []
        for item in self._objects:
            if isinstance(item, Service) and item.access is not None:
                result.append(
                    item.access.shortcut(
                        item.name, f"service/{item.name}", item.access_port(), namespace
                    )
                )
            elif isinstance(item, Deployment) and item.access is not None:
                result.append(
                    item.access.shortcut(
                        item.name,
                        f"deployment/{item.name}",
                        item.access_port(),
                        namespace,
                    )
                )
        return tuple(result)

    def config(
        self, name: str, data: Mapping[str, str], *, component: str | None = None
    ) -> Config:
        """Declare a ConfigMap with public string values."""
        return self._declare(Config(name=name, data=dict(data), component=component))

    def secret(
        self,
        name: str,
        data: Mapping[str, SecretVersionRef],
        *,
        type: str = "Opaque",
        component: str | None = None,
    ) -> Secret:
        """Declare a Secret whose values are opaque versions from ``ctx.secret``."""
        return self._declare(
            Secret(name=name, data=dict(data), type=type, component=component)
        )

    def deployment(
        self,
        name: str,
        *,
        image: str,
        command: Sequence[str] | None = None,
        args: Sequence[str] | None = None,
        working_dir: str | None = None,
        env: Mapping[str, EnvValue] | None = None,
        ports: Sequence[int | ContainerPort] = (),
        ready: Probe | None = None,
        live: Probe | None = None,
        startup: Probe | None = None,
        resources: Resources | None = None,
        volumes: Mapping[str, Volume | Mount] | None = None,
        pull_policy: str | None = None,
        sidecars: Sequence[Container] = (),
        init: Sequence[Container] = (),
        replicas: int = 1,
        share_process_namespace: bool = False,
        node: str | None = None,
        strategy: str | None = None,
        service_account: str | None = None,
        selector: Mapping[str, str] | None = None,
        labels: Mapping[str, str] | None = None,
        component: str | None = None,
        access: Forward | None = None,
    ) -> Deployment:
        """Declare a Deployment whose main container is named after it.

        Container arguments (``image`` … ``pull_policy``) describe the main
        container (see :class:`~piceli.app.model.Container`); ``sidecars`` run
        next to it and ``init`` containers run first. The remaining arguments
        are :class:`~piceli.app.model.Deployment` fields; ``access`` declares a
        loopback forward to the pods (prefer a Service's ``access``).
        """
        main = Container.model_validate(
            {
                "name": name,
                "image": image,
                "command": command,
                "args": args,
                "working_dir": working_dir,
                "env": dict(env or {}),
                "ports": tuple(ports),
                "ready": ready,
                "live": live,
                "startup": startup,
                "resources": resources,
                "volumes": dict(volumes or {}),
                "pull_policy": pull_policy,
            }
        )
        return self._declare(
            Deployment.model_validate(
                {
                    "name": name,
                    "containers": (main, *sidecars),
                    "init_containers": tuple(init),
                    "replicas": replicas,
                    "share_process_namespace": share_process_namespace,
                    "node": node,
                    "strategy": strategy,
                    "service_account": service_account,
                    "selector": dict(selector) if selector is not None else None,
                    "labels": dict(labels or {}),
                    "component": component,
                    "access": access,
                }
            )
        )

    def service(
        self,
        workload: Deployment,
        port: int | None = None,
        *,
        target_port: int | str | None = None,
        ports: Sequence[ServicePort] | None = None,
        name: str | None = None,
        type: str | None = None,
        access: Forward | None = None,
    ) -> Service:
        """Declare a Service that selects ``workload``'s pods.

        Pass one ``port`` (and optionally ``target_port``) or several named
        :class:`~piceli.app.model.ServicePort` objects. The Service is named
        after the workload unless ``name`` is given. ``access`` declares how
        to reach it from a laptop (``app.access.forward(local=...)``); it adds
        nothing to the manifest.
        """
        if (port is None) == (ports is None):
            raise ValueError("pass either port= or ports=")
        if ports is None:
            ports = (ServicePort(port=port, target_port=target_port),)  # type: ignore[arg-type]
        elif target_port is not None:
            raise ValueError("target_port= only applies with port=")
        return self._declare(
            Service.model_validate(
                {
                    "name": name or workload.name,
                    "selector": workload.selector_labels,
                    "ports": tuple(ports),
                    "type": type,
                    "component": workload.component_name,
                    "access": access,
                }
            )
        )

    def network_policy(
        self,
        workload: Deployment,
        *,
        allow_from: Sequence[Deployment] = (),
        ports: Sequence[int] = (),
        name: str | None = None,
    ) -> NetworkPolicy:
        """Restrict ingress to ``workload``'s pods (see :class:`~piceli.app.model.NetworkPolicy`).

        Named ``<workload>-ingress`` unless ``name`` is given.
        """
        return self._declare(
            NetworkPolicy.model_validate(
                {
                    "name": name or f"{workload.name}-ingress",
                    "pod_selector": workload.selector_labels,
                    "allow_from": tuple(peer.selector_labels for peer in allow_from),
                    "ports": tuple(ports),
                    "component": workload.component_name,
                }
            )
        )

    def add(self, component: DeploymentComponent | ComponentSource) -> None:
        """Include a component built elsewhere.

        Accepts a ``DeploymentComponent`` or a template with a
        ``component(namespace)`` method, such as
        :class:`~piceli.k8s.templates.NodeLocalRegistry`; the namespace is
        supplied at render time.
        """
        self._extra.append(component)

    def depends(self, dependent: Handle, *, on: Handle | Sequence[Handle]) -> None:
        """Make ``dependent``'s component wait for the components in ``on``.

        Arguments are declared objects or component names.
        """
        targets = [on] if isinstance(on, str | BaseModel) else list(on)
        source = _component(dependent)
        for target in targets:
            name = _component(target)
            if name == source:
                raise ValueError(f"component {source!r} cannot depend on itself")
            self._edges.append((source, name))

    # --------------------------------------------------------------- render

    def composition(self, ctx: ContextLike) -> DeploymentComposition:
        """Render for a release: the namespace and verified nodes come from ``ctx``.

        A release composition function can simply ``return app.composition(ctx)``.
        """
        return self.render(ctx.namespace, nodes=ctx.nodes)

    def __call__(self, ctx: ContextLike) -> DeploymentComposition:
        return self.composition(ctx)

    def render(
        self, namespace: str, *, nodes: Mapping[str, NodeLike] | None = None
    ) -> DeploymentComposition:
        """Render every declaration to ``ResourceIntent`` objects.

        :param namespace: Target namespace of every object.
        :param nodes: Verified nodes by alias, for ``node=`` pinning.
        :raises ValueError: on an unknown node alias, an unknown component in
            :meth:`depends`, or a managed claim that an ``ExistingClaim`` mounts.
        """
        if not _NAMESPACE.fullmatch(namespace or ""):
            raise ValueError(f"invalid namespace: {namespace!r}")
        nodes = nodes or {}
        labels = self.object_labels
        owners = {
            (kind, item.name): item.component_name
            for item in self._objects
            for kind, cls in (("ConfigMap", Config), ("Secret", Secret))
            if isinstance(item, cls)
        }
        resources: dict[str, list[ResourceIntent]] = {}
        edges: dict[str, set[str]] = {}
        claims: set[str] = set()
        for item in self._objects:
            component = item.component_name
            resources.setdefault(component, []).append(
                self._intent(item, namespace, labels, nodes)
            )
            if isinstance(item, Deployment):
                claims |= item.existing_claims()
                for ref in item.references():
                    owner = owners.get(ref)
                    if owner is not None and owner != component:
                        edges.setdefault(component, set()).add(owner)
        for source, target in self._edges:
            edges.setdefault(source, set()).add(target)
        components = [
            DeploymentComponent(name, tuple(items), tuple(edges.get(name, ())))
            for name, items in resources.items()
        ]
        for extra in self._extra:
            built = (
                extra
                if isinstance(extra, DeploymentComponent)
                else extra.component(namespace)
            )
            if built.name in resources:
                raise ValueError(f"component {built.name!r} is both declared and added")
            components.append(
                DeploymentComponent(
                    built.name,
                    built.resources,
                    (*built.dependencies, *edges.get(built.name, ())),
                )
            )
        known = {component.name for component in components}
        unknown = sorted({name for pair in self._edges for name in pair} - known)
        if unknown:
            raise ValueError(
                f"depends() names unknown components {unknown}; known: {sorted(known)}"
            )
        for component in components:
            for resource in component.resources:
                if (
                    resource.ref.kind == "PersistentVolumeClaim"
                    and resource.ref.name in claims
                ):
                    raise ValueError(
                        f"PersistentVolumeClaim {resource.ref.name!r} is mounted as "
                        "an ExistingClaim and must never be managed by the release; "
                        "remove it from the composition"
                    )
        return DeploymentComposition(tuple(components))

    def _intent(
        self,
        item: Declared,
        namespace: str,
        labels: Mapping[str, str],
        nodes: Mapping[str, NodeLike],
    ) -> ResourceIntent:
        metadata: dict[str, Any] = {"name": item.name, "namespace": namespace}
        if labels:
            metadata["labels"] = dict(labels)
        if isinstance(item, Config):
            return ResourceIntent.from_manifest(
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": metadata,
                    "data": dict(item.data),
                }
            )
        if isinstance(item, Secret):
            intent = ResourceIntent.from_manifest(
                {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "metadata": metadata,
                    "type": item.type,
                    "data": dict.fromkeys(item.data, "<private>"),
                }
            )
            for key, reference in item.data.items():
                intent = intent.with_secret(_pointer(key), reference)
            return intent
        if isinstance(item, Deployment):
            node_name = None
            if item.node is not None:
                if item.node not in nodes:
                    raise ValueError(
                        f"deployment {item.name!r} is pinned to node {item.node!r}, "
                        f"which the target does not declare; verified nodes: "
                        f"{sorted(nodes)}"
                    )
                node_name = nodes[item.node].name
            return ResourceIntent.from_manifest(
                item.manifest(namespace, labels, node_name)
            )
        return ResourceIntent.from_manifest(item.manifest(namespace, labels))


def _forward(item: Declared) -> Forward | None:
    if isinstance(item, Service | Deployment):
        return item.access
    return None


def _component(value: Handle) -> str:
    if isinstance(value, str):
        return value
    return value.component_name
