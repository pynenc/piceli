"""The in-cluster registry of ``Registry.in_cluster``: objects, node mirror, status.

One registry serves every node of a cluster:

- a one-replica registry **Deployment** pinned to one node (``on=``), its data
  on a retained ``ReadWriteOnce`` claim, listening on the pod network only;
- a **Service** with a stable ClusterIP (optionally a fixed NodePort for
  builders outside the cluster);
- a **node agent** (DaemonSet on every node, all taints tolerated) that writes
  ``<mirror_dir>/<host>/hosts.toml`` for the stable name
  ``<name>.<namespace>.svc:<port>``, pointing containerd at the Service's
  ClusterIP over plain HTTP, for that host only.

Nodes never use cluster DNS, so the name in an image reference only works
through that mirror; kube-proxy makes the ClusterIP reachable from every node.
Pods (cluster build Jobs, the GitOps controller) resolve the same name through
cluster DNS. The agent reads the ClusterIP from the Service environment
variable the kubelet injects (``<NAME>_SERVICE_HOST``): the Service is created
before the DaemonSet, and a container started before it exits and is
restarted with the variable. When the agent stops (uninstall, node drain) it
removes the file, so an uninstalled registry leaves nothing on the nodes.

Everything here is pure: :func:`render` returns manifests, :func:`summarize`
turns API reads into a status. Importing this module is side-effect free.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from piceli.k8s.templates.deployable.node_local_registry import DEFAULT_REGISTRY_IMAGE

if TYPE_CHECKING:
    from piceli.pipeline.model import ClusterRegistry

PLAN_SCHEMA = "piceli.cluster-registry-plan.v1"
STATUS_SCHEMA = "piceli.cluster-registry-status.v1"
FIELD_MANAGER = "piceli-registry"
STORAGE_DIR = "/var/lib/registry"
CONFIG_DIR = "/etc/distribution"
HOST_MIRROR_DIR = "/host/certs.d"
UID = 65532
#: ``hosts.toml`` for the stable host; ``${ADDR}`` is the Service address.
HOSTS_TOML = (
    'server = "http://${ADDR}:${PORT}"\n'
    "\n"
    '[host."http://${ADDR}:${PORT}"]\n'
    '  capabilities = ["pull", "resolve"]\n'
)


def labels(registry: ClusterRegistry, component: str) -> dict[str, str]:
    return {
        "app.kubernetes.io/managed-by": "piceli",
        "app.kubernetes.io/name": "piceli-cluster-registry",
        "app.kubernetes.io/instance": registry.name,
        "app.kubernetes.io/component": component,
    }


def selector(registry: ClusterRegistry, component: str) -> dict[str, str]:
    return {
        "app.kubernetes.io/name": "piceli-cluster-registry",
        "app.kubernetes.io/instance": registry.name,
        "app.kubernetes.io/component": component,
    }


def agent_name(registry: ClusterRegistry) -> str:
    return f"{registry.name}-mirror"


def claim_name(registry: ClusterRegistry) -> str:
    return f"{registry.name}-storage"


def config_name(registry: ClusterRegistry) -> str:
    return f"{registry.name}-config"


def service_env(registry: ClusterRegistry) -> str:
    """The variable the kubelet sets to the Service's ClusterIP in every pod."""
    return registry.name.upper().replace("-", "_") + "_SERVICE_HOST"


def hosts_toml(address: str, port: int) -> str:
    """The containerd mirror for the stable host (``address``: the ClusterIP)."""
    shown = f"[{address}]" if ":" in address else address
    return HOSTS_TOML.replace("${ADDR}", shown).replace("${PORT}", str(port))


def mirror_path(registry: ClusterRegistry) -> str:
    """Where the agent writes the mirror on each node."""
    return f"{registry.mirror_dir.rstrip('/')}/{registry.host}/hosts.toml"


