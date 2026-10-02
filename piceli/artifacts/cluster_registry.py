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

On k3s nodes (``node_mirror="k3s"``, or ``"auto"`` for nodes labelled
``node.kubernetes.io/instance-type=k3s`` or ``piceli.io/runtime=k3s``) a
second agent merges the same mirror into ``/etc/rancher/k3s/registries.yaml``
(between marker comments, never touching other entries) and writes
``hosts.toml`` into k3s's own ``certs.d``: the file takes effect at once on
k3s versions whose containerd reads ``certs.d``, and ``registries.yaml``
keeps it across k3s restarts. Each agent prints one report line
(``piceli-mirror: {...}``) that ``registry status`` reads from its log.

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
import re
from collections.abc import Callable, Mapping, Sequence
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
#: Where the k3s agent mounts ``/etc/rancher/k3s`` and k3s's containerd dir.
K3S_ETC_MOUNT = "/host/k3s-etc"
K3S_CONTAINERD_MOUNT = "/host/k3s-containerd"
K3S_ETC = "/etc/rancher/k3s"
K3S_CONTAINERD = "/var/lib/rancher/k3s/agent/etc/containerd"
#: The k3s agent's scratch directory (an emptyDir): staged files, readiness.
AGENT_STATE_DIR = "/run/piceli-mirror"
#: Node labels that pick the agent in ``node_mirror="auto"``.
RUNTIME_LABEL = "piceli.io/runtime"
INSTANCE_TYPE_LABEL = "node.kubernetes.io/instance-type"
#: The component label of the k3s agent's pods (the containerd agent's: ``mirror``).
K3S_COMPONENT = "mirror-k3s"
MIRROR_COMPONENTS = ("mirror", K3S_COMPONENT)
#: The prefix of the line each agent prints after writing its mirror.
REPORT_PREFIX = "piceli-mirror: "
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
echo '{REPORT_PREFIX}{{"runtime":"containerd","file":"written","restart":"not-needed"}}'
while true; do sleep 3600 & wait $!; done
"""


def k3s_agent_name(registry: ClusterRegistry) -> str:
    return f"{registry.name}-mirror-k3s"


def registries_marks(registry: ClusterRegistry) -> tuple[str, str]:
    """The comments around the agent's entry in ``registries.yaml``."""
    return f"# piceli:begin {registry.host}", f"# piceli:end {registry.host}"


#: First line of a ``registries.yaml`` the agent created (removed with it).
CREATED_MARK = "# piceli: created by the in-cluster registry node agent"

#: Drop the agent's block (between the marks) from a ``registries.yaml``.
_STRIP_AWK = r"""
{ t = $0; sub(/^[ \t]+/, "", t); sub(/[ \t]+$/, "", t) }
t == b { skip = 1; next }
t == e { skip = 0; next }
!skip { print }
"""

#: Insert the block under ``mirrors:`` (block style only), with the
#: indentation of its existing entries; exit 3 when the file is not plain
#: block-style YAML the agent can edit without rewriting it.
_MERGE_AWK = r"""
function block(ind) {
  print ind b
  print ind "\"" host "\":"
  print ind ind "endpoint:"
  print ind ind ind "- \"" ep "\""
  print ind e
}
{ line[++n] = $0 }
END {
  at = 0; bad = 0
  for (i = 1; i <= n; i++) {
    if (line[i] ~ /^[{[]/ || line[i] ~ /^["']mirrors["'][ \t]*:/) bad = 1
    if (line[i] ~ /^mirrors[ \t]*:/) {
      if (at || line[i] !~ /^mirrors[ \t]*:[ \t]*(#.*)?$/) bad = 1
      at = i
    }
  }
  if (bad) exit 3
  ind = "  "
  for (j = at + 1; at && j <= n; j++) {
    if (line[j] ~ /^[ \t]*(#.*)?$/) continue
    if (match(line[j], /^ +/)) ind = substr(line[j], 1, RLENGTH)
    break
  }
  if (created) print c
  for (i = 1; i <= n; i++) { print line[i]; if (i == at) block(ind) }
  if (!at) { print "mirrors:"; block(ind) }
}
"""

#: Exit 0 when a stripped file still holds something besides what the agent
#: added (keep it), 1 when it is only the agent's (remove it).
_LEFT_AWK = r"""
NR == 1 && $0 != c { keep = 1 }
/^[ \t]*(#.*)?$/ { next }
/^mirrors[ \t]*:[ \t]*$/ { next }
{ keep = 1 }
END { exit keep ? 0 : 1 }
"""


def _sh_quote(text: str) -> str:
    return "'" + text.replace("'", "'\"'\"'") + "'"


