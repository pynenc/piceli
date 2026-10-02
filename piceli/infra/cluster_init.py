"""``piceli cluster init``: set a declared :class:`~piceli.infra.Cluster` up.

One plan (with a hash) covers everything the cluster needs before any
environment is deployed:

- **node labels**: each declared node gets ``piceli.io/role-<role>=true``
  per role (``piceli.io/builder=true`` for ``builder``) and, when the
  registry picks its node agent per node (``node_mirror="auto"``),
  ``piceli.io/runtime=k3s|containerd`` from the node's own runtime version.
  The annotation ``piceli.io/managed-labels`` lists what init set, so a
  re-run removes a label only when init set it and the role is gone;
- the **in-cluster registry** (``Registry.in_cluster``) with its node
  mirrors (containerd ``certs.d`` or k3s ``registries.yaml``);
- the **GitOps controller's foundation** in ``piceli-system`` (namespace,
  service account, RBAC, state claim): ``piceli gitops enable`` adds its
  configuration and the Deployment, pinned to ``Controller(on=)``;
- the **UI** (``Ui(access="forward")``), when this build provides its
  renderer (:func:`ui_renderer`): no exposed Service, reached through
  ``piceli access ui``;
- the ConfigMap ``piceli-cluster`` with the declaration (no credentials),
  which ``gitops enable`` and the UI read.

Nothing restarts k3s. A k3s node whose containerd does not read ``certs.d``
loads the mirror only when k3s restarts; init and ``cluster status`` list
those nodes (``restart: needed``).

Importing this module is side-effect free.
"""

from __future__ import annotations

import hashlib
import importlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from piceli.infra.cluster import MANAGED_LABELS, ClusterError

if TYPE_CHECKING:
    from piceli.gitops.install import Api, InstallPlan
    from piceli.infra import Cluster

PLAN_SCHEMA = "piceli.cluster-init-plan.v1"
STATUS_SCHEMA = "piceli.cluster-status.v1"
NAMESPACE = "piceli-system"
CLUSTER_CONFIG = "piceli-cluster"
FIELD_MANAGER = "piceli-cluster"
#: The one Secret with the Git credentials (``username``, ``password``) the
#: controller mounts and cluster build Jobs clone with (``piceli secrets git``).
GIT_SECRET = "piceli-build-git"
MANAGED = {"app.kubernetes.io/managed-by": "piceli", "piceli.io/component": "cluster"}

UiRenderer = Callable[["Cluster"], list[dict[str, Any]]]


def ui_renderer() -> UiRenderer | None:
    """The UI's ``render_ui(cluster)`` when this build has it (else ``None``)."""
    try:
        module = importlib.import_module("piceli.infra.ui_install")
    except ImportError:
        return None
    render = getattr(module, "render_ui", None)
    return render if callable(render) else None


def node_runtime(node: Mapping[str, Any]) -> str | None:
    """``k3s`` or ``containerd`` from a Node's ``status.nodeInfo`` (else ``None``)."""
    info = (node.get("status") or {}).get("nodeInfo") or {}
    kubelet = str(info.get("kubeletVersion") or "")
    runtime = str(info.get("containerRuntimeVersion") or "")
    if "+k3s" in kubelet or "k3s" in runtime:
        return "k3s"
    if runtime.startswith("containerd://"):
        return "containerd"
    return None


def _meta(node: Mapping[str, Any]) -> Mapping[str, Any]:
    return node.get("metadata") or {}


@dataclass(frozen=True)
class NodeChange:
    """The label change of one node (``label`` or ``no-op``)."""

    node: str
    uid: str | None
    set: Mapping[str, str]
    remove: tuple[str, ...]
    managed: tuple[str, ...]

    @property
    def operation(self) -> str:
        return "label" if self.set or self.remove or self.annotate else "no-op"

    annotate: bool = False

    def describe(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "kind": "Node",
            "name": self.node,
            "set": dict(sorted(self.set.items())),
            "remove": list(self.remove),
        }


