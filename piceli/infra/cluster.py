"""The rules of a declared :class:`~piceli.infra.Cluster`: validation, node labels.

``Cluster``, ``Node``, ``Controller`` and ``Ui`` (in :mod:`piceli.infra`)
check their values here when they are built, so a wrong declaration fails at
import with a fixed code (``cluster-invalid``) and never halfway through
``piceli cluster init``.

- Each role of a node becomes the label ``piceli.io/role-<role>=true``; role
  ``builder`` also sets ``piceli.io/builder=true``, which cluster builds
  select by default.
- ``api`` is compared with the server of the credential profile's kubeconfig
  context (:func:`api_matches`): a profile that points at another cluster is
  refused (``cluster-api-mismatch``) before anything is read or written.

Importing this module is side-effect free.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from piceli.infra import Cluster, Controller, Node, Ui

ROLE_PREFIX = "piceli.io/role-"
BUILDER_LABEL = "piceli.io/builder"
#: Node annotation listing the labels ``cluster init`` set (so a re-run removes
#: only its own labels when a role goes away).
MANAGED_LABELS = "piceli.io/managed-labels"
ARCHES = ("amd64", "arm64")
_NODE = re.compile(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?")
_ROLE = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,56}[a-z0-9])?")
_LABEL = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")
_API = re.compile(r"https?://[^\s/?#@]+/?")


class ClusterError(ValueError):
    """A cluster declaration or ``cluster``/``secrets`` command refused, with a code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _invalid(message: str) -> ClusterError:
    return ClusterError("cluster-invalid", message)


def as_tuple(value: Any, what: str) -> tuple[Any, ...]:
    """Lists (as written in a composition) become tuples; a string is refused."""
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise _invalid(f"{what} must be a list")
    return tuple(value)


def check_node(node: Node) -> None:
    if not isinstance(node.name, str) or not _NODE.fullmatch(node.name):
        raise _invalid("Node name must be a node name (kubernetes.io/hostname)")
    if node.arch not in ARCHES:
        raise _invalid(f"Node {node.name}: arch must be amd64 or arm64")
    for role in node.roles:
        if not isinstance(role, str) or not _ROLE.fullmatch(role):
            raise _invalid(
                f"Node {node.name}: a role is lowercase letters, digits and '-' "
                "(at most 58 characters)"
            )
    if len(set(node.roles)) != len(node.roles):
        raise _invalid(f"Node {node.name}: a role is listed twice")
    if node.target is not None:
        from piceli.artifacts.node_transport import NodeTarget

        try:
            NodeTarget.parse(node.target)
        except ValueError as error:
            raise _invalid(
                f"Node {node.name}: target= is ssh://[user@]host?runtime=containerd "
                f"(or k3s-containerd) or docker://name?runtime=containerd ({error})"
            ) from None


def node_target_identity(target: str) -> str:
    """``target`` without its SSH user: what the controller's config keeps."""
    from piceli.artifacts.node_transport import NodeTarget

    return NodeTarget.parse(target).identity


def node_labels(node: Node) -> dict[str, str]:
    """The labels ``cluster init`` puts on ``node`` for its roles."""
    labels = {f"{ROLE_PREFIX}{role}": "true" for role in node.roles}
    if "builder" in node.roles:
        labels[BUILDER_LABEL] = "true"
    return labels