def k3s_agent_script(registry: ClusterRegistry) -> str:
    """The k3s node agent: merge the mirror into ``registries.yaml``, keep it,
    take it out again on stop.

    It never rewrites entries it did not add: its own entry sits between two
    marker comments under ``mirrors:``; a file it cannot edit that way (flow
    style, JSON) is left alone and reported ``unmergeable``. It never restarts
    k3s: it reports ``restart: needed`` when k3s's containerd does not read
    ``certs.d`` (old k3s), where only a k3s restart loads the mirror.
    """
    begin, end = registries_marks(registry)
    return f"""set -eu
addr="${{{service_env(registry)}:-}}"
if [ -z "$addr" ]; then
  echo "the Service address is not set yet; restarting" >&2
  exit 1
fi
case "$addr" in *:*) ADDR="[$addr]" ;; *) ADDR="$addr" ;; esac
PORT={registry.port}
HOST={_sh_quote(registry.host)}
ETC='{K3S_ETC_MOUNT}'
CTD='{K3S_CONTAINERD_MOUNT}'
STATE='{AGENT_STATE_DIR}'
FILE="$ETC/registries.yaml"
CERTS="$CTD/certs.d/$HOST"
BEGIN={_sh_quote(begin)}
END={_sh_quote(end)}
CREATED={_sh_quote(CREATED_MARK)}
umask 077
strip() {{ awk -v b="$BEGIN" -v e="$END" {_sh_quote(_STRIP_AWK)} "$1"; }}
put() {{
  if [ -f "$FILE" ]; then
    cp -p "$FILE" "$FILE.piceli-tmp"
    cat "$1" > "$FILE.piceli-tmp"
  else
    cp "$1" "$FILE.piceli-tmp"
  fi
  mv "$FILE.piceli-tmp" "$FILE"
}}
report() {{ echo "{REPORT_PREFIX}{{\\"runtime\\":\\"k3s\\",\\"file\\":\\"$1\\",\\"restart\\":\\"$2\\"}}"; }}
cleanup() {{
  rm -f "$STATE/ready" "$CERTS/hosts.toml" "$CERTS/hosts.toml.tmp"
  rmdir "$CERTS" 2>/dev/null || true
  if [ -f "$FILE" ] && grep -qF "$BEGIN" "$FILE"; then
    strip "$FILE" > "$STATE/left.yaml"
    if awk -v c="$CREATED" {_sh_quote(_LEFT_AWK)} "$STATE/left.yaml"; then
      put "$STATE/left.yaml"
    else
      rm -f "$FILE"
    fi
  fi
  exit 0
}}
trap cleanup TERM INT
mkdir -p "$ETC" "$STATE"
created=0
if [ -f "$FILE" ]; then strip "$FILE" > "$STATE/base.yaml"; else : > "$STATE/base.yaml"; created=1; fi
if ! awk -v b="$BEGIN" -v e="$END" -v host="$HOST" -v ep="http://$ADDR:$PORT" \\
    -v c="$CREATED" -v created="$created" {_sh_quote(_MERGE_AWK)} \\
    "$STATE/base.yaml" > "$STATE/registries.yaml"; then
  echo "registries.yaml is not block-style YAML; left unchanged" >&2
  report unmergeable not-needed
  while true; do sleep 3600 & wait $!; done
fi
if [ -f "$FILE" ] && cmp -s "$STATE/registries.yaml" "$FILE"; then
  file=unchanged
else
  put "$STATE/registries.yaml"
  file=written
fi
mkdir -p "$CERTS"
cat > "$CERTS/hosts.toml.tmp" <<EOF
{HOSTS_TOML}EOF
chmod 644 "$CERTS/hosts.toml.tmp"
mv "$CERTS/hosts.toml.tmp" "$CERTS/hosts.toml"
if grep -q config_path "$CTD/config.toml" 2>/dev/null || grep -qF "$HOST" "$CTD/config.toml" 2>/dev/null; then
  restart=not-needed
else
  restart=needed
fi
: > "$STATE/ready"
echo "mirror for $HOST -> $ADDR:$PORT in registries.yaml ($file)"
report "$file" "$restart"
while true; do sleep 3600 & wait $!; done
"""


def parse_report(log: str) -> dict[str, str] | None:
    """The last report line of an agent's log (``{runtime, file, restart}``)."""
    for line in reversed(log.splitlines()):
        if line.startswith(REPORT_PREFIX):
            try:
                body = json.loads(line[len(REPORT_PREFIX) :])
            except ValueError:
                return None
            if isinstance(body, dict) and all(
                isinstance(body.get(key), str) and _WORD.fullmatch(body[key])
                for key in ("runtime", "file", "restart")
            ):
                return {key: body[key] for key in ("runtime", "file", "restart")}
            return None
    return None


