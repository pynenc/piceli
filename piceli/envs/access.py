"""Tailnet access to an environment's services (0.19.0).

``Environment(access=Access(tailnet=True))`` (or ``Cluster(access=...)`` for
every environment of a cluster, or ``Environment.per_branch(access=...)``)
exposes the environment's declared Services on the cluster's Tailscale
tailnet instead of through ``kubectl port-forward`` and the API server's
stream proxy. For each exposed Service the environment's release adds a
companion Service ``<name>-tailnet`` of ``type: LoadBalancer`` with
``loadBalancerClass: tailscale`` (or, with ``https=True``, a Tailscale
``Ingress`` per HTTP port): the Tailscale Kubernetes operator (which the
cluster's owner installs; Piceli does not) gives it a MagicDNS name on the
tailnet, reachable only by the tailnet's devices its policy lets in. The
app's own login stays as it is. ``piceli access`` prints the tailnet URLs;
port-forward stays the fallback.

The operator's proxies reach the pods from its own namespace. Which ingress
NetworkPolicies need to let them in is decided by the **pods**: the pod
templates of the release's workloads whose labels match an exposed
Service's selector, and every ingress policy whose ``podSelector``
(``matchLabels`` and ``matchExpressions``) selects one of them gets one
more rule admitting the operator's namespace on the exposed target ports.
No new policy is added (a new policy would isolate the pods from every
other source), no policy is widened beyond those ports, and what cannot be
decided is reported (:data:`SKIPPED`, :data:`UNCHANGED`), never skipped
silently.

Importing this module is side-effect free.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from piceli.envs.model import EnvError

#: The label of every tailnet companion (``piceli access`` finds them).
ACCESS_LABEL = "piceli.io/access"
#: The label naming the Service a companion exposes.
SERVICE_LABEL = "piceli.io/access-service"
SUFFIX = "-tailnet"
#: Note codes (``access.notes`` in a plan, ``tailnet_notes`` in ``piceli access``).
SKIPPED = "access-tailnet-skipped"
UNCHANGED = "access-policy-unchanged"
_LABEL = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")
_FIELDS = re.compile(r"\{(env|service|namespace)\}")
_TAG = re.compile(r"tag:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_TEMPLATED = ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job")


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
        (label ``tailscale.com/proxy-class``), if any.
    :param tags: The proxies' Tailscale tags (``tag:NAME``; annotation
        ``tailscale.com/tags``); default: the operator's own default tag.
        The tailnet policy must make the operator's tag an owner of them.
    :param https: For every exposed port whose forward is HTTP (it declares
        a ``path`` or an HTTP health check), a Tailscale ``Ingress``
        instead: ``https://NAME.TAILNET.ts.net/`` with a tailnet
        certificate and no port in the URL. Needs MagicDNS and HTTPS
        certificates on in the tailnet. A Service with other ports too
        keeps the plain companion (reported).
    """

    tailnet: bool = False
    services: Sequence[str] = ()
    hostname: str = "{env}-{service}"
    operator_namespace: str = "tailscale"
    proxy_class: str | None = None
    tags: Sequence[str] = ()
    https: bool = False

    def __post_init__(self) -> None:
        for name in ("tailnet", "https"):
            if not isinstance(getattr(self, name), bool):
                raise EnvError(
                    "env-config-invalid", f"Access({name}=) is True or False"
                )
        services = tuple(str(name) for name in self.services)
        if not all(_LABEL.fullmatch(name) for name in services):
            raise EnvError("env-config-invalid", "Access(services=) are Service names")
        object.__setattr__(self, "services", services)
        if isinstance(self.tags, str):
            raise EnvError("env-config-invalid", "Access(tags=) is a list of tag:NAME")
        tags = tuple(str(tag) for tag in self.tags)
        if not all(_TAG.fullmatch(tag) for tag in tags):
            raise EnvError(
                "env-config-invalid",
                "Access(tags=) are Tailscale tags: tag:NAME (letters, digits, -)",
            )
        object.__setattr__(self, "tags", tags)
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
            "tags": list(self.tags),
            "https": self.https,
        }


# ------------------------------------------------------------ what is exposed


def _forwards(app: Any, namespace: str) -> list[Any]:
    shortcuts = getattr(app, "shortcuts", None)
    return [
        item
        for item in (shortcuts(namespace) if callable(shortcuts) else ())
        if item.target.startswith("service/")
    ]