def plan_nodes(cluster: Cluster, live: Sequence[Mapping[str, Any]]) -> list[NodeChange]:
    """The label changes of every declared node; refuses missing or wrong nodes."""
    from piceli.artifacts.cluster_registry import RUNTIME_LABEL

    by_name = {str(_meta(node).get("name")): node for node in live}
    auto = (
        cluster.registry is not None
        and getattr(cluster.registry, "node_mirror", None) == "auto"
    )
    changes: list[NodeChange] = []
    for declared in cluster.nodes:
        node = by_name.get(declared.name)
        if node is None:
            raise ClusterError(
                "cluster-node-missing",
                f"node {declared.name!r} is declared but not in the cluster",
            )
        arch = ((node.get("status") or {}).get("nodeInfo") or {}).get("architecture")
        if arch and arch != declared.arch:
            raise ClusterError(
                "cluster-node-arch-mismatch",
                f"node {declared.name!r} is {arch}, declared {declared.arch}",
            )
        desired = dict(declared.labels)
        runtime = node_runtime(node)
        if auto and runtime is not None:
            desired[RUNTIME_LABEL] = runtime
        meta = _meta(node)
        labels = meta.get("labels") or {}
        before = [
            item
            for item in str(
                (meta.get("annotations") or {}).get(MANAGED_LABELS, "")
            ).split(",")
            if item
        ]
        managed = tuple(sorted(desired))
        changes.append(
            NodeChange(
                node=declared.name,
                uid=meta.get("uid"),
                set={k: v for k, v in desired.items() if labels.get(k) != v},
                remove=tuple(
                    k for k in sorted(before) if k not in desired and k in labels
                ),
                managed=managed,
                annotate=tuple(sorted(before)) != managed,
            )
        )
    return changes


def cluster_config(cluster: Cluster) -> dict[str, Any]:
    """The ConfigMap ``piceli-cluster``: the declaration for the controller and UI."""
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": CLUSTER_CONFIG,
            "namespace": NAMESPACE,
            "labels": dict(MANAGED),
        },
        "data": {
            "cluster.json": json.dumps(cluster.describe(), sort_keys=True, indent=2)
        },
    }


def render_objects(
    cluster: Cluster, ui: UiRenderer | None, *, cluster_rbac: bool = False
) -> list[dict[str, Any]]:
    """Every object init installs, in apply order (each one once).

    ``cluster_rbac`` keeps the rule ``gitops enable --cluster-rbac`` added
    to the controller's ClusterRole (:func:`plan_init` reads it live).
    """
    from piceli.artifacts.cluster_registry import render as render_registry
    from piceli.gitops.install import render_foundation
    from piceli.pipeline.model import ClusterRegistry

    objects: list[dict[str, Any]] = [
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": NAMESPACE,
                "labels": {"app.kubernetes.io/managed-by": "piceli"},
            },
        },
        cluster_config(cluster),
    ]
    if isinstance(cluster.registry, ClusterRegistry):
        objects += render_registry(cluster.registry)
    if cluster.controller is not None:
        objects += render_foundation(
            NAMESPACE, storage_class=cluster.storage_class, cluster_rbac=cluster_rbac
        )
    if cluster.ui is not None and ui is not None:
        objects += ui(cluster)
    seen: set[tuple[str, str | None, str]] = set()
    unique: list[dict[str, Any]] = []
    for item in objects:
        meta = item["metadata"]
        key = (item["kind"], meta.get("namespace"), meta["name"])
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def ui_state(cluster: Cluster, ui: UiRenderer | None) -> str:
    if cluster.ui is None:
        return "not-declared"
    return "included" if ui is not None else "unavailable"


@dataclass(frozen=True)
class InitPlan:
    """What ``cluster init`` would do, and the hash that approves it."""

    cluster: Mapping[str, Any]
    nodes: tuple[NodeChange, ...]
    objects: InstallPlan
    ui: str
    mirrors: Sequence[Mapping[str, Any]] = field(default_factory=tuple)

    @property
    def plan_hash(self) -> str:
        body = {
            "schema": PLAN_SCHEMA,
            "cluster": self.cluster,
            "nodes": [
                {
                    **change.describe(),
                    "uid": change.uid,
                    "managed": list(change.managed),
                }
                for change in self.nodes
            ],
            "objects": self.objects.plan_hash,
        }
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        return "sha256:" + hashlib.sha256(canonical).hexdigest()

    @property
    def changes(self) -> bool:
        return self.objects.changes or any(n.operation != "no-op" for n in self.nodes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": PLAN_SCHEMA,
            "cluster": self.cluster["name"],
            "plan_hash": self.plan_hash,
            "cluster_uid": self.objects.cluster_uid,
            "nodes": [change.describe() for change in self.nodes],
            "changes": [item.describe() for item in self.objects.objects],
            "ui": self.ui,
        }


