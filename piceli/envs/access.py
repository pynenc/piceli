"""Tailnet access to an environment's services (0.19.0).

``Environment(access=Access(tailnet=True))`` (or ``Cluster(access=...)`` for
every environment of a cluster, or ``Environment.per_branch(access=...)``)
exposes the environment's declared Services on the cluster's Tailscale
tailnet instead of through ``kubectl port-forward`` and the API server's
stream proxy. For each exposed Service the environment's release adds a
companion Service ``<name>-tailnet`` of ``type: LoadBalancer`` with
``loadBalancerClass: tailscale``: the Tailscale Kubernetes operator (which
the cluster's owner installs; Piceli does not) gives it a MagicDNS name on
the tailnet, reachable only by the tailnet's devices under its ACLs. The
app's own login stays as it is. ``piceli access`` prints the tailnet URLs;
port-forward stays the fallback.

The operator's proxy reaches the pods from its own namespace: every ingress
NetworkPolicy of the environment that already selects those pods gets one
more rule allowing that namespace on the exposed ports. No new policy is
added (a new policy would isolate the pods from every other source).

Importing this module is side-effect free.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from piceli.envs.model import EnvError

#: The label of every tailnet companion Service (``piceli access`` finds them).
ACCESS_LABEL = "piceli.io/access"
SUFFIX = "-tailnet"
_LABEL = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")
_FIELDS = re.compile(r"\{(env|service|namespace)\}")


@dataclass(frozen=True)
class Access:
    """How people reach an environment's services.

    :param tailnet: Expose the Services on the Tailscale tailnet through the
        Tailscale Kubernetes operator (``loadBalancerClass: tailscale``).
    :param services: The Services to expose (their every port); default:
        every Service the app declares a forward for
        (``app.access.forward``), on the forwarded port.
    :param hostname: The tailnet machine name, from ``{env}``, ``{service}``
        and ``{namespace}`` (a DNS label once filled in).
    :param operator_namespace: Where the operator's proxies run (the
        namespace the environment's ingress policies then allow).
    :param proxy_class: The operator's ``ProxyClass`` for these proxies
        (``tailscale.com/proxy-class``), if any.
    """

    tailnet: bool = False
    services: Sequence[str] = ()
    hostname: str = "{env}-{service}"
    operator_namespace: str = "tailscale"
    proxy_class: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.tailnet, bool):
            raise EnvError("env-config-invalid", "Access(tailnet=) is True or False")
        services = tuple(str(name) for name in self.services)
        if not all(_LABEL.fullmatch(name) for name in services):
            raise EnvError("env-config-invalid", "Access(services=) are Service names")
        object.__setattr__(self, "services", services)
        sample = _FIELDS.sub("x", self.hostname)
        if not _LABEL.fullmatch(sample) or "{" in sample:
            raise EnvError(
                "env-config-invalid",
                "Access(hostname=) is a DNS label with {env}, {service}, {namespace}",
            )
        if not _LABEL.fullmatch(self.operator_namespace):
            raise EnvError(
                "env-config-invalid", "Access(operator_namespace=) is a namespace"
            )
        if self.proxy_class is not None and not _LABEL.fullmatch(self.proxy_class):
            raise EnvError(
                "env-config-invalid", "Access(proxy_class=) is a ProxyClass name"
            )

    def host(self, env: str, service: str, namespace: str) -> str:
        """The tailnet machine name of ``service`` (at most 63 characters)."""
        name = self.hostname.format(env=env, service=service, namespace=namespace)
        name = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
        return name[:63].rstrip("-") or "service"

    def describe(self) -> dict[str, Any]:
        return {
            "tailnet": self.tailnet,
            "services": list(self.services),
            "hostname": self.hostname,
            "operator_namespace": self.operator_namespace,
            "proxy_class": self.proxy_class,
        }


def exposed_services(
    access: Access | None, app: Any, namespace: str
) -> dict[str, tuple[int, ...]]:
    """``{service: ports}`` the environment exposes (empty ports: every port)."""
    if access is None or not access.tailnet:
        return {}
    if access.services:
        return dict.fromkeys(access.services, ())
    found: dict[str, list[int]] = {}
    shortcuts = getattr(app, "shortcuts", None)
    for item in shortcuts(namespace) if callable(shortcuts) else ():
        if item.target.startswith("service/"):
            found.setdefault(item.target.split("/", 1)[1], []).append(
                int(item.remote_port)
            )
    return {name: tuple(sorted(set(ports))) for name, ports in found.items()}


def _meta(manifest: Mapping[str, Any]) -> dict[str, Any]:
    value = manifest.get("metadata")
    return value if isinstance(value, dict) else {}


def tailnet_services(
    manifests: Sequence[Mapping[str, Any]],
    access: Access,
    exposed: Mapping[str, tuple[int, ...]],
    env: str,
    namespace: str,
) -> list[dict[str, Any]]:
    """The companion Services: same selector, the exposed ports."""
    found = []
    services = {
        _meta(item).get("name"): item
        for item in manifests
        if item.get("kind") == "Service"
    }
    for name, wanted in sorted(exposed.items()):
        source = services.get(name)
        if source is None:
            continue
        spec = source.get("spec") or {}
        selector = spec.get("selector")
        if not isinstance(selector, Mapping) or not selector:
            continue  # a Service without a selector cannot be exposed
        ports = [
            {
                key: port[key]
                for key in ("name", "port", "targetPort", "protocol")
                if key in port
            }
            for port in spec.get("ports") or ()
            if not wanted or int(port.get("port") or 0) in wanted
        ]
        if not ports:
            continue
        annotations = {"tailscale.com/hostname": access.host(env, name, namespace)}
        if access.proxy_class:
            annotations["tailscale.com/proxy-class"] = access.proxy_class
        found.append(
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": {
                    "name": f"{name}{SUFFIX}"[:63],
                    "namespace": namespace,
                    "labels": {
                        "app.kubernetes.io/managed-by": "piceli",
                        ACCESS_LABEL: "tailnet",
                        "piceli.io/access-service": name,
                    },
                    "annotations": annotations,
                },
                "spec": {
                    "type": "LoadBalancer",
                    "loadBalancerClass": "tailscale",
                    "selector": dict(selector),
                    "ports": ports,
                },
            }
        )
    return found


def allow_tailnet(
    policies: Sequence[dict[str, Any]],
    services: Sequence[Mapping[str, Any]],
    access: Access,
) -> list[dict[str, Any]]:
    """Add the operator's namespace to each ingress policy that selects an
    exposed Service's pods (in place); the policies changed."""
    changed = []
    source = {
        "namespaceSelector": {
            "matchLabels": {"kubernetes.io/metadata.name": access.operator_namespace}
        }
    }
    for policy in policies:
        if policy.get("kind") != "NetworkPolicy":
            continue
        spec = policy.setdefault("spec", {})
        if "Ingress" not in (spec.get("policyTypes") or ["Ingress"]):
            continue
        selector = spec.get("podSelector") or {}
        if selector.get("matchExpressions"):
            continue  # not provably the same pods: left as declared
        wanted = selector.get("matchLabels") or {}
        ports: list[dict[str, Any]] = []
        for service in services:
            labels = (service.get("spec") or {}).get("selector") or {}
            if all(labels.get(key) == value for key, value in wanted.items()):
                for port in (service.get("spec") or {}).get("ports") or ():
                    entry = {
                        "port": port.get("targetPort", port.get("port")),
                        "protocol": port.get("protocol", "TCP"),
                    }
                    if entry not in ports:
                        ports.append(entry)
        if ports:
            spec.setdefault("ingress", []).append({"from": [source], "ports": ports})
            changed.append(policy)
    return changed
