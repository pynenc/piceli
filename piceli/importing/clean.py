"""Reduce live or file manifests to the fields a person declared.

Two steps, both pure:

* :func:`scrub` removes what the API server populates (status, UIDs, resource
  versions, managed fields, bookkeeping annotations) and replaces every Secret
  value with ``<private>`` so no value can reach the generated module;
* :func:`strip_defaults` removes fields whose value is the Kubernetes default
  (``dnsPolicy: ClusterFirst``, ``protocol: TCP`` …), and :func:`is_default`
  answers the same question for one field, so the typed model may render a
  default explicitly without it counting as a difference.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping
from typing import Any

PRIVATE = "<private>"

_RUNTIME_METADATA = (
    "creationTimestamp",
    "deletionGracePeriodSeconds",
    "deletionTimestamp",
    "generateName",
    "generation",
    "managedFields",
    "resourceVersion",
    "selfLink",
    "uid",
)
_BOOKKEEPING_ANNOTATIONS = (
    "deployment.kubernetes.io/revision",
    "kubectl.kubernetes.io/last-applied-configuration",
)
_BOOKKEEPING_PREFIXES = (
    "piceli.io/",
    "pv.kubernetes.io/",
    "volume.kubernetes.io/",
    "volume.beta.kubernetes.io/",
)


def scrub(manifest: Mapping[str, Any], namespace: str | None = None) -> dict[str, Any]:
    """A copy without server-populated fields and without Secret values.

    ``namespace`` (when given) is set on the copy, as the typed app renders it.
    """
    value = json.loads(json.dumps(manifest))
    value.pop("status", None)
    metadata = value.setdefault("metadata", {})
    for key in _RUNTIME_METADATA:
        metadata.pop(key, None)
    finalizers = [
        item
        for item in metadata.pop("finalizers", None) or []
        if not str(item).startswith("kubernetes.io/")
    ]
    if finalizers:
        metadata["finalizers"] = finalizers
    annotations = {
        key: item
        for key, item in (metadata.pop("annotations", None) or {}).items()
        if key not in _BOOKKEEPING_ANNOTATIONS
        and not key.startswith(_BOOKKEEPING_PREFIXES)
    }
    if annotations:
        metadata["annotations"] = annotations
    if not metadata.get("labels"):
        metadata.pop("labels", None)
    if namespace is not None:
        metadata["namespace"] = namespace
    if value.get("kind") == "Secret":
        data = dict.fromkeys(
            [*(value.get("data") or {}), *(value.get("stringData") or {})], PRIVATE
        )
        value.pop("stringData", None)
        value["data"] = dict(sorted(data.items()))
    return dict(value)


# ----------------------------------------------------------------- defaults

Predicate = Callable[[Any, Mapping[str, Any]], bool]
Pattern = tuple[str | frozenset[str], ...]


def _equals(expected: Any) -> Predicate:
    return lambda value, _parent: bool(value == expected)


def _always(_value: Any, _parent: Mapping[str, Any]) -> bool:
    return True


def pull_policy_default(image: str) -> str:
    """``imagePullPolicy`` the API server picks for ``image``."""
    if "@" in image:
        return "IfNotPresent"
    last = image.rsplit("/", 1)[-1]
    tag = last.rsplit(":", 1)[1] if ":" in last else "latest"
    return "Always" if tag == "latest" else "IfNotPresent"


_POD = ("spec", "template", "spec")
_CONTAINERS = frozenset({"containers", "initContainers"})
_PROBES = frozenset({"readinessProbe", "livenessProbe", "startupProbe"})
_ROLLING = {
    "type": "RollingUpdate",
    "rollingUpdate": {"maxSurge": "25%", "maxUnavailable": "25%"},
}

_DEPLOYMENT: tuple[tuple[Pattern, Predicate], ...] = (
    (("spec", "replicas"), _equals(1)),
    (("spec", "progressDeadlineSeconds"), _equals(600)),
    (("spec", "revisionHistoryLimit"), _equals(10)),
    (
        ("spec", "strategy"),
        lambda value, _: value in ({}, {"type": "RollingUpdate"}, _ROLLING),
    ),
    (("spec", "template", "metadata", "creationTimestamp"), _always),
    ((*_POD, "dnsPolicy"), _equals("ClusterFirst")),
    ((*_POD, "restartPolicy"), _equals("Always")),
    ((*_POD, "schedulerName"), _equals("default-scheduler")),
    ((*_POD, "securityContext"), _equals({})),
    ((*_POD, "terminationGracePeriodSeconds"), _equals(30)),
    (
        (*_POD, "serviceAccount"),
        lambda value, parent: value == parent.get("serviceAccountName"),
    ),
    (
        (*_POD, _CONTAINERS, "*", "terminationMessagePath"),
        _equals("/dev/termination-log"),
    ),
    ((*_POD, _CONTAINERS, "*", "terminationMessagePolicy"), _equals("File")),
    (
        (*_POD, _CONTAINERS, "*", "imagePullPolicy"),
        lambda value, parent: (
            value == pull_policy_default(str(parent.get("image", "")))
        ),
    ),
    ((*_POD, _CONTAINERS, "*", "resources"), _equals({})),
    ((*_POD, _CONTAINERS, "*", "ports", "*", "protocol"), _equals("TCP")),
    ((*_POD, _CONTAINERS, "*", _PROBES, "timeoutSeconds"), _equals(1)),
    ((*_POD, _CONTAINERS, "*", _PROBES, "periodSeconds"), _equals(10)),
    ((*_POD, _CONTAINERS, "*", _PROBES, "successThreshold"), _equals(1)),
    ((*_POD, _CONTAINERS, "*", _PROBES, "failureThreshold"), _equals(3)),
    ((*_POD, _CONTAINERS, "*", _PROBES, "httpGet", "scheme"), _equals("HTTP")),
    (
        (*_POD, _CONTAINERS, "*", "env", "*", "valueFrom", "fieldRef", "apiVersion"),
        _equals("v1"),
    ),
    (
        (
            *_POD,
            "volumes",
            "*",
            frozenset({"configMap", "secret", "downwardAPI", "projected"}),
            "defaultMode",
        ),
        _equals(0o644),
    ),
)

_SERVICE: tuple[tuple[Pattern, Predicate], ...] = (
    (("spec", "clusterIP"), lambda value, _: value != "None"),
    (("spec", "clusterIPs"), _always),
    (("spec", "ipFamilies"), _always),
    (("spec", "ipFamilyPolicy"), _equals("SingleStack")),
    (("spec", "sessionAffinity"), _equals("None")),
    (("spec", "internalTrafficPolicy"), _equals("Cluster")),
    (("spec", "externalTrafficPolicy"), _equals("Cluster")),
    (("spec", "allocateLoadBalancerNodePorts"), _equals(True)),
    (("spec", "type"), _equals("ClusterIP")),
    (("spec", "ports", "*", "protocol"), _equals("TCP")),
    (("spec", "ports", "*", "nodePort"), _always),
    (
        ("spec", "ports", "*", "targetPort"),
        lambda value, parent: value == parent.get("port"),
    ),
)

_NETWORK_POLICY: tuple[tuple[Pattern, Predicate], ...] = (
    (
        ("spec", "policyTypes"),
        lambda value, parent: (
            value == (["Ingress", "Egress"] if "egress" in parent else ["Ingress"])
        ),
    ),
    (("spec", "ingress"), _equals([])),
    (
        ("spec", frozenset({"ingress", "egress"}), "*", "ports", "*", "protocol"),
        _equals("TCP"),
    ),
)

_SECRET: tuple[tuple[Pattern, Predicate], ...] = ((("type",), _equals("Opaque")),)

DEFAULTS: Mapping[str, tuple[tuple[Pattern, Predicate], ...]] = {
    "Deployment": _DEPLOYMENT,
    "Service": _SERVICE,
    "NetworkPolicy": _NETWORK_POLICY,
    "Secret": _SECRET,
}


def _matches(pattern: Pattern, path: tuple[str, ...]) -> bool:
    if len(pattern) != len(path):
        return False
    for expected, actual in zip(pattern, path, strict=True):
        if isinstance(expected, frozenset):
            if actual not in expected:
                return False
        elif expected != actual:
            return False
    return True


def is_default(
    kind: str, path: tuple[str, ...], value: Any, parent: Mapping[str, Any]
) -> bool:
    """Whether ``value`` at ``path`` (list indices as ``*``) is the API default."""
    return any(
        _matches(pattern, path) and predicate(value, parent)
        for pattern, predicate in DEFAULTS.get(kind, ())
    )


def strip_defaults(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """A copy of ``manifest`` without fields that hold the Kubernetes default."""
    kind = str(manifest.get("kind", ""))
    value = copy.deepcopy(dict(manifest))

    def walk(node: Any, path: tuple[str, ...]) -> None:
        if isinstance(node, dict):
            for key in list(node):
                child_path = (*path, key)
                if is_default(kind, child_path, node[key], node):
                    del node[key]
                else:
                    walk(node[key], child_path)
        elif isinstance(node, list):
            for item in node:
                walk(item, (*path, "*"))

    walk(value, ())
    return value