def list_items(api: Api, path: str) -> list[dict[str, Any]]:
    found = api.call(path, "GET")
    items = (found or {}).get("items") if isinstance(found, dict) else None
    return [item for item in items or () if isinstance(item, dict)]


def plan_init(api: Api, cluster: Cluster, ui: UiRenderer | None) -> InitPlan:
    """Compare the declaration with the live cluster (reads only)."""
    from piceli.gitops.install import (
        _RBAC,
        NAME,
        has_cluster_rbac,
        object_path,
        plan_objects,
    )

    nodes = plan_nodes(cluster, list_items(api, "/api/v1/nodes"))
    described = cluster.describe()
    # `gitops enable --cluster-rbac` widened the controller's ClusterRole:
    # init keeps the rule rather than taking the owner's opt-in back.
    role = {
        "apiVersion": f"{_RBAC}/v1",
        "kind": "ClusterRole",
        "metadata": {"name": NAME},
    }
    live = api.call(object_path(role), "GET") if cluster.controller else None
    objects = plan_objects(
        api,
        render_objects(
            cluster,
            ui,
            cluster_rbac=has_cluster_rbac(live if isinstance(live, dict) else None),
        ),
        action="enable",
        config=described,
        schema=PLAN_SCHEMA,
    )
    return InitPlan(described, tuple(nodes), objects, ui_state(cluster, ui))


def execute_init(
    api: Api, plan: InitPlan, log: Callable[[str], None] = lambda _: None
) -> list[dict[str, Any]]:
    """Label the nodes, then apply the objects (the caller checked the hash)."""
    from piceli.gitops import GitOpsError
    from piceli.gitops.install import execute

    done: list[dict[str, Any]] = []
    for change in plan.nodes:
        if change.operation == "no-op":
            continue
        labels: dict[str, str | None] = dict(change.set)
        labels.update(dict.fromkeys(change.remove))
        metadata: dict[str, Any] = {
            "labels": labels,
            "annotations": {MANAGED_LABELS: ",".join(change.managed)},
        }
        if change.uid:
            metadata["uid"] = change.uid  # a replaced node refuses the patch
        body = {"metadata": metadata}
        try:
            api.call(
                f"/api/v1/nodes/{change.node}",
                "PATCH",
                body,
                "application/merge-patch+json",
                missing_ok=False,
            )
        except GitOpsError:
            raise ClusterError(
                "cluster-plan-changed",
                f"node {change.node!r} changed since the plan; plan again",
            ) from None
        done.append(change.describe())
        log(f"  label Node/{change.node}")
    try:
        done += execute(api, plan.objects, log)
    except GitOpsError as error:
        if error.code == "gitops-plan-changed":
            raise ClusterError("cluster-plan-changed", str(error)) from None
        raise
    return done


def restart_needed(mirrors: Sequence[Mapping[str, Any]]) -> list[str]:
    """The nodes whose k3s must restart to load the registry mirror."""
    return [str(m["node"]) for m in mirrors if m.get("restart") == "needed"]


def unmergeable(mirrors: Sequence[Mapping[str, Any]]) -> list[str]:
    """The k3s nodes whose ``registries.yaml`` the agent could not edit."""
    return [str(m["node"]) for m in mirrors if m.get("file") == "unmergeable"]