def agent_script(registry: ClusterRegistry) -> str:
    """The node agent: write the mirror once, keep it, remove it on stop."""
    directory = f"{HOST_MIRROR_DIR}/{registry.host}"
    return f"""set -eu
addr="${{{service_env(registry)}:-}}"
if [ -z "$addr" ]; then
  echo "the Service address is not set yet; restarting" >&2
  exit 1
fi
case "$addr" in *:*) ADDR="[$addr]" ;; *) ADDR="$addr" ;; esac
PORT={registry.port}
dir='{directory}'
cleanup() {{ rm -f "$dir/hosts.toml" "$dir/hosts.toml.tmp"; rmdir "$dir" 2>/dev/null || true; exit 0; }}
trap cleanup TERM INT
mkdir -p "$dir"
cat > "$dir/hosts.toml.tmp" <<EOF
{HOSTS_TOML}EOF
mv "$dir/hosts.toml.tmp" "$dir/hosts.toml"
echo "mirror for {registry.host} -> $ADDR:$PORT written"
while true; do sleep 3600 & wait $!; done
"""


def registry_config(registry: ClusterRegistry) -> dict[str, Any]:
    """The complete registry configuration: no debug listener, deletes allowed."""
    return {
        "version": 0.1,
        "log": {"level": "info", "fields": {"service": "registry"}},
        "storage": {
            "filesystem": {"rootdirectory": STORAGE_DIR},
            "delete": {"enabled": True},
            "maintenance": {
                "uploadpurging": {
                    "enabled": True,
                    "age": "168h",
                    "interval": "24h",
                    "dryrun": False,
                }
            },
        },
        "http": {
            "addr": f":{registry.port}",
            "headers": {"X-Content-Type-Options": ["nosniff"]},
        },
        "health": {
            "storagedriver": {"enabled": True, "interval": "10s", "threshold": 3}
        },
    }


def _meta(
    registry: ClusterRegistry, name: str, component: str, **extra: Any
) -> dict[str, Any]:
    return {
        "name": name,
        "namespace": registry.namespace,
        "labels": labels(registry, component),
        **extra,
    }


def _probe(port: int, period: int, failures: int) -> dict[str, Any]:
    return {
        "httpGet": {"path": "/v2/", "port": port, "scheme": "HTTP"},
        "periodSeconds": period,
        "timeoutSeconds": 2,
        "failureThreshold": failures,
    }