def check_controller(controller: Controller) -> None:
    from piceli.gitops import GitOpsError
    from piceli.gitops.config import parse_duration

    if not isinstance(controller.on, str) or not _NODE.fullmatch(controller.on):
        raise _invalid("Controller(on=) must name a node")
    try:
        seconds = parse_duration(controller.poll)
    except GitOpsError:
        raise _invalid("Controller(poll=) must be like 60, 30s, 1m or 1h") from None
    if seconds < 10:
        raise _invalid("Controller(poll=) must be at least 10 seconds")
    if controller.sync != "on change":
        raise _invalid('Controller(sync=) must be "on change"')
    if not isinstance(controller.delete_volumes, bool):
        raise _invalid("Controller(delete_volumes=) must be True or False")
    if not isinstance(controller.prebuilt, bool):
        raise _invalid("Controller(prebuilt=) must be True or False")
    from piceli.infra import Otlp

    if controller.telemetry is not None and not isinstance(controller.telemetry, Otlp):
        raise _invalid("Controller(telemetry=) must be Otlp(...)")
    if controller.telemetry is not None and controller.telemetry.ca_file is not None:
        raise _invalid(
            "the controller runs in the cluster: Otlp(ca_file=) is a file on this "
            "machine; give the CA as a Secret with Otlp(ca_secret=)"
        )
    if controller.alarms is not None:
        from piceli.gitops.alarms import Alarms

        if not isinstance(controller.alarms, Alarms):
            raise _invalid("Controller(alarms=) must be Alarms(...)")
    builds = tuple(controller.builds) if not isinstance(controller.builds, str) else ()
    if (
        not builds
        or len(set(builds)) != len(builds)
        or not all(item in {"cluster", "hosts"} for item in builds)
    ):
        raise _invalid(
            'Controller(builds=) is ("cluster",), ("hosts",) or both, in order'
        )
    object.__setattr__(controller, "builds", builds)
    try:
        parse_duration(controller.host_wait)
    except GitOpsError:
        raise _invalid("Controller(host_wait=) must be like 30m or 2h") from None
    if controller.delivery not in {"registry", "nodes"}:
        raise _invalid('Controller(delivery=) is "registry" or "nodes"')
    if controller.delivery == "nodes" and "hosts" not in builds:
        raise _invalid(
            'Controller(delivery="nodes") is how build hosts deliver: it needs '
            'builds=("hosts",) or ("hosts", "cluster")'
        )
    after = controller.quarantine_after
    if after is not None and (
        not isinstance(after, int) or isinstance(after, bool) or after < 1
    ):
        raise _invalid("Controller(quarantine_after=) is a positive integer or None")
    try:
        parse_duration(controller.quarantine_window)
    except GitOpsError:
        raise _invalid("Controller(quarantine_window=) must be like 24h") from None


def check_ui(ui: Ui) -> None:
    """Nothing to refuse here: the UI installer checks its own values when it
    renders (``ui-install-image-unpinned``, ``-node-unknown``,
    ``-access-unsupported``) and ``cluster init`` reports those codes."""


def check_cluster(cluster: Cluster) -> None:
    from piceli.pipeline.model import ClusterRegistry
    from piceli.profiles import ProfileError, check_name

    if not isinstance(cluster.name, str) or not _LABEL.fullmatch(cluster.name):
        raise _invalid("Cluster name must be a DNS label (like my-cluster)")
    if not isinstance(cluster.api, str) or not _API.fullmatch(cluster.api):
        raise _invalid("Cluster(api=) must be the API URL, like https://10.0.0.1:6443")
    try:
        check_name(cluster.credentials)
    except ProfileError:
        raise _invalid(
            "Cluster(credentials=) must name a credential profile (piceli login NAME)"
        ) from None
    names = [node.name for node in cluster.nodes]
    if len(set(names)) != len(names):
        raise _invalid("a node is declared twice")
    if cluster.storage_class is not None and (
        not isinstance(cluster.storage_class, str)
        or not _NODE.fullmatch(cluster.storage_class)
    ):
        raise _invalid("invalid Cluster(storage_class=)")
    if cluster.registry is not None and not isinstance(
        cluster.registry, ClusterRegistry
    ):
        raise _invalid(
            "Cluster(registry=) must be Registry.in_cluster(on=NODE): the "
            "cluster installs it (a hosted registry needs nothing here)"
        )
    placed = {
        "Registry.in_cluster(on=)": getattr(cluster.registry, "on", None),
        "Controller(on=)": getattr(cluster.controller, "on", None),
    }
    for what, node in placed.items():
        if node is not None and names and node not in names:
            raise _invalid(f"{what} names {node!r}, which is not one of the nodes")
    controller = cluster.controller
    if controller is not None and controller.delivery == "nodes":
        if not cluster.nodes:
            raise _invalid(
                'Controller(delivery="nodes") imports images into the declared '
                "nodes: declare them with Node(name, arch=, target=...)"
            )
        missing = [node.name for node in cluster.nodes if node.target is None]
        if missing:
            raise _invalid(
                f'Controller(delivery="nodes"): node {missing[0]} has no target= '
                "(a workload can land on any node, so every node receives the images)"
            )
        if "cluster" in controller.builds and cluster.registry is None:
            raise _invalid(
                'Controller(builds=("hosts", "cluster")) falls back to the in-cluster '
                "build, which pushes to the registry: declare "
                'registry=Registry.in_cluster(...) or use builds=("hosts",)'
            )
    if cluster.access is not None:
        from piceli.envs.access import Access

        if not isinstance(cluster.access, Access):
            raise _invalid("Cluster(access=) must be Access(...)")
    if cluster.dev is not None:
        from piceli.dev.model import DevBuilds

        if not isinstance(cluster.dev, DevBuilds):
            raise _invalid("Cluster(dev=) must be DevBuilds(...)")
        if cluster.dev.node is not None and cluster.dev.node not in names:
            raise _invalid(
                f"DevBuilds(node={cluster.dev.node!r}) is not a declared node"
            )