def summarize(
    cluster: Cluster,
    *,
    config: Mapping[str, Any] | None,
    nodes: Sequence[Mapping[str, Any]],
    registry: Mapping[str, Any] | None,
    foundation: Mapping[str, bool],
    controller: Mapping[str, Any] | None,
    ui: str,
    ui_present: bool | None,
    secret: Mapping[str, Any] | None,
    controller_health: Mapping[str, Any] | None = None,
    ui_health: str | None = None,
) -> dict[str, Any]:
    """The document of ``piceli cluster status`` (pure; tested offline).

    Shape (``piceli.cluster-status.v1``, read by the UI): ``state``,
    ``cluster``, ``nodes[]`` (``name``, ``arch``, ``roles``, ``ready``,
    ``mirror {kind, state, restart?, file?}``, labels), ``registry`` (the
    ``registry status`` document), ``controller {health, last_poll, …}``,
    ``ui {health, …}`` and ``git_secret`` (key names only).
    """
    from piceli.artifacts.cluster_registry import RUNTIME_LABEL

    by_name = {str(_meta(node).get("name")): node for node in nodes}
    auto = (
        cluster.registry is not None
        and getattr(cluster.registry, "node_mirror", None) == "auto"
    )
    mirror_rows = {str(m.get("node")): m for m in (registry or {}).get("mirrors") or ()}
    node_rows = []
    for declared in cluster.nodes:
        live = by_name.get(declared.name)
        row: dict[str, Any] = {
            "name": declared.name,
            "present": live is not None,
            "arch": declared.arch,
            "roles": list(declared.roles),
            "ready": None,
            "mirror": None,
        }
        if live is not None:
            labels = _meta(live).get("labels") or {}
            info = (live.get("status") or {}).get("nodeInfo") or {}
            wanted = dict(declared.labels)
            runtime = node_runtime(live)
            if auto and runtime is not None:
                wanted[RUNTIME_LABEL] = runtime
            ready = next(
                (
                    c.get("status") == "True"
                    for c in (live.get("status") or {}).get("conditions") or ()
                    if c.get("type") == "Ready"
                ),
                None,
            )
            row.update(
                {
                    "arch": info.get("architecture"),
                    "arch_ok": info.get("architecture") in (None, declared.arch),
                    "runtime": runtime,
                    "ready": ready,
                    "labels_ok": all(labels.get(k) == v for k, v in wanted.items()),
                    "missing_labels": sorted(
                        k for k, v in wanted.items() if labels.get(k) != v
                    ),
                }
            )
        if registry is not None:
            found = mirror_rows.get(declared.name) or {}
            mirror: dict[str, Any] = {
                "kind": found.get("runtime") or row.get("runtime"),
                "state": found.get("mirror", "missing"),
            }
            for key in ("restart", "file"):
                if key in found:
                    mirror[key] = found[key]
            row["mirror"] = mirror
        node_rows.append(row)
    mirrors = list((registry or {}).get("mirrors") or ())
    problems: list[str] = []
    if config is None:
        problems.append("not-initialized")
    for row in node_rows:
        if not row["present"]:
            problems.append(f"node-missing:{row['name']}")
        elif not (row["arch_ok"] and row["labels_ok"]):
            problems.append(f"node-labels:{row['name']}")
    if cluster.registry is not None and (registry or {}).get("state") != "ready":
        problems.append("registry")
    if restart_needed(mirrors):
        problems.append("k3s-restart-needed")
    if cluster.controller is not None and not all(foundation.values()):
        problems.append("controller-foundation")
    if ui == "included" and not ui_present:
        problems.append("ui")
    state = (
        "not-initialized"
        if config is None
        else ("ready" if not problems else "degraded")
    )
    return {
        "schema": STATUS_SCHEMA,
        "cluster": cluster.name,
        "api": cluster.api,
        "state": state,
        "problems": problems,
        "nodes": node_rows,
        "registry": registry,
        "restart_needed": restart_needed(mirrors),
        "unmergeable": unmergeable(mirrors),
        "controller": {
            "on": cluster.controller.on,
            "health": (controller_health or {}).get("health", "not-enabled"),
            "last_poll": (controller_health or {}).get("last_poll"),
            "foundation": dict(foundation),
            "deployment": controller,
        }
        if cluster.controller is not None
        else None,
        "ui": {
            "state": ui,
            "installed": ui_present,
            "health": ui_health
            or ("not-declared" if ui == "not-declared" else "not-installed"),
        },
        "git_secret": secret,
    }
