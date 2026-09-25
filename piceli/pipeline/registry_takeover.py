"""Decide how the node-loopback registry release meets a live registry.

``piceli deploy`` releases its registry as its own release. A registry that
already runs on the node (created with ``kubectl``, or by another owner)
holds the loopback port, so a second one could never start. This module reads
the namespace's Deployments (read-only, during planning) and decides:

* **nothing live**: the registry release creates its objects;
* **the registry release's own Deployment**: a normal release; the live
  Deployment selector is kept (Kubernetes never changes it);
* ``NodeLoopbackRegistry(adopt="NAME")``: the release **adopts** the live
  Deployment ``NAME`` (and its ``NAME-config`` ConfigMap and ``NAME-storage``
  claim, when they exist) by ownership transfer, keeping its selector and,
  for a ``RollingUpdate`` Deployment, a one-pod-at-a-time rolling update
  (Kubernetes refuses the switch to ``Recreate``). It must be compatible: a
  ``hostNetwork`` registry on the same port and node whose data lives on the
  declared storage (``host_path=``, ``existing_claim=`` or ``NAME-storage``);
  otherwise the plan is refused with ``pipeline-registry-incompatible``;
* ``NodeLoopbackRegistry(replace="NAME")``: the live Deployment is replaced
  (backed up, deleted and recreated by the release engine); its port, selector
  and storage may differ. Storage (host directory or claim) is never deleted;
  whether its data carries over is shown in the plan;
* another live Deployment that holds the port on the node, or a live
  Deployment with the registry's name that another owner manages, without
  ``adopt``/``replace``: refused with ``pipeline-registry-takeover-required``
  (it names the Deployment and the flag).

Importing this module is side-effect free.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from piceli.pipeline.compose import RegistryTakeover
from piceli.pipeline.errors import PipelineError
from piceli.pipeline.model import NodeLoopbackRegistry

OWNER_ANNOTATION = "piceli.io/owner"
HOSTNAME_LABEL = "kubernetes.io/hostname"
STORAGE_DIR = "/var/lib/registry"
_ADDR_ENV = "REGISTRY_HTTP_ADDR"
_ROOT_ENV = "REGISTRY_STORAGE_FILESYSTEM_ROOTDIRECTORY"


@dataclass(frozen=True)
class LiveRegistry:
    """What planning learned about a live registry Deployment (public, path-free
    except the declared host directory)."""

    name: str
    managed: bool
    host_network: bool
    ports: tuple[int, ...]
    node: str | None
    storage: dict[str, str] | None
    selector: dict[str, str] | None
    strategy: str = "RollingUpdate"
    owner: str | None = field(default=None, repr=False)

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "managed": self.managed,
            "host_network": self.host_network,
            "ports": list(self.ports),
            "node": self.node,
            "storage": self.storage,
        }


@dataclass(frozen=True)
class Takeover:
    """The decision: release inputs plus what the plan shows."""

    takeover: RegistryTakeover
    existing: dict[str, Any] | None


def _pod(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    spec = manifest.get("spec") or {}
    template = spec.get("template") or {}
    return template.get("spec") or {}


def _containers(pod: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [item for item in pod.get("containers") or () if isinstance(item, Mapping)]


def _ports(pod: Mapping[str, Any]) -> tuple[int, ...]:
    found: set[int] = set()
    for container in _containers(pod):
        for port in container.get("ports") or ():
            if not isinstance(port, Mapping):
                continue
            for key in ("hostPort", "containerPort"):
                value = port.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    if key == "containerPort" and not pod.get("hostNetwork"):
                        continue
                    found.add(value)
        for env in container.get("env") or ():
            if isinstance(env, Mapping) and env.get("name") == _ADDR_ENV:
                tail = str(env.get("value") or "").rpartition(":")[2]
                if tail.isdigit():
                    found.add(int(tail))
    return tuple(sorted(found))


def _node(pod: Mapping[str, Any]) -> str | None:
    selector = pod.get("nodeSelector") or {}
    if isinstance(selector, Mapping) and isinstance(selector.get(HOSTNAME_LABEL), str):
        return str(selector[HOSTNAME_LABEL])
    name = pod.get("nodeName")
    return str(name) if isinstance(name, str) and name else None


def _storage(pod: Mapping[str, Any]) -> dict[str, str] | None:
    """The volume the registry keeps its data on: a host directory or a claim."""
    volumes = {
        str(item.get("name")): item
        for item in pod.get("volumes") or ()
        if isinstance(item, Mapping)
    }
    for container in _containers(pod):
        root = STORAGE_DIR
        for env in container.get("env") or ():
            if isinstance(env, Mapping) and env.get("name") == _ROOT_ENV:
                root = str(env.get("value") or STORAGE_DIR)
        for mount in container.get("volumeMounts") or ():
            if not isinstance(mount, Mapping) or mount.get("mountPath") != root:
                continue
            volume = volumes.get(str(mount.get("name")))
            if volume is None:
                return None
            host = volume.get("hostPath")
            if isinstance(host, Mapping) and isinstance(host.get("path"), str):
                return {"host_path": str(host["path"])}
            claim = volume.get("persistentVolumeClaim")
            if isinstance(claim, Mapping) and isinstance(claim.get("claimName"), str):
                return {"claim": str(claim["claimName"])}
            return None
    return None


def _selector(manifest: Mapping[str, Any]) -> dict[str, str] | None:
    selector = (manifest.get("spec") or {}).get("selector") or {}
    if not isinstance(selector, Mapping) or selector.get("matchExpressions"):
        return None
    labels = selector.get("matchLabels")
    if not isinstance(labels, Mapping) or not labels:
        return None
    return {str(key): str(value) for key, value in labels.items()}


def live_registry(manifest: Mapping[str, Any], owners: Sequence[str]) -> LiveRegistry:
    """Summarize a live Deployment as a registry."""
    metadata = manifest.get("metadata") or {}
    owner = (metadata.get("annotations") or {}).get(OWNER_ANNOTATION)
    pod = _pod(manifest)
    return LiveRegistry(
        name=str(metadata.get("name")),
        managed=isinstance(owner, str) and owner in set(owners),
        host_network=bool(pod.get("hostNetwork")),
        ports=_ports(pod),
        node=_node(pod),
        storage=_storage(pod),
        selector=_selector(manifest),
        strategy=str(
            ((manifest.get("spec") or {}).get("strategy") or {}).get("type")
            or "RollingUpdate"
        ),
        owner=owner if isinstance(owner, str) else None,
    )


def declared_storage(strategy: NodeLoopbackRegistry) -> dict[str, str]:
    if strategy.host_path is not None:
        return {"host_path": strategy.host_path}
    if strategy.existing_claim is not None:
        return {"claim": strategy.existing_claim}
    return {"claim": f"{strategy.registry_name}-storage"}


def _holds_port(manifest: Mapping[str, Any], port: int, node: str) -> bool:
    replicas = (manifest.get("spec") or {}).get("replicas", 1)
    if replicas == 0:
        return False
    pod = _pod(manifest)
    placed = _node(pod)
    if placed is not None and placed != node:
        return False
    return port in _ports(pod)


def decide(
    strategy: NodeLoopbackRegistry,
    *,
    node: str,
    owner: str,
    deployments: Sequence[Mapping[str, Any]],
) -> Takeover:
    """How the registry release meets the live Deployments of the namespace.

    :param node: The node name the registry runs on.
    :param owner: The registry release's owner.
    :param deployments: Live Deployment manifests of the target namespace.
    :raises PipelineError: ``pipeline-registry-takeover-required`` or
        ``pipeline-registry-incompatible``.
    """
    name = strategy.registry_name
    owners = (owner, *strategy.inherited_owners)
    by_name = {
        str((item.get("metadata") or {}).get("name")): item for item in deployments
    }
    others = sorted(
        other
        for other, manifest in by_name.items()
        if other != name and _holds_port(manifest, strategy.port, node)
    )
    if others:
        hint = others[0]
        raise PipelineError(
            "pipeline-registry-takeover-required",
            f"Deployment/{hint} already holds port {strategy.port} on node {node}, "
            "so the node-loopback registry could never start; take it over with "
            f"NodeLoopbackRegistry(port={strategy.port}, adopt={hint!r}) (or "
            f"replace={hint!r}), or choose another port=",
        )
    configmap = f"ConfigMap/{name}-config"
    claim = (
        [f"PersistentVolumeClaim/{name}-storage"]
        if strategy.host_path is None and strategy.existing_claim is None
        else []
    )
    current = by_name.get(name)
    if current is None:
        # Standing adopt/replace flags are harmless once there is nothing live.
        adopt = (f"Deployment/{name}", configmap, *claim) if strategy.adopt else ()
        return Takeover(RegistryTakeover(adopt=tuple(adopt)), None)
    live = live_registry(current, owners)
    wanted = declared_storage(strategy)
    existing: dict[str, Any] = live.summary()
    selector = live.selector
    if selector is None:
        raise PipelineError(
            "pipeline-registry-incompatible",
            f"Deployment/{name} selects its pods with match expressions; the "
            "registry release can neither keep nor change that selector",
        )
    # Kubernetes keeps a Deployment's selector forever and refuses to switch a
    # rolling update another manager configured to Recreate: keep both.
    rolling = live.strategy != "Recreate"
    if live.managed:
        existing.update(action="managed", data="kept")
        return Takeover(
            RegistryTakeover(selector=selector, rolling_update=rolling), existing
        )
    if strategy.adopt is None and strategy.replace is None:
        raise PipelineError(
            "pipeline-registry-takeover-required",
            f"Deployment/{name} exists and is not managed by the registry release; "
            f"take it over with NodeLoopbackRegistry(adopt={name!r}) (or "
            f"replace={name!r}), or choose another name=",
        )
    problems = []
    if not live.host_network:
        problems.append("it does not use the host network")
    if strategy.port not in live.ports:
        problems.append(
            f"it listens on port(s) {list(live.ports) or 'unknown'}, not {strategy.port}"
        )
    if live.node is not None and live.node != node:
        problems.append(f"it runs on node {live.node}, not {node}")
    if live.storage != wanted:
        problems.append(
            f"its data is on {_shown(live.storage)}, but the registry declares "
            f"{_shown(wanted)}"
        )
    if strategy.adopt is not None:
        if problems:
            raise PipelineError(
                "pipeline-registry-incompatible",
                f"Deployment/{name} cannot be adopted: "
                + "; ".join(problems)
                + f". Declare matching settings, or use replace={name!r}",
            )
        existing.update(action="adopt", data="kept")
        return Takeover(
            RegistryTakeover(
                adopt=(f"Deployment/{name}", configmap, *claim),
                selector=selector,
                rolling_update=rolling,
            ),
            existing,
        )
    existing.update(
        action="replace",
        data="kept" if live.storage == wanted else "not-carried-over",
        differences=problems,
    )
    return Takeover(
        RegistryTakeover(adopt=(configmap, *claim), replace=(f"Deployment/{name}",)),
        existing,
    )


def _shown(storage: Mapping[str, str] | None) -> str:
    if not storage:
        return "no persistent volume"
    if "host_path" in storage:
        return f"host directory {storage['host_path']}"
    return f"claim {storage['claim']}"