def _origin(url: str) -> tuple[str, str, int] | None:
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None
    return (
        parts.scheme,
        parts.hostname.lower(),
        port or (443 if parts.scheme == "https" else 80),
    )


def api_matches(declared: str, server: str) -> bool:
    """Whether the profile's ``server`` is the declared ``api`` (scheme, host, port)."""
    left, right = _origin(declared), _origin(server)
    return left is not None and left == right


def kubeconfig_server(document: Mapping[str, Any], context: str) -> str | None:
    """The server URL of ``context`` in a parsed kubeconfig, if it names one."""
    from piceli.k8s.ops.exec_credentials import ProviderFactoryError
    from piceli.k8s.ops.provider_factory import _named

    try:
        entry = _named(document, "contexts", context)
        cluster = _named(document, "clusters", str(entry.get("cluster")))
    except ProviderFactoryError:
        return None
    server = cluster.get("server")
    return server if isinstance(server, str) else None


def describe(cluster: Cluster) -> dict[str, Any]:
    """The declaration as data (``piceli.cluster.v1``): no credentials, no paths."""
    body: dict[str, Any] = {
        "schema": "piceli.cluster.v1",
        "name": cluster.name,
        "api": cluster.api,
        "credentials": cluster.credentials,
        "nodes": [
            {
                "name": node.name,
                "arch": node.arch,
                "roles": list(node.roles),
                # 0.22.0, only when declared (same hashes); never the SSH user.
                **(
                    {"target": node_target_identity(node.target)}
                    if node.target is not None
                    else {}
                ),
            }
            for node in cluster.nodes
        ],
        "storage_class": cluster.storage_class,
        "registry": cluster.registry.describe() if cluster.registry else None,
        "controller": None,
        "ui": None,
    }
    if cluster.controller is not None:
        from piceli.gitops.config import parse_duration

        body["controller"] = {
            "on": cluster.controller.on,
            "poll_seconds": parse_duration(cluster.controller.poll),
            "sync": cluster.controller.sync,
            "image": cluster.controller.image,
        }
        if cluster.controller.delete_volumes:  # absent when off: same hashes
            body["controller"]["delete_volumes"] = True
        if cluster.controller.telemetry is not None:  # absent when off: same hashes
            body["controller"]["telemetry"] = cluster.controller.telemetry.to_dict()
        if tuple(cluster.controller.builds) != ("cluster",):  # 0.21.0; same hashes
            body["controller"]["builds"] = list(cluster.controller.builds)
            body["controller"]["host_wait"] = cluster.controller.host_wait
        if cluster.controller.delivery != "registry":  # 0.22.0; same hashes
            body["controller"]["delivery"] = cluster.controller.delivery
        if cluster.controller.prebuilt:  # 0.26.0; same hashes
            body["controller"]["prebuilt"] = True
        if (
            cluster.controller.quarantine_after,
            cluster.controller.quarantine_window,
        ) != (2, "24h"):  # 0.22.0; same hashes
            body["controller"]["quarantine_after"] = cluster.controller.quarantine_after
            body["controller"]["quarantine_window"] = (
                cluster.controller.quarantine_window
            )
    if cluster.dev is not None:  # 0.18.0; absent when off: same hashes
        body["dev"] = cluster.dev.describe()
    if cluster.access is not None:  # 0.19.0; absent when off: same hashes
        body["access"] = cluster.access.describe()
    if cluster.ui is not None:
        body["ui"] = {
            "access": cluster.ui.access,
            "on": cluster.ui.on,
            "image": cluster.ui.image,
        }
    return body