def exposed_services(
    access: Access | None, app: Any, namespace: str
) -> dict[str, tuple[int, ...]]:
    """``{service: ports}`` the environment exposes (empty ports: every port)."""
    if access is None or not access.tailnet:
        return {}
    if access.services:
        return dict.fromkeys(access.services, ())
    found: dict[str, list[int]] = {}
    for item in _forwards(app, namespace):
        found.setdefault(item.target.split("/", 1)[1], []).append(int(item.remote_port))
    return {name: tuple(sorted(set(ports))) for name, ports in found.items()}


def http_ports(app: Any, namespace: str) -> dict[str, tuple[int, ...]]:
    """``{service: ports}`` whose declared forward is HTTP (a path or an
    HTTP health check): the ones ``Access(https=True)`` serves over HTTPS."""
    found: dict[str, set[int]] = {}
    for item in _forwards(app, namespace):
        probe = getattr(item, "probe", None)
        if getattr(item, "path", None) or getattr(probe, "type", None) == "http":
            found.setdefault(item.target.split("/", 1)[1], set()).add(
                int(item.remote_port)
            )
    return {name: tuple(sorted(ports)) for name, ports in found.items()}


# ------------------------------------------------------------ selectors


def selects(selector: Any, labels: Mapping[str, str]) -> bool | None:
    """Whether a Kubernetes label selector selects ``labels``; ``None`` when
    it cannot be evaluated (malformed, or an unknown operator). A missing or
    empty selector selects everything."""
    if selector is None:
        return True
    if not isinstance(selector, Mapping):
        return None
    wanted = selector.get("matchLabels") or {}
    expressions = selector.get("matchExpressions") or []
    if not isinstance(wanted, Mapping) or not isinstance(expressions, list):
        return None
    result = all(labels.get(str(key)) == str(value) for key, value in wanted.items())
    for expression in expressions:
        if not isinstance(expression, Mapping) or "key" not in expression:
            return None
        key = str(expression["key"])
        operator = expression.get("operator")
        values = expression.get("values") or []
        if not isinstance(values, list):
            return None
        if operator == "In":
            matched = key in labels and labels[key] in {str(v) for v in values}
        elif operator == "NotIn":
            matched = key not in labels or labels[key] not in {str(v) for v in values}
        elif operator == "Exists":
            matched = key in labels
        elif operator == "DoesNotExist":
            matched = key not in labels
        else:
            return None
        result = result and matched
    return result


def _meta(manifest: Mapping[str, Any]) -> dict[str, Any]:
    value = manifest.get("metadata")
    return value if isinstance(value, dict) else {}


def pod_templates(
    manifests: Iterable[Mapping[str, Any]],
) -> list[tuple[str, dict[str, str]]]:
    """``(Kind/name, labels)`` of every pod the release's objects create."""
    found = []
    for manifest in manifests:
        kind = manifest.get("kind")
        spec = manifest.get("spec") or {}
        if kind in _TEMPLATED:
            template = spec.get("template") or {}
        elif kind == "CronJob":
            template = ((spec.get("jobTemplate") or {}).get("spec") or {}).get(
                "template"
            ) or {}
        elif kind == "Pod":
            template = manifest
        else:
            continue
        labels = (template.get("metadata") or {}).get("labels") or {}
        if isinstance(labels, Mapping):
            found.append(
                (
                    f"{kind}/{_meta(manifest).get('name')}",
                    {str(k): str(v) for k, v in labels.items()},
                )
            )
    return found


# ------------------------------------------------------------ the objects


@dataclass
class Backend:
    """One exposed Service: its selector and the pod ports the proxies reach."""

    service: str
    selector: dict[str, str]
    ports: list[dict[str, Any]]


@dataclass
class TailnetPlan:
    """The tailnet objects of an environment, their backends and the notes."""

    objects: list[dict[str, Any]] = field(default_factory=list)
    backends: list[Backend] = field(default_factory=list)
    notes: list[dict[str, Any]] = field(default_factory=list)


def _skipped(service: str, why: str, detail: str) -> dict[str, Any]:
    return {"code": SKIPPED, "service": service, "why": why, "detail": detail}