_WORD = re.compile(r"[a-z0-9-]{1,32}")


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
    agents = []
    if registry.node_mirror in ("auto", "k3s"):
        agents.append(_k3s_agent(registry, image, drop_all))
    if registry.node_mirror in ("auto", "containerd"):
        agents.append(_containerd_agent(registry, image, drop_all))
    return _base_objects(registry, config_text, claim, server, service_port) + [
        deployment,
        *agents,
    ]


def _runtime_affinity(runtime: str) -> dict[str, Any]:
    """Nodes of one runtime: ``piceli.io/runtime`` decides, else k3s's own label."""
    explicit = {"key": RUNTIME_LABEL, "operator": "In", "values": [runtime]}
    unset = {"key": RUNTIME_LABEL, "operator": "DoesNotExist"}
    k3s = "In" if runtime == "k3s" else "NotIn"
    inferred = {"key": INSTANCE_TYPE_LABEL, "operator": k3s, "values": ["k3s"]}
    return {
        "nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {
                "nodeSelectorTerms": [
                    {"matchExpressions": [explicit]},
                    {"matchExpressions": [unset, inferred]},
                ]
            }
        }
    }


def _k3s_agent(
    registry: ClusterRegistry, image: str, drop_all: dict[str, Any]
) -> dict[str, Any]:
    spec: dict[str, Any] = {
        # Every node pulls, so every k3s node gets the mirror.
        "tolerations": [{"operator": "Exists"}],
        "automountServiceAccountToken": False,
        "enableServiceLinks": True,
        "terminationGracePeriodSeconds": 10,
        "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [
            {
                "name": "mirror",
                "image": image,
                "imagePullPolicy": "IfNotPresent",
                "command": ["/bin/sh", "-c", k3s_agent_script(registry)],
                "readinessProbe": {
                    "exec": {"command": ["test", "-f", f"{AGENT_STATE_DIR}/ready"]},
                    "periodSeconds": 10,
                },
                "resources": {
                    "requests": {"cpu": "1m", "memory": "8Mi"},
                    "limits": {"memory": "32Mi"},
                },
                "securityContext": {
                    "runAsUser": 0,
                    "runAsGroup": 0,
                    "runAsNonRoot": False,
                    **drop_all,
                },
                "volumeMounts": [
                    {"name": "k3s-etc", "mountPath": K3S_ETC_MOUNT},
                    {"name": "k3s-containerd", "mountPath": K3S_CONTAINERD_MOUNT},
                    {"name": "state", "mountPath": AGENT_STATE_DIR},
                ],
            }
        ],
        "volumes": [
            {
                "name": "k3s-etc",
                "hostPath": {"path": K3S_ETC, "type": "DirectoryOrCreate"},
            },
            {
                "name": "k3s-containerd",
                "hostPath": {"path": K3S_CONTAINERD, "type": "DirectoryOrCreate"},
            },
            {"name": "state", "emptyDir": {"medium": "Memory", "sizeLimit": "1Mi"}},
        ],
    }
    if registry.node_mirror == "auto":
        spec["affinity"] = _runtime_affinity("k3s")
    return {
        "apiVersion": "apps/v1",
        "kind": "DaemonSet",
        "metadata": _meta(registry, k3s_agent_name(registry), K3S_COMPONENT),
        "spec": {
            "selector": {"matchLabels": selector(registry, K3S_COMPONENT)},
            "updateStrategy": {
                "type": "RollingUpdate",
                "rollingUpdate": {"maxUnavailable": 1},
            },
            "template": {
                "metadata": {"labels": labels(registry, K3S_COMPONENT)},
                "spec": spec,
            },
        },
    }


