"""The :class:`App` builder: typed declarations that render to resource intents."""

from __future__ import annotations

import json
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
    PodDefaults,
    Probe,
    Resources,
    Rule,
    Secret,
    Security,
    Service,
    ServiceAccount,
    ServicePort,
    Volume,
)
from piceli.k8s.ops.discovery import RELEASE_NAMESPACE_ANNOTATION
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


Declared = Config | Secret | Deployment | Service | NetworkPolicy | ServiceAccount
Handle = Declared | str


def _pointer(key: str) -> str:
    return "/data/" + key.replace("~", "~0").replace("/", "~1")


class App(BaseModel):
    """A typed application: workloads, configuration and policies in one namespace.

    Declare objects with :meth:`deployment`, :meth:`service`, :meth:`config`,
    :meth:`secret`, :meth:`service_account` and :meth:`network_policy`, order
    components with :meth:`depends`, and render with :meth:`composition` (from
    a release context) or :meth:`render` (from a namespace). Rendering is
    pure: it never contacts a cluster.

    :param name: Application name, a DNS label.
    :param owner: The release owner expected to manage this app. It is
        recorded for tooling and never rendered: ``piceli release`` stamps
        ``piceli.io/owner`` from ``release.toml`` at apply time.
    :param labels: Labels on every object. Defaults to
        ``{"app.kubernetes.io/part-of": name}``. Workloads add their selector
        labels on top, and every pod carries them (see :attr:`release_selector`).
    :param pod_defaults: Pod settings applied to every Deployment declared on
        this app (:class:`~piceli.app.model.PodDefaults`): security, an extra
        node selector, the termination grace period and token automounting.
        A workload's own typed arguments win; ``override`` still patches last.

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
    pod_defaults: PodDefaults | None = None

    _objects: list[Declared] = PrivateAttr(default_factory=list)
    _extra: list[DeploymentComponent | ComponentSource] = PrivateAttr(
        default_factory=list
    )
    _edges: list[tuple[str, str]] = PrivateAttr(default_factory=list)
    _overrides: list[tuple[str, str, dict[str, Any]]] = PrivateAttr(
        default_factory=list
    )

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
    def release_selector(self) -> dict[str, str]:
        """Labels every pod of this app carries: a selector for "the whole app".

        Use it with :meth:`network_policy`, for example to let only this app's
        pods connect to each other. These are :attr:`object_labels`, so they
        are stable as long as the app ``name`` and ``labels`` are.

        :raises ValueError: when the app was declared with ``labels={}``.
        """
        labels = self.object_labels
        if not labels:
            raise ValueError(
                f"app {self.name!r} has no labels (labels={{}}), so no selector "
                "matches all its pods; declare labels= on the App"
            )
        return labels

    @property
    def objects(self) -> tuple[Declared, ...]:
        """Everything declared so far, in declaration order."""
        return tuple(self._objects)

    def _declare[T: Declared](self, item: T) -> T:
        kind = type(item).__name__
        for existing in self._objects:
            if type(existing) is type(item) and existing.name == item.name:
                raise ValueError(f"{kind} {item.name!r} is already declared")
        if isinstance(item, Deployment):
            item.check_defaults(self.pod_defaults)
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
        container: str | None = None,
        sidecars: Sequence[Container] = (),
        init: Sequence[Container] = (),
        replicas: int = 1,
        share_process_namespace: bool = False,
        node: str | None = None,
        strategy: str | None = None,
        service_account: ServiceAccount | str | None = None,
        security: Security | None = None,
        node_selector: Mapping[str, str] | None = None,
        termination_grace_seconds: int | None = None,
        automount_token: bool | None = None,
        selector: Mapping[str, str] | None = None,
        labels: Mapping[str, str] | None = None,
        component: str | None = None,
        access: Forward | None = None,
    ) -> Deployment:
        """Declare a Deployment whose main container is named after it.

        Container arguments (``image`` … ``pull_policy``) describe the main
        container (see :class:`~piceli.app.model.Container`); ``container``
        names it when it must not be named after the Deployment. ``sidecars``
        run next to it and ``init`` containers run first. The remaining
        arguments are :class:`~piceli.app.model.Deployment` fields; ``access``
        declares a loopback forward to the pods (prefer a Service's ``access``).

        ``service_account`` is a :class:`~piceli.app.model.ServiceAccount`
        from :meth:`service_account` (its pods get a token and its
        permissions) or the name of one the app does not manage.
        ``security``, ``node_selector`` and ``termination_grace_seconds`` are
        layered over the app's ``pod_defaults``.
        """
        if isinstance(service_account, ServiceAccount):
            if not any(existing is service_account for existing in self._objects):
                raise ValueError(
                    f"service_account={service_account.name!r} is not declared on "
                    "this app; declare it with app.service_account(...) first"
                )
            service_account = service_account.name
        main = Container.model_validate(
            {
                "name": name if container is None else container,
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
                    "security": security,
                    "node_selector": (
                        dict(node_selector) if node_selector is not None else None
                    ),
                    "termination_grace_seconds": termination_grace_seconds,
                    "automount_token": automount_token,
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

    def service_account(
        self,
        name: str,
        *,
        rules: Sequence[Rule] = (),
        cluster_rules: Sequence[Rule] = (),
        component: str | None = None,
    ) -> ServiceAccount:
        """Declare a ServiceAccount and the permissions its pods get.

        Bind it to a workload with ``app.deployment(..., service_account=sa)``.

        :param name: ServiceAccount name; the Role and RoleBinding share it.
        :param rules: Namespaced permissions (:class:`~piceli.app.model.Rule`):
            a Role and RoleBinding in the release namespace.
        :param cluster_rules: Cluster-wide permissions: a ClusterRole and
            ClusterRoleBinding named ``<namespace>:<app>:<name>``, annotated
            ``piceli.io/namespace: <namespace>``. Only the release in that
            namespace manages them; the deployer needs cluster RBAC rights.
        :param component: Deployment component; defaults to ``name``.

        Tokens: the ServiceAccount renders
        ``automountServiceAccountToken: false``; a pod bound to it with
        ``service_account=`` renders ``true`` (unless the workload sets
        ``automount_token``), so only the pods you bind get API credentials.

        Example::

            watcher = app.service_account(
                "watcher",
                rules=[Rule(resources=["pods"], verbs=["get", "list", "watch"])],
                cluster_rules=[Rule(resources=["nodes"], verbs=["get", "list"])],
            )
            app.deployment("watcher", image=..., service_account=watcher)
        """
        return self._declare(
            ServiceAccount.model_validate(
                {
                    "name": name,
                    "rules": tuple(rules),
                    "cluster_rules": tuple(cluster_rules),
                    "component": component,
                }
            )
        )

    def network_policy(
        self,
        workload: Deployment | None = None,
        *,
        selector: Mapping[str, str] | None = None,
        allow_from: Sequence[Deployment] = (),
        allow_from_selector: Mapping[str, str]
        | Sequence[Mapping[str, str]]
        | None = None,
        ports: Sequence[int] = (),
        name: str | None = None,
        component: str | None = None,
    ) -> NetworkPolicy:
        """Restrict ingress to selected pods (see :class:`~piceli.app.model.NetworkPolicy`).

        Select the protected pods with ``workload`` (its selector) or with
        ``selector`` (any pod labels, such as :attr:`release_selector` for
        every pod of this app). Sources are ``allow_from`` workloads and
        ``allow_from_selector`` label sets (one mapping or several), in the
        same namespace.

        Named ``<workload>-ingress`` for a workload; a ``selector`` policy
        needs ``name``. Its component is the workload's, else ``component``,
        else ``name``.

        Example, only this app's pods may connect to its pods::

            app.network_policy(
                selector=app.release_selector,
                allow_from_selector=app.release_selector,
                name="shop-internal",
            )
        """
        if (workload is None) == (selector is None):
            raise ValueError("pass either a workload or selector=")
        if workload is None and name is None:
            raise ValueError("a network policy with selector= needs name=")
        if allow_from_selector is None:
            peers: list[Mapping[str, str]] = []
        elif isinstance(allow_from_selector, Mapping):
            peers = [allow_from_selector]
        else:
            peers = list(allow_from_selector)
        for peer in peers:
            if not peer:
                raise ValueError(
                    "allow_from_selector needs labels; an empty selector would "
                    "allow every pod in the namespace"
                )
        pod_selector = workload.selector_labels if workload else dict(selector or {})
        policy_name = name or f"{workload.name}-ingress"  # type: ignore[union-attr]
        return self._declare(
            NetworkPolicy.model_validate(
                {
                    "name": policy_name,
                    "pod_selector": pod_selector,
                    "allow_from": (
                        *(peer.selector_labels for peer in allow_from),
                        *(dict(peer) for peer in peers),
                    ),
                    "ports": tuple(ports),
                    "component": workload.component_name
                    if workload
                    else component or policy_name,
                }
            )
        )

    def override(self, item: Declared, patch: Mapping[str, Any]) -> None:
        """Set fields of ``item``'s rendered manifest that the typed model lacks.

        The escape hatch for fields with no typed argument yet (tolerations,
        an annotation, a list the model orders differently). ``patch`` is
        merged into the manifest when the app is rendered, after the typed
        fields and the app's ``pod_defaults``, in call order:

        * a mapping merges key by key, and ``None`` removes a key;
        * a list of objects that all have a unique ``name`` (containers, env,
          volumes, ports) merges item by item on ``name``; items with a new
          name are appended;
        * any other value, including any other list, replaces the rendered one.

        ``piceli import`` writes one ``override`` per imported object that has
        untyped fields, with a comment naming each field.

        Invariants: the object's identity (``apiVersion``, ``kind``,
        ``metadata.name``, ``metadata.namespace``) cannot be overridden, and a
        Secret's ``data`` and ``stringData`` cannot be overridden (secret
        values never appear in the model).

        For a ServiceAccount, the patch applies to the ServiceAccount object
        (its Role and binding objects follow from its rules).

        Example::

            app.override(api, {"spec": {"template": {"spec": {
                "tolerations": [{"key": "dedicated", "operator": "Exists"}],
            }}}})
        """
        if not any(existing is item for existing in self._objects):
            raise ValueError(
                f"override() takes an object declared on this app, got "
                f"{type(item).__name__} {getattr(item, 'name', '?')!r}"
            )
        _check_override(item, patch)
        self._overrides.append(
            (_KINDS[type(item)], item.name, json.loads(json.dumps(dict(patch))))
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
            for kind, cls in (
                ("ConfigMap", Config),
                ("Secret", Secret),
                ("ServiceAccount", ServiceAccount),
            )
            if isinstance(item, cls)
        }
        resources: dict[str, list[ResourceIntent]] = {}
        edges: dict[str, set[str]] = {}
        claims: set[str] = set()
        for item in self._objects:
            component = item.component_name
            resources.setdefault(component, []).extend(
                self._intents(item, namespace, labels, nodes)
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

    def _intents(
        self,
        item: Declared,
        namespace: str,
        labels: Mapping[str, str],
        nodes: Mapping[str, NodeLike],
    ) -> list[ResourceIntent]:
        metadata: dict[str, Any] = {"name": item.name, "namespace": namespace}
        if labels:
            metadata["labels"] = dict(labels)
        manifest: dict[str, Any]
        extra: list[dict[str, Any]] = []
        if isinstance(item, Config):
            manifest = {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": metadata,
                "data": dict(item.data),
            }
        elif isinstance(item, Secret):
            manifest = {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": metadata,
                "type": item.type,
                "data": dict.fromkeys(item.data, "<private>"),
            }
        elif isinstance(item, Deployment):
            node_name = None
            if item.node is not None:
                if item.node not in nodes:
                    raise ValueError(
                        f"deployment {item.name!r} is pinned to node {item.node!r}, "
                        f"which the target does not declare; verified nodes: "
                        f"{sorted(nodes)}"
                    )
                node_name = nodes[item.node].name
            manifest = item.manifest(
                namespace,
                labels,
                node_name,
                self.pod_defaults,
                self._automount(item),
            )
        elif isinstance(item, ServiceAccount):
            manifest, *extra = item.manifests(
                namespace, self.name, labels, RELEASE_NAMESPACE_ANNOTATION
            )
        else:
            manifest = item.manifest(namespace, labels)
        kind = _KINDS[type(item)]
        for target_kind, name, patch in self._overrides:
            if (target_kind, name) == (kind, item.name):
                manifest = merge_override(manifest, patch)
        intent = ResourceIntent.from_manifest(manifest)
        if isinstance(item, Secret):
            for key, reference in item.data.items():
                intent = intent.with_secret(_pointer(key), reference)
        return [intent, *(ResourceIntent.from_manifest(value) for value in extra)]

    def _automount(self, item: Deployment) -> bool | None:
        """Token automounting when the workload sets none (see ``service_account``)."""
        if item.service_account is not None and any(
            isinstance(other, ServiceAccount) and other.name == item.service_account
            for other in self._objects
        ):
            return True
        return self.pod_defaults.automount_token if self.pod_defaults else None


_KINDS: dict[type, str] = {
    Config: "ConfigMap",
    Secret: "Secret",
    Deployment: "Deployment",
    Service: "Service",
    NetworkPolicy: "NetworkPolicy",
    ServiceAccount: "ServiceAccount",
}


def _check_override(item: Declared, patch: Mapping[str, Any]) -> None:
    if not isinstance(patch, Mapping):
        raise ValueError("an override patch must be a mapping")
    json.dumps(dict(patch), allow_nan=False)  # plain JSON values only
    for key in ("apiVersion", "kind"):
        if key in patch:
            raise ValueError(f"an override cannot change {key}")
    metadata = patch.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, Mapping):
            raise ValueError("an override's metadata must be a mapping")
        for key in ("name", "namespace"):
            if key in metadata:
                raise ValueError(f"an override cannot change metadata.{key}")
    if isinstance(item, Secret) and ({"data", "stringData"} & set(patch)):
        raise ValueError(
            "an override cannot set Secret data; secret values come from "
            "ctx.secret(...) in app.secret(...)"
        )


def _named_items(value: Any) -> list[str] | None:
    """The item names when ``value`` is a list of uniquely named objects."""
    if not isinstance(value, list):
        return None
    names = [item.get("name") if isinstance(item, dict) else None for item in value]
    if any(not isinstance(name, str) for name in names) or len(set(names)) != len(
        names
    ):
        return None
    return names  # type: ignore[return-value]


def merge_override(base: Any, patch: Any) -> Any:
    """Merge an :meth:`App.override` patch into ``base`` (see its rules).

    Pure: returns a new value and never changes ``base`` or ``patch``.
    """
    if isinstance(patch, Mapping):
        result = dict(base) if isinstance(base, Mapping) else {}
        for key, value in patch.items():
            if value is None:
                result.pop(key, None)
            else:
                result[key] = merge_override(result.get(key), value)
        return result
    if _named_items(base) is not None and _named_items(patch) is not None:
        merged = [dict(item) for item in base]
        index = {item["name"]: position for position, item in enumerate(merged)}
        for item in patch:
            position = index.get(item["name"])
            if position is None:
                merged.append(merge_override({}, item))
            else:
                merged[position] = merge_override(merged[position], item)
        return merged
    return json.loads(json.dumps(patch))


def _forward(item: Declared) -> Forward | None:
    if isinstance(item, Service | Deployment):
        return item.access
    return None


def _component(value: Handle) -> str:
    if isinstance(value, str):
        return value
    return value.component_name