def _companion_meta(
    name: str,
    namespace: str,
    service: str,
    access: Access,
    annotations: dict[str, str],
) -> dict[str, Any]:
    labels = {
        "app.kubernetes.io/managed-by": "piceli",
        ACCESS_LABEL: "tailnet",
        SERVICE_LABEL: service,
    }
    if access.proxy_class:
        labels["tailscale.com/proxy-class"] = access.proxy_class
    if access.tags:
        annotations = {**annotations, "tailscale.com/tags": ",".join(access.tags)}
    return {
        "name": name[:63],
        "namespace": namespace,
        "labels": labels,
        **({"annotations": annotations} if annotations else {}),
    }


def tailnet_plan(
    manifests: Sequence[Mapping[str, Any]],
    access: Access,
    exposed: Mapping[str, tuple[int, ...]],
    env: str,
    namespace: str,
    http: Mapping[str, tuple[int, ...]] | None = None,
) -> TailnetPlan:
    """The companion Services (and, with ``https``, Ingresses) of ``exposed``."""
    plan = TailnetPlan()
    services = {
        _meta(item).get("name"): item
        for item in manifests
        if item.get("kind") == "Service"
    }
    for name, wanted in sorted(exposed.items()):
        source = services.get(name)
        if source is None:
            plan.notes.append(
                _skipped(name, "not-found", f"the release has no Service {name!r}")
            )
            continue
        spec = source.get("spec") or {}
        selector = spec.get("selector")
        if not isinstance(selector, Mapping) or not selector:
            plan.notes.append(
                _skipped(
                    name,
                    "no-selector",
                    "a Service without a selector has no pods to reach",
                )
            )
            continue
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
            plan.notes.append(
                _skipped(
                    name,
                    "no-matching-port",
                    f"the Service has no port {list(wanted)}",
                )
            )
            continue
        plan.backends.append(
            Backend(
                name,
                {str(k): str(v) for k, v in selector.items()},
                [
                    {
                        "port": port.get("targetPort", port.get("port")),
                        "protocol": port.get("protocol", "TCP"),
                    }
                    for port in ports
                ],
            )
        )
        host = access.host(env, name, namespace)
        secure = set((http or {}).get(name) or ()) if access.https else set()
        served = [p for p in ports if int(p.get("port") or 0) in secure]
        if served and len(served) < len(ports):
            plan.notes.append(
                _skipped(
                    name,
                    "https-mixed-ports",
                    "the Service exposes ports that are not HTTP too: one plain "
                    "companion serves them all over http",
                )
            )
            served = []
        for index, port in enumerate(served):
            plan.objects.append(
                {
                    "apiVersion": "networking.k8s.io/v1",
                    "kind": "Ingress",
                    "metadata": _companion_meta(
                        f"{name}{SUFFIX}"
                        if index == 0
                        else f"{name}-{port['port']}{SUFFIX}",
                        namespace,
                        name,
                        access,
                        {},
                    ),
                    "spec": {
                        "ingressClassName": "tailscale",
                        "defaultBackend": {
                            "service": {
                                "name": name,
                                "port": {"number": int(port["port"])},
                            }
                        },
                        "tls": [
                            {
                                "hosts": [
                                    host
                                    if index == 0
                                    else f"{host[:56]}-{port['port']}"
                                ]
                            }
                        ],
                    },
                }
            )
        if served:
            continue
        plan.objects.append(
            {
                "apiVersion": "v1",
                "kind": "Service",
                "metadata": _companion_meta(
                    f"{name}{SUFFIX}",
                    namespace,
                    name,
                    access,
                    {"tailscale.com/hostname": host},
                ),
                "spec": {
                    "type": "LoadBalancer",
                    "loadBalancerClass": "tailscale",
                    "selector": dict(selector),
                    "ports": ports,
                },
            }
        )
    return plan


# ------------------------------------------------------------ the policies


def _ingress_policy(policy: Mapping[str, Any]) -> bool:
    if policy.get("kind") != "NetworkPolicy":
        return False
    spec = policy.get("spec") or {}
    return "Ingress" in (spec.get("policyTypes") or ["Ingress"])