def render(registry: ClusterRegistry) -> list[dict[str, Any]]:
    """The registry's objects, in apply order (the Service before the agent)."""
    image = registry.image or DEFAULT_REGISTRY_IMAGE
    config_text = json.dumps(registry_config(registry), sort_keys=True, indent=2)
    config_digest = hashlib.sha256(config_text.encode()).hexdigest()
    claim: dict[str, Any] = {
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": registry.storage}},
    }
    if registry.storage_class:
        claim["storageClassName"] = registry.storage_class
    service_port: dict[str, Any] = {
        "name": "registry",
        "port": registry.port,
        "targetPort": registry.port,
        "protocol": "TCP",
    }
    if registry.node_port is not None:
        service_port["nodePort"] = registry.node_port
    server = selector(registry, "registry")
    agent = selector(registry, "mirror")
    drop_all = {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": _meta(registry, registry.name, "registry"),
        "spec": {
            "replicas": 1,
            # The claim is ReadWriteOnce: the old pod lets it go first.
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": server},
            "template": {
                "metadata": {
                    "labels": labels(registry, "registry"),
                    "annotations": {"piceli.io/registry-config-sha256": config_digest},
                },
                "spec": {
                    "nodeSelector": {"kubernetes.io/hostname": registry.on},
                    # Pinned to one node by name: a control-plane node is a
                    # deliberate choice, so its taint is tolerated.
                    "tolerations": [
                        {
                            "key": "node-role.kubernetes.io/control-plane",
                            "operator": "Exists",
                            "effect": "NoSchedule",
                        }
                    ],
                    "automountServiceAccountToken": False,
                    "enableServiceLinks": False,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": UID,
                        "runAsGroup": UID,
                        "fsGroup": UID,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "registry",
                            "image": image,
                            "imagePullPolicy": "IfNotPresent",
                            "args": [f"{CONFIG_DIR}/config.yml"],
                            "ports": [
                                {
                                    "name": "registry",
                                    "containerPort": registry.port,
                                    "protocol": "TCP",
                                }
                            ],
                            "readinessProbe": _probe(registry.port, 5, 3),
                            "livenessProbe": _probe(registry.port, 20, 6),
                            "startupProbe": _probe(registry.port, 2, 60),
                            "resources": {
                                "requests": {"cpu": "10m", "memory": "32Mi"},
                                "limits": {"memory": "256Mi"},
                            },
                            "securityContext": drop_all,
                            "volumeMounts": [
                                {"name": "storage", "mountPath": STORAGE_DIR},
                                {
                                    "name": "config",
                                    "mountPath": CONFIG_DIR,
                                    "readOnly": True,
                                },
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "storage",
                            "persistentVolumeClaim": {
                                "claimName": claim_name(registry)
                            },
                        },
                        {
                            "name": "config",
                            "configMap": {
                                "name": config_name(registry),
                                "items": [{"key": "config.yml", "path": "config.yml"}],
                            },
                        },
                    ],
                },
            },
        },
    }
    mirror_file = f"{HOST_MIRROR_DIR}/{registry.host}/hosts.toml"
    daemon_set = {
        "apiVersion": "apps/v1",
        "kind": "DaemonSet",
        "metadata": _meta(registry, agent_name(registry), "mirror"),
        "spec": {
            "selector": {"matchLabels": agent},
            "updateStrategy": {
                "type": "RollingUpdate",
                "rollingUpdate": {"maxUnavailable": 1},
            },
            "template": {
                "metadata": {"labels": labels(registry, "mirror")},
                "spec": {
                    # Every node pulls, so every node gets the mirror.
                    "tolerations": [{"operator": "Exists"}],
                    "automountServiceAccountToken": False,
                    # The kubelet's Service variables carry the ClusterIP.
                    "enableServiceLinks": True,
                    "terminationGracePeriodSeconds": 10,
                    "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
                    "containers": [
                        {
                            "name": "mirror",
                            "image": image,
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["/bin/sh", "-c", agent_script(registry)],
                            "readinessProbe": {
                                "exec": {"command": ["test", "-s", mirror_file]},
                                "periodSeconds": 10,
                            },
                            "resources": {
                                "requests": {"cpu": "1m", "memory": "8Mi"},
                                "limits": {"memory": "32Mi"},
                            },
                            # root (the containerd directory is root's) without
                            # any capability: it writes only its own file.
                            "securityContext": {
                                "runAsUser": 0,
                                "runAsGroup": 0,
                                "runAsNonRoot": False,
                                **drop_all,
                            },
                            "volumeMounts": [
                                {"name": "certs", "mountPath": HOST_MIRROR_DIR}
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "certs",
                            "hostPath": {
                                "path": registry.mirror_dir.rstrip("/"),
                                "type": "DirectoryOrCreate",
                            },
                        }
                    ],
                },
            },
        },
    }
    return [
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": registry.namespace,
                "labels": {"app.kubernetes.io/managed-by": "piceli"},
            },
        },
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": _meta(registry, config_name(registry), "registry"),
            "data": {"config.yml": config_text},
        },
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": _meta(
                registry,
                claim_name(registry),
                "registry",
                annotations={"piceli.io/retained": "true"},
            ),
            "spec": claim,
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": _meta(registry, registry.name, "registry"),
            "spec": {
                "type": "NodePort" if registry.node_port is not None else "ClusterIP",
                "selector": server,
                "ports": [service_port],
            },
        },
        deployment,
        daemon_set,
    ]