def _containerd_agent(
    registry: ClusterRegistry, image: str, drop_all: dict[str, Any]
) -> dict[str, Any]:
    agent = selector(registry, "mirror")
    mirror_file = f"{HOST_MIRROR_DIR}/{registry.host}/hosts.toml"
    daemon_set: dict[str, Any] = {
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
    if registry.node_mirror == "auto":
        pod = daemon_set["spec"]["template"]["spec"]
        pod["affinity"] = _runtime_affinity("containerd")
    return daemon_set


def _base_objects(
    registry: ClusterRegistry,
    config_text: str,
    claim: dict[str, Any],
    server: dict[str, str],
    service_port: dict[str, Any],
) -> list[dict[str, Any]]:
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
    reports: Mapping[str, Mapping[str, str]] | None = None,
    usage: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The status document of ``piceli registry status`` (pure; tested offline).

    ``reports`` maps a node to its agent's report (:func:`parse_report`): each
    mirror then also says its ``runtime``, ``file`` state and whether k3s
    needs a ``restart`` to load it. ``usage`` is :func:`claim_usage`'s
    result (it wins over ``used_bytes``): the claim's own use, where it came
    from, and the filesystem that holds it.
    """
    if usage is not None:
        used_bytes = usage.get("used_bytes")
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
        in MIRROR_COMPONENTS
    }
    mirrors = []
    for node in nodes:
        name = (node.get("metadata") or {}).get("name")
        pod = agents.get(name)
        entry: dict[str, Any] = {
            "node": name,
            "mirror": "ready"
            if pod is not None and _ready(pod)
            else ("pending" if pod is not None else "missing"),
        }
        report = (reports or {}).get(str(name))
        if pod is not None and report:
            entry.update(report)
        mirrors.append(entry)
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
            "used_source": (usage or {}).get("used_source")
            or ("volume-stats" if used_bytes is not None else None),
            "filesystem": (usage or {}).get("filesystem"),
        }
        if claim is not None
        else None,
    }


def volume_stats(
    summary: Mapping[str, Any], namespace: str, claim: str
) -> dict[str, int | None] | None:
    """The kubelet's ``stats/summary`` entry of the claim's volume:
    ``{used_bytes, capacity_bytes}``, else ``None``."""
    for pod in summary.get("pods") or ():
        if (pod.get("podRef") or {}).get("namespace") != namespace:
            continue
        for volume in pod.get("volume") or ():
            ref = volume.get("pvcRef") or {}
            if ref.get("name") == claim and isinstance(volume.get("usedBytes"), int):
                capacity = volume.get("capacityBytes")
                return {
                    "used_bytes": int(volume["usedBytes"]),
                    "capacity_bytes": int(capacity)
                    if isinstance(capacity, int)
                    else None,
                }
    return None


def shared_filesystem(
    summary: Mapping[str, Any],
    stats: Mapping[str, Any],
    claim_bytes: int | None = None,
) -> bool:
    """Whether the volume entry is a filesystem the claim shares (a node-path
    volume such as k3s ``local-path`` reports the node's disk): its capacity
    is the node's or the image filesystem's, or larger than the claim."""
    capacity = stats.get("capacity_bytes")
    if not isinstance(capacity, int):
        return False
    node = summary.get("node") or {}
    image_fs = (node.get("runtime") or {}).get("imageFs") or {}
    if capacity in {
        (node.get("fs") or {}).get("capacityBytes"),
        image_fs.get("capacityBytes"),
    }:
        return True
    return claim_bytes is not None and capacity > claim_bytes * 1.1


def used_bytes(
    summary: Mapping[str, Any],
    namespace: str,
    claim: str,
    claim_bytes: int | None = None,
) -> int | None:
    """The claim's used bytes from a kubelet ``stats/summary`` document, or
    ``None`` when the entry is a shared filesystem (never the node's disk
    called the claim's)."""
    stats = volume_stats(summary, namespace, claim)
    if stats is None or shared_filesystem(summary, stats, claim_bytes):
        return None
    return stats["used_bytes"]


def claim_usage(
    summary: Mapping[str, Any] | None,
    namespace: str,
    claim: str,
    *,
    claim_bytes: int | None,
    du: Callable[[], int | None],
) -> dict[str, Any]:
    """The registry claim's use: ``{used_bytes, used_source, filesystem}``.

    The kubelet's volume entry when it is the claim's own filesystem
    (``volume-stats``); otherwise ``du`` of the registry's storage inside its
    pod (``du``, called only then); otherwise unknown (``None``).
    ``filesystem`` is the volume entry itself, ``shared`` when it is not the
    claim's alone.
    """
    stats = volume_stats(summary or {}, namespace, claim) if summary else None
    filesystem = None
    if stats is not None:
        shared = shared_filesystem(summary or {}, stats, claim_bytes)
        filesystem = {**stats, "shared": shared}
        if not shared:
            return {
                "used_bytes": stats["used_bytes"],
                "used_source": "volume-stats",
                "filesystem": filesystem,
            }
    measured = du()
    return {
        "used_bytes": measured,
        "used_source": "du" if measured is not None else None,
        "filesystem": filesystem,
    }


def quantity_bytes(value: Any) -> int | None:
    """A Kubernetes quantity (``20Gi``) in bytes, ``None`` when unreadable."""
    try:
        from kubernetes.utils import parse_quantity

        return int(parse_quantity(str(value)))
    except Exception:
        return None