def allow_tailnet(
    policies: Sequence[dict[str, Any]],
    backends: Sequence[Backend],
    manifests: Sequence[Mapping[str, Any]],
    access: Access,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Let the operator's proxies reach the exposed pods (in place).

    Every ingress policy whose ``podSelector`` selects a pod template of
    ``manifests`` that an exposed Service's selector matches gets one rule
    admitting ``access.operator_namespace`` on those Services' target ports.
    Returns ``(changed policies, notes)``: a policy that cannot be evaluated,
    and a Service whose pods the release does not create, are notes.
    """
    templates = pod_templates(manifests)
    pods: list[tuple[Backend, list[tuple[str, dict[str, str]]]]] = []
    notes: list[dict[str, Any]] = []
    for backend in backends:
        matched = [
            (name, labels)
            for name, labels in templates
            if all(labels.get(k) == v for k, v in backend.selector.items())
        ]
        if not matched:
            notes.append(
                {
                    "code": UNCHANGED,
                    "service": backend.service,
                    "policy": None,
                    "why": "pods-not-in-release",
                    "detail": "no workload of the release has pods this Service "
                    "selects, so the policies in front of them cannot be checked",
                }
            )
        pods.append((backend, matched))
    source = {
        "namespaceSelector": {
            "matchLabels": {"kubernetes.io/metadata.name": access.operator_namespace}
        }
    }
    changed = []
    for policy in policies:
        if not _ingress_policy(policy):
            continue
        spec = policy.setdefault("spec", {})
        selector = spec.get("podSelector")
        ports: list[dict[str, Any]] = []
        unknown = False
        for backend, matched in pods:
            for _name, labels in matched:
                verdict = selects(selector, labels)
                if verdict is None:
                    unknown = True
                elif verdict:
                    for entry in backend.ports:
                        if entry not in ports:
                            ports.append(dict(entry))
                    break
        if unknown:
            notes.append(
                {
                    "code": UNCHANGED,
                    "service": None,
                    "policy": _meta(policy).get("name"),
                    "why": "selector-unevaluable",
                    "detail": "its podSelector cannot be evaluated; if it selects "
                    "the exposed pods, the operator's proxies are blocked",
                }
            )
            continue
        if not ports:
            continue
        rule = {"from": [source], "ports": ports}
        rules = spec.setdefault("ingress", [])
        if rule not in rules:
            rules.append(rule)
            changed.append(policy)
    return changed, notes


def admitting(policies: Iterable[Mapping[str, Any]], access: Access) -> list[str]:
    """The names of the policies that admit the operator's namespace."""
    source = {
        "namespaceSelector": {
            "matchLabels": {"kubernetes.io/metadata.name": access.operator_namespace}
        }
    }
    return sorted(
        str(_meta(policy).get("name"))
        for policy in policies
        if _ingress_policy(policy)
        and any(
            source in (rule.get("from") or [])
            for rule in (policy.get("spec") or {}).get("ingress") or ()
            if isinstance(rule, Mapping)
        )
    )


def tailnet_report(
    manifests: Sequence[Mapping[str, Any]],
    access: Access,
    exposed: Mapping[str, tuple[int, ...]],
    env: str,
    namespace: str,
    http: Mapping[str, tuple[int, ...]] | None = None,
) -> dict[str, Any]:
    """What a rendered environment puts on the tailnet, and why not the rest.

    ``manifests``: the environment's rendered objects (companions and the
    amended policies included). ``tailnet`` lists each companion (kind,
    name, Service, host, ports); ``policies`` the ingress policies that admit
    the operator's namespace; ``notes`` every :data:`SKIPPED` Service and
    :data:`UNCHANGED` policy. Never contacts the cluster.
    """
    import copy

    plan = tailnet_plan(manifests, access, exposed, env, namespace, http)
    policies = [dict(m) for m in manifests if m.get("kind") == "NetworkPolicy"]
    _changed, notes = allow_tailnet(
        copy.deepcopy(policies), plan.backends, manifests, access
    )
    companions = []
    for item in plan.objects:
        meta = _meta(item)
        spec = item.get("spec") or {}
        if item["kind"] == "Ingress":
            host = spec["tls"][0]["hosts"][0]
            ports = [spec["defaultBackend"]["service"]["port"]["number"]]
        else:
            host = meta["annotations"]["tailscale.com/hostname"]
            ports = [port.get("port") for port in spec.get("ports") or ()]
        companions.append(
            {
                "kind": item["kind"],
                "name": meta["name"],
                "service": meta["labels"][SERVICE_LABEL],
                "host": host,
                "ports": ports,
                "scheme": "https" if item["kind"] == "Ingress" else "http",
            }
        )
    return {
        "operator_namespace": access.operator_namespace,
        "tailnet": companions,
        "policies": admitting(policies, access),
        "notes": plan.notes + notes,
    }