def removable(
    objects: Sequence[Mapping[str, Any]], *, delete_storage: bool
) -> list[Mapping[str, Any]]:
    """What ``uninstall`` deletes: never the namespace; the data only on request."""
    return [
        item
        for item in objects
        if item["kind"] != "Namespace"
        and (delete_storage or item["kind"] != "PersistentVolumeClaim")
    ]


def _ready(pod: Mapping[str, Any]) -> bool:
    for condition in (pod.get("status") or {}).get("conditions") or ():
        if condition.get("type") == "Ready":
            return condition.get("status") == "True"
    return False


def summarize(
    registry: ClusterRegistry,
    *,
    deployment: Mapping[str, Any] | None,
    service: Mapping[str, Any] | None,
    claim: Mapping[str, Any] | None,
    nodes: Sequence[Mapping[str, Any]],
    pods: Sequence[Mapping[str, Any]],
    used_bytes: int | None = None,
) -> dict[str, Any]:
    """The status document of ``piceli registry status`` (pure; tested offline)."""
    installed = deployment is not None
    registry_pods = [
        pod
        for pod in pods
        if (pod.get("metadata") or {})
        .get("labels", {})
        .get("app.kubernetes.io/component")
        == "registry"
    ]
    agents = {
        (pod.get("spec") or {}).get("nodeName"): pod
        for pod in pods
        if (pod.get("metadata") or {})
        .get("labels", {})
        .get("app.kubernetes.io/component")
        == "mirror"
    }
    mirrors = []
    for node in nodes:
        name = (node.get("metadata") or {}).get("name")
        pod = agents.get(name)
        mirrors.append(
            {
                "node": name,
                "mirror": "ready"
                if pod is not None and _ready(pod)
                else ("pending" if pod is not None else "missing"),
            }
        )
    ready = bool(((deployment or {}).get("status") or {}).get("readyReplicas"))
    mirrors_ready = bool(mirrors) and all(m["mirror"] == "ready" for m in mirrors)
    spec = (service or {}).get("spec") or {}
    node_port = next(
        (p.get("nodePort") for p in spec.get("ports") or () if p.get("nodePort")),
        None,
    )
    claim_status = (claim or {}).get("status") or {}
    if not installed:
        state = "not-installed"
    elif ready and mirrors_ready:
        state = "ready"
    else:
        state = "degraded"
    return {
        "schema": STATUS_SCHEMA,
        "state": state,
        "host": registry.host,
        "namespace": registry.namespace,
        "name": registry.name,
        "node": registry.on,
        "registry": {
            "ready": ready,
            "pods": [
                {
                    "name": (pod.get("metadata") or {}).get("name"),
                    "node": (pod.get("spec") or {}).get("nodeName"),
                    "phase": (pod.get("status") or {}).get("phase"),
                    "ready": _ready(pod),
                }
                for pod in registry_pods
            ],
        },
        "service": {
            "cluster_ip": spec.get("clusterIP"),
            "node_port": node_port,
        }
        if service is not None
        else None,
        "mirrors": mirrors,
        "mirror_path": mirror_path(registry),
        "storage": {
            "claim": claim_name(registry),
            "phase": claim_status.get("phase"),
            "capacity": (claim_status.get("capacity") or {}).get("storage"),
            "requested": registry.storage,
            "used_bytes": used_bytes,
        }
        if claim is not None
        else None,
    }


def used_bytes(summary: Mapping[str, Any], namespace: str, claim: str) -> int | None:
    """The claim's used bytes from a kubelet ``stats/summary`` document."""
    for pod in summary.get("pods") or ():
        if (pod.get("podRef") or {}).get("namespace") != namespace:
            continue
        for volume in pod.get("volume") or ():
            ref = volume.get("pvcRef") or {}
            if ref.get("name") == claim and isinstance(volume.get("usedBytes"), int):
                return int(volume["usedBytes"])
    return None
