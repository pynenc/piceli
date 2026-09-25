"""An in-process fake Kubernetes API server for tests, driven by the real client.

Maturity: **preview**. Public and covered by semantic versioning from 0.4.0:
names listed in ``__all__`` only change in a minor release with a changelog
entry, never silently.

The server speaks enough of the Kubernetes REST API (discovery, list with
pagination and label selectors, get, create, server-side apply, merge patch,
delete with preconditions, dry run) for :class:`~piceli.k8s.ops.kubernetes_provider.KubernetesProvider`,
``piceli release`` and ``piceli import live`` to run against it unchanged over a
loopback HTTP socket. Faults (HTTP errors, delays, disconnects, raw bodies) can
be injected per request, and an opt-in field-ownership model reproduces
server-side-apply bookkeeping (``managedFields``, conflicts, pruning).

It is not a conformance-tested API server: there is no admission, no
defaulting beyond readiness status, no watch, and one namespace
(:data:`TARGET` ``.namespace``) holds every namespaced object.

Importing this module has no side effects: nothing listens until
:func:`serve` (or :func:`fake_cluster`) is entered.

Example::

    from piceli.testing import fake_cluster, manifest

    with fake_cluster() as cluster:
        cluster.api.put(manifest("ConfigMap", "settings", value="on"))
        page = cluster.provider.get(...)  # the real client, over loopback
"""

from __future__ import annotations

import copy
import hashlib
import json
import socket
import textwrap
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path as FilePath
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from piceli.k8s.ops.discovery import EvidenceSource, PlanTarget
from piceli.k8s.ops.kubernetes_provider import KubernetesProvider

__all__ = [
    "TARGET",
    "TYPES",
    "FakeAPI",
    "FakeCluster",
    "fake_cluster",
    "field_paths",
    "fields_v1",
    "manifest",
    "paths_of",
    "provider_at",
    "serve",
    "value_at",
    "write_kubeconfig",
]

#: The one target every fake server represents: cluster UID and namespace.
TARGET = PlanTarget("acceptance-cluster", "piceli-test")
#: Served resources by plural: ``(apiVersion, kind, namespaced)``.
TYPES: Mapping[str, tuple[str, str, bool]] = {
    "configmaps": ("v1", "ConfigMap", True),
    "secrets": ("v1", "Secret", True),
    "services": ("v1", "Service", True),
    "pods": ("v1", "Pod", True),
    "replicasets": ("apps/v1", "ReplicaSet", True),
    "persistentvolumeclaims": ("v1", "PersistentVolumeClaim", True),
    "persistentvolumes": ("v1", "PersistentVolume", False),
    "namespaces": ("v1", "Namespace", False),
    "deployments": ("apps/v1", "Deployment", True),
    "statefulsets": ("apps/v1", "StatefulSet", True),
    "daemonsets": ("apps/v1", "DaemonSet", True),
    "jobs": ("batch/v1", "Job", True),
    "cronjobs": ("batch/v1", "CronJob", True),
    "horizontalpodautoscalers": ("autoscaling/v2", "HorizontalPodAutoscaler", True),
    "poddisruptionbudgets": ("policy/v1", "PodDisruptionBudget", True),
    "ingresses": ("networking.k8s.io/v1", "Ingress", True),
    "httproutes": ("gateway.networking.k8s.io/v1", "HTTPRoute", True),
    "networkpolicies": ("networking.k8s.io/v1", "NetworkPolicy", True),
    "serviceaccounts": ("v1", "ServiceAccount", True),
    "roles": ("rbac.authorization.k8s.io/v1", "Role", True),
    "rolebindings": ("rbac.authorization.k8s.io/v1", "RoleBinding", True),
    "clusterroles": ("rbac.authorization.k8s.io/v1", "ClusterRole", False),
    "clusterrolebindings": (
        "rbac.authorization.k8s.io/v1",
        "ClusterRoleBinding",
        False,
    ),
    "widgets": ("example.test/v1", "Widget", False),
    # The release lock of shared state (``state="cluster"``).
    "leases": ("coordination.k8s.io/v1", "Lease", True),
}


def manifest(
    kind: str = "ConfigMap", name: str = "settings", *, value: str = "one"
) -> dict[str, Any]:
    """A minimal valid object of ``kind`` in :data:`TARGET`'s namespace.

    ``value`` fills the one data key of a ConfigMap (``mode``) or Secret
    (``password``). A Deployment gets one container and ``app: <name>``
    selector labels.
    """
    api, _, namespaced = next(entry for entry in TYPES.values() if entry[1] == kind)
    result: dict[str, Any] = {
        "apiVersion": api,
        "kind": kind,
        "metadata": {"name": name},
    }
    if namespaced:
        result["metadata"]["namespace"] = TARGET.namespace
    if kind == "ConfigMap":
        result["data"] = {"mode": value}
    elif kind == "Secret":
        result["data"] = {"password": value}
    elif kind == "Deployment":
        result["spec"] = {
            "replicas": 1,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": {"app": name}},
                "spec": {
                    "containers": [
                        {"name": "worker", "image": "example.invalid/worker:test"}
                    ]
                },
            },
        }
    return result


# --- Field ownership model (opt-in: ``FakeAPI.field_ownership = True``) -------
#
# A small model of server-side apply bookkeeping: field paths are tuples of
# FieldsV1 segments (``f:name``; ``k:{"name":...}`` for lists of named items).
# Only ``metadata.labels``/``metadata.annotations`` and non-metadata content
# are tracked. It reproduces what the takeover code relies on: apply conflicts
# on differing values owned by another manager (``force=true`` moves them),
# Update ownership of changed fields, and managedFields replacement through a
# non-apply patch.

Path = tuple[str, ...]
_UNTRACKED_METADATA = {
    "name",
    "namespace",
    "uid",
    "resourceVersion",
    "generation",
    "managedFields",
    "creationTimestamp",
}


def _segments(value: Any, prefix: Path = ()) -> set[Path]:
    paths: set[Path] = set()
    if isinstance(value, dict) and value:
        for key, child in value.items():
            if not prefix and key in {"status", "apiVersion", "kind"}:
                continue
            if prefix == ("f:metadata",) and key in _UNTRACKED_METADATA:
                continue
            paths |= _segments(child, prefix + (f"f:{key}",))
    elif (
        isinstance(value, list)
        and value
        and all(isinstance(item, dict) and "name" in item for item in value)
    ):
        for item in value:
            key = "k:" + json.dumps({"name": item["name"]}, separators=(",", ":"))
            paths.add(prefix + (key, "f:name"))
            for name, child in item.items():
                if name != "name":
                    paths |= _segments(child, prefix + (key, f"f:{name}"))
    elif prefix:
        paths.add(prefix)
    return paths


def field_paths(manifest: dict[str, Any]) -> set[Path]:
    """The FieldsV1 paths of ``manifest``'s content (a field-ownership set)."""
    return {path for path in _segments(manifest) if path != ("f:metadata",)}


def fields_v1(paths: set[Path]) -> dict[str, Any]:
    """A ``fieldsV1`` tree (as in ``managedFields``) from a set of paths."""
    tree: dict[str, Any] = {}
    for path in sorted(paths):
        node = tree
        for segment in path:
            node = node.setdefault(segment, {})
    return tree


def paths_of(fields: dict[str, Any], prefix: Path = ()) -> set[Path]:
    """The paths of a ``fieldsV1`` tree; the inverse of :func:`fields_v1`."""
    paths: set[Path] = set()
    for key, child in fields.items():
        if key == ".":
            continue
        if isinstance(child, dict) and child:
            paths |= paths_of(child, prefix + (key,))
        else:
            paths.add(prefix + (key,))
    return paths


def value_at(manifest: Any, path: Path) -> Any:
    """The value at a FieldsV1 ``path`` of ``manifest`` (a private sentinel if absent)."""
    node = manifest
    for segment in path:
        kind, _, name = segment.partition(":")
        if kind == "f" and isinstance(node, dict) and name in node:
            node = node[name]
        elif kind == "k" and isinstance(node, list):
            key = json.loads(name)
            node = next(
                (
                    item
                    for item in node
                    if all(item.get(k) == v for k, v in key.items())
                ),
                None,
            )
            if node is None:
                return _MISSING
        else:
            return _MISSING
    return node


_MISSING = object()


def _ssa_merge(current: Any, applied: Any) -> Any:
    if isinstance(current, dict) and isinstance(applied, dict):
        result = copy.deepcopy(current)
        for key, value in applied.items():
            result[key] = _ssa_merge(current.get(key), value)
        return result
    if (
        isinstance(current, list)
        and isinstance(applied, list)
        and all(isinstance(item, dict) and "name" in item for item in current + applied)
    ):
        by_name = {item["name"]: item for item in current}
        merged = [_ssa_merge(by_name.get(item["name"]), item) for item in applied]
        names = {item["name"] for item in applied}
        return merged + [item for item in current if item["name"] not in names]
    return copy.deepcopy(applied)


def _remove_path(manifest: dict[str, Any], path: Path) -> None:
    parent = value_at(manifest, path[:-1]) if len(path) > 1 else manifest
    kind, _, name = path[-1].partition(":")
    if kind == "f" and isinstance(parent, dict):
        parent.pop(name, None)
    elif kind == "k" and isinstance(parent, list):
        key = json.loads(name)
        parent[:] = [
            item for item in parent if not all(item.get(k) == v for k, v in key.items())
        ]


def _prune(manifest: dict[str, Any], removed: set[Path], kept: set[Path]) -> None:
    """SSA pruning: drop fields nobody owns any more; whole list items too."""
    items = {
        path[: index + 1]
        for path in removed
        for index, segment in enumerate(path)
        if segment.startswith("k:")
    }
    for item in sorted(items, key=len):
        if not any(path[: len(item)] == item for path in kept):
            _remove_path(manifest, item)
    for path in removed:
        if value_at(manifest, path) is not _MISSING:
            _remove_path(manifest, path)


# Spec fields the API server refuses to change on an existing object.
_IMMUTABLE_SPEC = {
    "Job": ("template", "completions", "completionMode", "selector"),
    "StatefulSet": (
        "selector",
        "serviceName",
        "podManagementPolicy",
        "volumeClaimTemplates",
    ),
}


def _validation_error(body: Any, current: Any = None) -> str | None:
    """The API server's validation rules the fake models (a small subset).

    Besides a Deployment's strategy, an update may not change the immutable
    spec fields of a Job or StatefulSet (``field is immutable``).
    """
    if not isinstance(body, dict):
        return None
    if isinstance(current, dict) and body.get("kind") in _IMMUTABLE_SPEC:
        before, after = current.get("spec") or {}, body.get("spec") or {}
        for key in _IMMUTABLE_SPEC[body["kind"]]:
            if before.get(key) != after.get(key):
                return f"spec.{key}: Invalid value: field is immutable"
    if body.get("kind") != "Deployment":
        return None
    strategy = (body.get("spec") or {}).get("strategy") or {}
    if strategy.get("type") == "Recreate" and strategy.get("rollingUpdate"):
        return (
            "spec.strategy.rollingUpdate: Forbidden: may not be specified when "
            "strategy `type` is 'Recreate'"
        )
    return None


# --- Server defaulting (opt-in: ``FakeAPI.server_defaults = True``) ----------
#
# What a real API server adds to every stored object, so that a live object
# differs from the manifest that created it: defaulted fields, canonical
# quantities (``cpu: 0.5`` is stored as ``500m``), allocated values kept across
# updates (a Service's clusterIP) and controller bookkeeping (a Deployment's
# revision annotation, a claim's protection finalizer). Applied to every write,
# dry runs included, like the real admission chain.

_POD_DEFAULTS = {
    "restartPolicy": "Always",
    "dnsPolicy": "ClusterFirst",
    "schedulerName": "default-scheduler",
    "securityContext": {},
    "terminationGracePeriodSeconds": 30,
}
_CONTAINER_DEFAULTS = {
    "imagePullPolicy": "IfNotPresent",
    "terminationMessagePath": "/dev/termination-log",
    "terminationMessagePolicy": "File",
}


def _cpu(value: Any) -> Any:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    return (
        f"{round(number * 1000)}m" if number < 1 or not number.is_integer() else value
    )


def apply_server_defaults(body: dict[str, Any], current: dict[str, Any] | None) -> None:
    kind = body.get("kind")
    if kind not in {"Deployment", "PersistentVolumeClaim", "Service"}:
        return
    spec = body.setdefault("spec", {})
    metadata = body["metadata"]
    if kind == "Deployment":
        spec.setdefault(
            "strategy",
            {
                "type": "RollingUpdate",
                "rollingUpdate": {"maxSurge": "25%", "maxUnavailable": "25%"},
            },
        )
        spec.setdefault("revisionHistoryLimit", 10)
        spec.setdefault("progressDeadlineSeconds", 600)
        template = spec.setdefault("template", {})
        template.setdefault("metadata", {}).setdefault("creationTimestamp", None)
        pod = template.setdefault("spec", {})
        for key, value in _POD_DEFAULTS.items():
            pod.setdefault(key, copy.deepcopy(value))
        for container in pod.get("containers", []):
            for key, value in _CONTAINER_DEFAULTS.items():
                container.setdefault(key, value)
            for section in container.get("resources", {}).values():
                if isinstance(section, dict) and "cpu" in section:
                    section["cpu"] = _cpu(section["cpu"])
        annotations = metadata.setdefault("annotations", {})
        annotations.setdefault(
            "deployment.kubernetes.io/revision",
            (current or {})
            .get("metadata", {})
            .get("annotations", {})
            .get("deployment.kubernetes.io/revision", "1"),
        )
    elif kind == "PersistentVolumeClaim":
        spec.setdefault("volumeMode", "Filesystem")
        spec.setdefault("storageClassName", "standard")
        finalizers = metadata.setdefault("finalizers", [])
        if "kubernetes.io/pvc-protection" not in finalizers:
            finalizers.append("kubernetes.io/pvc-protection")
    elif kind == "Service":
        previous = (current or {}).get("spec", {})
        address = spec.get("clusterIP") or previous.get("clusterIP") or "10.96.0.10"
        spec.update({"clusterIP": address, "clusterIPs": [address]})
        spec.setdefault("type", "ClusterIP")
        spec.setdefault("sessionAffinity", "None")
        spec.setdefault("ipFamilies", ["IPv4"])
        spec.setdefault("ipFamilyPolicy", "SingleStack")
        spec.setdefault("internalTrafficPolicy", "Cluster")
        for port in spec.get("ports", []):
            port.setdefault("protocol", "TCP")
            port.setdefault("targetPort", port.get("port"))


class FakeAPI:
    """The state and behaviour of one fake API server.

    Attributes a test reads or sets:

    - ``objects``: ``{(kind, name): manifest}``, the stored objects;
    - ``requests``: every request received (method, path, query, body,
      content type), in order;
    - ``field_ownership``: opt in to the server-side-apply field model;
    - ``ready``: whether Deployments, StatefulSets and DaemonSets report
      ready pods and Jobs report completion;
    - ``wait_for_first_consumer``: claim names that stay ``Pending`` until a
      workload mounts them;
    - ``types``: the served resources (default :data:`TYPES`);
    - ``terminating_reads``: when above ``0``, an ``Orphan`` delete keeps the
      object (with a ``deletionTimestamp`` and the ``orphan`` finalizer) for
      that many more reads of it, as a real API server does until its
      garbage collector removes the finalizer.
    - ``nodes``: ``{name: Node manifest}`` served at ``/api/v1/nodes/NAME``
      (read-only, not part of discovery); add one with :meth:`add_node`.
    - ``pod_failures``: workloads whose pods cannot start (see
      :meth:`fail_pods`); ``pod_logs`` ``{(pod, container, previous):
      text}`` served at ``pods/NAME/log``; ``events``: Event objects served
      at ``/api/v1/namespaces/NS/events`` (``fieldSelector`` on
      ``involvedObject.kind``/``name``).

    - ``intercept``: an optional ``(request, phase) -> bool`` called for
      every request with ``phase`` ``"received"`` (before the server acts on
      it) and ``"committed"`` (after it acted, before the response). Returning
      true drops the connection at that point: a ``"received"`` request is
      then never applied, a ``"committed"`` one is applied but unanswered.
      Kill tests use it to stop a client process at an exact step.

    Use :meth:`put` to seed objects and :meth:`inject` to add faults.
    """

    def __init__(
        self, types: Mapping[str, tuple[str, str, bool]] | None = None
    ) -> None:
        self.types: dict[str, tuple[str, str, bool]] = dict(
            TYPES if types is None else types
        )
        self.field_ownership = False
        self.server_defaults = False
        self.objects: dict[tuple[str, str], dict[str, Any]] = {}
        self.requests: list[dict[str, Any]] = []
        self.faults: list[dict[str, Any]] = []
        self.lock = threading.RLock()
        self.ready = True
        self.wait_for_first_consumer: set[str] = set()
        self.terminating_reads = 0
        self._terminating: dict[tuple[str, str], int] = {}
        self.version = 1
        self.nodes: dict[str, dict[str, Any]] = {}
        self.intercept: Callable[[dict[str, Any], str], bool] | None = None
        self.pod_failures: dict[str, dict[str, Any]] = {}
        self.pod_logs: dict[tuple[str, str, bool], str] = {}
        self.events: list[dict[str, Any]] = []
        self.put(manifest("Namespace", "kube-system"), uid="cluster-uid")
        self.put(manifest("Namespace", TARGET.namespace), uid="namespace-uid")

    def put(
        self,
        value: dict[str, Any],
        *,
        uid: str | None = None,
        owned: bool = False,
        managers: list[tuple[str, str, dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        """Store an object; ``managers`` = (manager, operation, owned subset)."""
        stored = self._put(value, uid=uid, owned=owned)
        if managers is not None:
            with self.lock:
                current = self.objects[(value["kind"], value["metadata"]["name"])]
                current["metadata"]["managedFields"] = [
                    {
                        "manager": manager,
                        "operation": operation,
                        "apiVersion": value["apiVersion"],
                        "fieldsType": "FieldsV1",
                        "fieldsV1": fields_v1(field_paths(subset)),
                    }
                    for manager, operation, subset in managers
                ]
                stored = copy.deepcopy(current)
        return stored

    def add_node(
        self, name: str, *, architecture: str = "arm64", uid: str | None = None
    ) -> dict[str, Any]:
        """Serve a Node (identity and ``status.nodeInfo``) for node-pinned releases."""
        node = {
            "apiVersion": "v1",
            "kind": "Node",
            "metadata": {"name": name, "uid": uid or uuid.uuid4().hex},
            "status": {
                "nodeInfo": {"architecture": architecture, "operatingSystem": "linux"}
            },
        }
        with self.lock:
            self.nodes[name] = node
        return copy.deepcopy(node)

    def scale(
        self,
        kind: str,
        name: str,
        replicas: int,
        *,
        manager: str = "kube-controller-manager",
    ) -> dict[str, Any]:
        """Write ``spec.replicas`` through the ``scale`` subresource.

        What a HorizontalPodAutoscaler does: ``manager`` gets an ``Update``
        entry with ``subresource: scale`` owning ``spec.replicas``, which no
        other entry owns afterwards.
        """
        with self.lock:
            current = self.objects[(kind, name)]
            self.version += 1
            current.setdefault("spec", {})["replicas"] = replicas
            metadata = current["metadata"]
            metadata["resourceVersion"] = str(self.version)
            metadata["generation"] = metadata.get("generation", 1) + 1
            owned: set[Path] = {("f:spec", "f:replicas")}
            entries = []
            for entry in metadata.get("managedFields", []):
                if entry.get("subresource") == "scale":
                    continue
                entry["fieldsV1"] = fields_v1(paths_of(entry["fieldsV1"]) - owned)
                if entry["fieldsV1"]:
                    entries.append(entry)
            entries.append(
                {
                    "manager": manager,
                    "operation": "Update",
                    "subresource": "scale",
                    "apiVersion": current["apiVersion"],
                    "fieldsType": "FieldsV1",
                    "fieldsV1": fields_v1(owned),
                }
            )
            metadata["managedFields"] = entries
            self._readiness(current)
            return copy.deepcopy(current)

    def fail_pods(
        self,
        workload: str,
        *,
        reason: str | None = "CrashLoopBackOff",
        exit_code: int | None = 1,
        restarts: int = 1,
        logs: str = "",
        message: str = "",
        container: str | None = None,
        init: bool = False,
        events: tuple[tuple[str, str], ...] = (),
    ) -> None:
        """Make the pods of the Deployment/StatefulSet/DaemonSet/Job ``workload`` fail.

        When the executor reads the workload (with :attr:`ready` false) the
        fake creates what the controllers would: for a Deployment a
        ReplicaSet of its current revision, and one pod owned by it (or by
        the workload), whose container ``container`` (default: the first of
        the template) waits with ``reason`` (``None``: running) after
        ``restarts`` restarts, the last one exiting ``exit_code``. ``logs``
        is the previous instance's log (the current one when ``restarts`` is
        0); ``events`` are ``(reason, message)`` Warning events of the pod.
        """
        with self.lock:
            self.pod_failures[workload] = {
                "reason": reason,
                "exit_code": exit_code,
                "restarts": restarts,
                "logs": logs,
                "message": message,
                "container": container,
                "init": init,
                "events": tuple(events),
            }

    def _materialize_pods(self, value: dict[str, Any]) -> None:
        """Create the failing pods of a workload registered with :meth:`fail_pods`."""
        kind = value["kind"]
        metadata = value["metadata"]
        failure = self.pod_failures.get(metadata["name"])
        if failure is None or kind not in {
            "Deployment",
            "StatefulSet",
            "DaemonSet",
            "Job",
        }:
            return
        spec = value.get("spec") or {}
        template = spec.get("template") or {}
        labels = dict((template.get("metadata") or {}).get("labels") or {})
        digest = hashlib.sha256(
            json.dumps(template, sort_keys=True).encode()
        ).hexdigest()[:10]
        owner = {"kind": kind, "name": metadata["name"], "uid": metadata["uid"]}
        if kind == "Deployment":
            revision = str(metadata.get("generation", 1))
            metadata.setdefault("annotations", {})[
                "deployment.kubernetes.io/revision"
            ] = revision
            rs_name = f"{metadata['name']}-{digest}"
            if ("ReplicaSet", rs_name) not in self.objects:
                self.objects[("ReplicaSet", rs_name)] = {
                    "apiVersion": "apps/v1",
                    "kind": "ReplicaSet",
                    "metadata": {
                        "name": rs_name,
                        "namespace": TARGET.namespace,
                        "uid": uuid.uuid4().hex,
                        "resourceVersion": str(self.version),
                        "labels": {**labels, "pod-template-hash": digest},
                        "annotations": {"deployment.kubernetes.io/revision": revision},
                        "ownerReferences": [owner],
                    },
                }
            rs = self.objects[("ReplicaSet", rs_name)]["metadata"]
            owner = {"kind": "ReplicaSet", "name": rs_name, "uid": rs["uid"]}
            labels["pod-template-hash"] = digest
        elif kind == "StatefulSet":
            value.setdefault("status", {})["updateRevision"] = (
                f"{metadata['name']}-{digest}"
            )
            labels["controller-revision-hash"] = f"{metadata['name']}-{digest}"
        elif kind == "DaemonSet":
            labels["pod-template-generation"] = str(metadata.get("generation", 1))
        pod_name = f"{metadata['name']}-{digest}-x1"
        if ("Pod", pod_name) in self.objects:
            return
        pod_spec = template.get("spec") or {}
        containers = [
            str(item.get("name"))
            for item in pod_spec.get(
                "initContainers" if failure["init"] else "containers"
            )
            or ()
            if isinstance(item, dict)
        ] or ["main"]
        name = failure["container"] or containers[0]
        terminated_reason = "Error"
        last = (
            {
                "terminated": {
                    "exitCode": failure["exit_code"],
                    "reason": terminated_reason,
                }
            }
            if failure["exit_code"] is not None and failure["restarts"]
            else {}
        )
        state: dict[str, Any] = (
            {"waiting": {"reason": failure["reason"], "message": failure["message"]}}
            if failure["reason"]
            else {"running": {}}
        )
        status = {
            "name": name,
            "ready": False,
            "restartCount": failure["restarts"],
            "state": state,
            "lastState": last,
            "image": "example.test/app:1",
        }
        self.version += 1
        self.objects[("Pod", pod_name)] = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": pod_name,
                "namespace": TARGET.namespace,
                "uid": uuid.uuid4().hex,
                "resourceVersion": str(self.version),
                "labels": labels,
                "ownerReferences": [owner],
            },
            "spec": copy.deepcopy(pod_spec),
            "status": {
                "phase": "Pending" if failure["init"] else "Running",
                "initContainerStatuses" if failure["init"] else "containerStatuses": [
                    status
                ],
            },
        }
        if failure["logs"]:
            self.pod_logs[(pod_name, name, bool(failure["restarts"]))] = failure["logs"]
        for index, (reason, message) in enumerate(failure["events"]):
            self.events.append(
                {
                    "apiVersion": "v1",
                    "kind": "Event",
                    "metadata": {"name": f"{pod_name}.{index}"},
                    "involvedObject": {"kind": "Pod", "name": pod_name},
                    "type": "Warning",
                    "reason": reason,
                    "message": message,
                    "count": 1,
                    "lastTimestamp": f"2026-01-01T00:00:{index:02d}Z",
                }
            )

    def _serve_pod_extras(
        self, path: str, method: str, query: dict[str, Any]
    ) -> tuple[int, Any] | None:
        """``pods/NAME/log`` (text) and the namespace's ``events``, or ``None``."""
        base = f"/api/v1/namespaces/{TARGET.namespace}/"
        if method != "GET" or not path.startswith(base):
            return None
        rest = [unquote(part) for part in path[len(base) :].split("/")]
        if rest == ["events"]:
            wanted = dict(
                term.split("=", 1)
                for term in query.get("fieldSelector", [""])[0].split(",")
                if "=" in term
            )
            items = [
                copy.deepcopy(event)
                for event in self.events
                if all(
                    str(event.get("involvedObject", {}).get(key.split(".", 1)[1]))
                    == value
                    for key, value in wanted.items()
                    if key.startswith("involvedObject.")
                )
            ]
            return 200, {
                "apiVersion": "v1",
                "kind": "EventList",
                "metadata": {"resourceVersion": str(self.version)},
                "items": items,
            }
        if len(rest) == 3 and rest[0] == "pods" and rest[2] == "log":
            if ("Pod", rest[1]) not in self.objects:
                return 404, {}
            container = query.get("container", [""])[0]
            previous = query.get("previous", ["false"])[0] == "true"
            text = self.pod_logs.get((rest[1], container, previous))
            if text is None:
                return (400, {}) if previous else (200, b"")
            tail = int(query.get("tailLines", [0])[0] or 0)
            lines = text.splitlines(keepends=True)
            if tail:
                lines = lines[-tail:]
            return 200, "".join(lines).encode()
        return None

    def managers(self, kind: str, name: str) -> dict[str, set[Path]]:
        """Owned field paths by ``manager/operation`` (status excluded)."""
        entries = self.objects[(kind, name)]["metadata"].get("managedFields", [])
        return {
            f"{entry['manager']}/{entry['operation']}": paths_of(entry["fieldsV1"])
            for entry in entries
            if entry.get("subresource") != "status"
        }

    def _put(
        self, value: dict[str, Any], *, uid: str | None = None, owned: bool = False
    ) -> dict[str, Any]:
        with self.lock:
            value = copy.deepcopy(value)
            self.version += 1
            metadata = value["metadata"]
            metadata.update(
                {
                    "uid": uid or uuid.uuid4().hex,
                    "resourceVersion": str(self.version),
                    "generation": 1,
                }
            )
            if owned:
                metadata.setdefault("annotations", {})["piceli.io/owner"] = (
                    "acceptance-owner"
                )
            metadata["managedFields"] = [
                {
                    "manager": "piceli-acceptance" if owned else "other",
                    "operation": "Apply",
                    "apiVersion": value["apiVersion"],
                    "fieldsType": "FieldsV1",
                    "fieldsV1": {"f:spec": {}},
                }
            ]
            self.objects[(value["kind"], metadata["name"])] = value
            self._readiness(value)
            return copy.deepcopy(value)

    def _readiness(self, value: dict[str, Any]) -> None:
        if value["kind"] in {"Deployment", "StatefulSet"}:
            count = value.get("spec", {}).get("replicas", 1)
            value["status"] = {
                "observedGeneration": value["metadata"]["generation"],
                "readyReplicas": count if self.ready else 0,
                "updatedReplicas": count,
                "replicas": count,
            }
        elif value["kind"] == "DaemonSet":
            value["status"] = {
                "observedGeneration": value["metadata"]["generation"],
                "desiredNumberScheduled": 1,
                "numberReady": 1 if self.ready else 0,
                "updatedNumberScheduled": 1,
            }
        elif value["kind"] == "Job":
            value["status"] = {
                "conditions": [{"type": "Complete", "status": "True"}]
                if self.ready
                else []
            }
        elif value["kind"] == "Namespace":
            value["status"] = {"phase": "Active"}
        elif value["kind"] == "PersistentVolumeClaim":
            name = value["metadata"]["name"]
            consumed = any(
                name
                in {
                    volume.get("persistentVolumeClaim", {}).get("claimName")
                    for volume in deployment.get("spec", {})
                    .get("template", {})
                    .get("spec", {})
                    .get("volumes", [])
                    if isinstance(volume, dict)
                }
                for (kind, _), deployment in self.objects.items()
                if kind in {"Deployment", "StatefulSet", "DaemonSet", "Job", "Pod"}
            )
            value["status"] = {
                "phase": "Bound"
                if name not in self.wait_for_first_consumer or consumed
                else "Pending"
            }
            metadata = value["metadata"]
            if (
                name in self.wait_for_first_consumer
                and consumed
                and "uid" in metadata
                and not value.get("spec", {}).get("volumeName")
            ):
                # Binding a WaitForFirstConsumer claim writes server-owned
                # fields, as the scheduler and PV controller do.
                value.setdefault("spec", {})["volumeName"] = "pvc-" + metadata["uid"]
                metadata.setdefault("annotations", {}).update(
                    {
                        "pv.kubernetes.io/bind-completed": "yes",
                        "pv.kubernetes.io/bound-by-controller": "yes",
                        "volume.kubernetes.io/selected-node": "node-a",
                        "volume.kubernetes.io/storage-provisioner": "local-path",
                        "volume.beta.kubernetes.io/storage-provisioner": "local-path",
                    }
                )
                metadata["managedFields"] = list(metadata.get("managedFields", [])) + [
                    {
                        "manager": "kube-controller-manager",
                        "operation": "Update",
                        "apiVersion": "v1",
                        "fieldsType": "FieldsV1",
                        "fieldsV1": {
                            "f:metadata": {
                                "f:annotations": {
                                    "f:pv.kubernetes.io/bind-completed": {},
                                    "f:pv.kubernetes.io/bound-by-controller": {},
                                }
                            },
                            "f:spec": {"f:volumeName": {}},
                        },
                    },
                    {
                        "manager": "kube-scheduler",
                        "operation": "Update",
                        "apiVersion": "v1",
                        "fieldsType": "FieldsV1",
                        "fieldsV1": {
                            "f:metadata": {
                                "f:annotations": {
                                    "f:volume.kubernetes.io/selected-node": {}
                                }
                            }
                        },
                    },
                ]
                self.version += 1
                metadata["resourceVersion"] = str(self.version)
        elif value["kind"] == "PersistentVolume":
            value["status"] = {"phase": "Bound"}
        elif value["kind"] == "Service":
            # The API server allocates a cluster IP and fills IP families.
            spec = value.setdefault("spec", {})
            if spec.get("type") != "ExternalName" and not spec.get("clusterIP"):
                digest = hashlib.sha256(value["metadata"]["name"].encode()).digest()
                address = f"10.96.{digest[0]}.{digest[1] or 1}"
                spec["clusterIP"] = address
                spec["clusterIPs"] = [address]
                spec.setdefault("ipFamilies", ["IPv4"])
                spec.setdefault("ipFamilyPolicy", "SingleStack")

    def inject(self, method: str, path_suffix: str, **fault: Any) -> None:
        """Fail the next ``method`` request whose path ends with ``path_suffix``.

        ``fault`` keys: ``status`` (HTTP error), ``raw`` (body bytes),
        ``delay`` / ``after_commit_delay`` (seconds), ``disconnect_before`` /
        ``disconnect_after`` (drop the connection), ``dry_run`` (match only
        dry-run or only real requests). Each fault fires once.
        """
        self.faults.append({"method": method, "suffix": path_suffix, **fault})

    def handler(self) -> type[BaseHTTPRequestHandler]:
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: Any) -> None:
                pass

            def do_GET(self) -> None:
                self.handle_request()

            def do_POST(self) -> None:
                self.handle_request()

            def do_PATCH(self) -> None:
                self.handle_request()

            def do_DELETE(self) -> None:
                self.handle_request()

            def handle_request(self) -> None:
                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query)
                body = json.loads(
                    self.rfile.read(int(self.headers.get("Content-Length", "0")))
                    or b"null"
                )
                request = {
                    "method": self.command,
                    "path": parsed.path,
                    "query": query,
                    "body": body,
                    "content_type": self.headers.get("Content-Type"),
                    "authorization": self.headers.get("Authorization"),
                }
                with api.lock:
                    api.requests.append(request)
                    fault = next(
                        (
                            item
                            for item in api.faults
                            if item["method"] == self.command
                            and parsed.path.endswith(item["suffix"])
                            and (
                                "dry_run" not in item
                                or item["dry_run"] == (query.get("dryRun") == ["All"])
                            )
                        ),
                        {},
                    )
                    if fault:
                        api.faults.remove(fault)
                if fault.get("delay"):
                    time.sleep(fault["delay"])
                if "status" in fault:
                    self.respond(
                        fault["status"], {"message": "server-password-do-not-publish"}
                    )
                    return
                if "raw" in fault:
                    self.respond(200, None, raw=fault["raw"])
                    return
                if fault.get("disconnect_before") or (
                    api.intercept is not None and api.intercept(request, "received")
                ):
                    self.connection.close()
                    return
                with api.lock:
                    status, response = api.route(request)
                if api.intercept is not None and api.intercept(request, "committed"):
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                if fault.get("after_commit_delay"):
                    time.sleep(fault["after_commit_delay"])
                if fault.get("disconnect_after"):
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                self.respond(status, response)

            def respond(
                self, status: int, value: Any, *, raw: bytes | None = None
            ) -> None:
                text = isinstance(value, bytes)
                if isinstance(value, bytes):
                    raw = value
                encoded = raw if raw is not None else json.dumps(value).encode()
                try:
                    self.send_response(status)
                    self.send_header(
                        "Content-Type", "text/plain" if text else "application/json"
                    )
                    self.send_header("Content-Length", str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        return Handler

    def route(self, request: dict[str, Any]) -> tuple[int, Any]:
        path = request["path"]
        method = request["method"]
        query = request["query"]
        for version in sorted({item[0] for item in self.types.values()}):
            root = ("/apis/" if "/" in version else "/api/") + version
            if path == root:
                return 200, {
                    "kind": "APIResourceList",
                    "groupVersion": version,
                    "resources": [
                        {
                            "name": plural,
                            "kind": kind,
                            "namespaced": namespaced,
                            "verbs": ["get", "list", "create", "patch", "delete"],
                        }
                        for plural, (api, kind, namespaced) in self.types.items()
                        if api == version
                    ],
                }
        extra = self._serve_pod_extras(path, method, query)
        if extra is not None:
            return extra
        if path.startswith("/api/v1/nodes/") and method == "GET":
            node = self.nodes.get(path.rsplit("/", 1)[1])
            return (200, copy.deepcopy(node)) if node is not None else (404, {})
        # Like the API server, decode each path segment: RBAC names may hold
        # ':' (sent as %3A).
        parts = [unquote(part) for part in path.split("/")]
        found = [
            (index, self.types[part])
            for index, part in enumerate(parts)
            if part in self.types
        ]
        if not found:
            return 404, {}
        index, (api_version, kind, namespaced) = found[-1]
        if namespaced and parts[index - 2 : index] != ["namespaces", TARGET.namespace]:
            return 403, {}
        name = parts[index + 1] if len(parts) > index + 1 else ""
        current = self.objects.get((kind, name))
        if method == "GET":
            if name:
                remaining = self._terminating.get((kind, name))
                if remaining is not None:
                    if remaining <= 0:
                        del self._terminating[(kind, name)]
                        del self.objects[(kind, name)]
                        self.version += 1
                        current = None
                    else:
                        self._terminating[(kind, name)] = remaining - 1
                if current is None:
                    return 404, {}
                self._readiness(current)
                if not self.ready:
                    self._materialize_pods(current)
                return 200, copy.deepcopy(current)
            selector = query.get("labelSelector", [""])[0]
            values = sorted(
                [
                    copy.deepcopy(item)
                    for (item_kind, _), item in self.objects.items()
                    if item_kind == kind and _selected(item, selector)
                ],
                key=lambda value: value["metadata"]["name"],
            )
            limit = int(query.get("limit", [100])[0])
            start = int(query.get("continue", [0])[0])
            continuation = str(start + limit) if len(values) > start + limit else ""
            return 200, {
                "apiVersion": api_version,
                "kind": kind + "List",
                "metadata": {
                    "resourceVersion": str(self.version),
                    "continue": continuation,
                },
                "items": values[start : start + limit],
            }
        body = copy.deepcopy(request["body"])
        if method == "DELETE":
            if current is None:
                return 404, {}
            if body.get("preconditions") != {
                "uid": current["metadata"]["uid"],
                "resourceVersion": current["metadata"]["resourceVersion"],
            } or body.get("propagationPolicy") not in {"Orphan", "Background"}:
                return 409, {}
            # Like the API server: with a body, DeleteOptions come from the
            # body only (a dryRun query parameter alone would delete).
            if body.get("dryRun") == ["All"]:
                return 200, {"kind": "Status", "status": "Success"}
            if self.terminating_reads > 0 and body["propagationPolicy"] == "Orphan":
                self.version += 1
                current["metadata"].update(
                    {
                        "deletionTimestamp": "2026-01-01T00:00:00Z",
                        "finalizers": ["orphan"],
                        "resourceVersion": str(self.version),
                    }
                )
                self._terminating[(kind, name)] = self.terminating_reads
                return 200, copy.deepcopy(current)
            del self.objects[(kind, name)]
            self.version += 1
            return 200, {"kind": "Status", "status": "Success"}
        metadata = body["metadata"]
        # A patch may omit metadata.name; the URL names the object.
        name = str(metadata.get("name") or name)
        current = self.objects.get((kind, name))
        if method == "POST" and current is not None:
            return 409, {}
        if method == "PATCH":
            if (
                current is None
                or metadata.get("uid") != current["metadata"]["uid"]
                or metadata.get("resourceVersion")
                != current["metadata"]["resourceVersion"]
            ):
                return 409, {}
            apply_patch = request["content_type"] == "application/apply-patch+yaml"
            merge_patch = request["content_type"] == "application/merge-patch+json"
            if self.field_ownership and (apply_patch or merge_patch):
                return self._owned_patch(
                    request, current, body, kind, api_version, merge_patch
                )
            if not (apply_patch or merge_patch) or (
                apply_patch and query.get("force") != ["false"]
            ):
                return 422, {}
            if merge_patch:
                body = _merge_patch(current, body)
                metadata = body["metadata"]
        if self.server_defaults:
            apply_server_defaults(body, current)
        if self.field_ownership and method == "POST":
            metadata["managedFields"] = [
                {
                    "manager": query["fieldManager"][0],
                    "operation": "Update",
                    "apiVersion": api_version,
                    "fieldsType": "FieldsV1",
                    "fieldsV1": fields_v1(field_paths(body)),
                }
            ]
            metadata.update(
                {"uid": uuid.uuid4().hex, "resourceVersion": str(self.version + 1)}
            )
            metadata["generation"] = 1
            return self._commit(query, kind, name, body, 201)
        metadata.update(
            {
                "uid": current["metadata"]["uid"] if current else uuid.uuid4().hex,
                "resourceVersion": str(self.version + 1),
                "generation": current["metadata"].get("generation", 0) + 1
                if current
                else 1,
                "managedFields": [
                    {
                        "manager": query["fieldManager"][0],
                        "operation": "Apply" if query.get("force") else "Update",
                        "apiVersion": api_version,
                        "fieldsType": "FieldsV1",
                        "fieldsV1": {"f:spec": {}},
                    }
                ],
            }
        )
        invalid = _validation_error(body, current)
        if invalid is not None:
            return 422, {"kind": "Status", "message": invalid}
        self._readiness(body)
        if query.get("dryRun") != ["All"]:
            self.version += 1
            self.objects[(kind, name)] = body
            for (existing_kind, _), existing in self.objects.items():
                if existing_kind == "PersistentVolumeClaim":
                    self._readiness(existing)
        return 201 if method == "POST" else 200, copy.deepcopy(body)

    def _commit(
        self, query: dict[str, Any], kind: str, name: str, body: Any, status: int
    ) -> tuple[int, Any]:
        invalid = _validation_error(body, self.objects.get((kind, name)))
        if invalid is not None:
            return 422, {"kind": "Status", "message": invalid}
        if self.server_defaults:
            apply_server_defaults(body, self.objects.get((kind, name)))
        self._readiness(body)
        if query.get("dryRun") != ["All"]:
            self.version += 1
            self.objects[(kind, name)] = body
            for (existing_kind, _), existing in self.objects.items():
                if existing_kind == "PersistentVolumeClaim":
                    self._readiness(existing)
        return status, copy.deepcopy(body)

    def _owned_patch(
        self,
        request: dict[str, Any],
        current: dict[str, Any],
        body: dict[str, Any],
        kind: str,
        api_version: str,
        merge_patch: bool,
    ) -> tuple[int, Any]:
        query = request["query"]
        manager = query["fieldManager"][0]
        entries = copy.deepcopy(current["metadata"].get("managedFields", []))
        if merge_patch:
            provided = body["metadata"].pop("managedFields", None)
            result = _merge_patch(current, body)
            if provided:
                entries = copy.deepcopy(provided)
            else:
                before, after = field_paths(current), field_paths(result)
                changed = {
                    path
                    for path in after
                    if value_at(current, path) != value_at(result, path)
                } | (before - after)
                for entry in entries:
                    entry["fieldsV1"] = fields_v1(paths_of(entry["fieldsV1"]) - changed)
                own = next(
                    (
                        e
                        for e in entries
                        if e["manager"] == manager and e["operation"] == "Update"
                    ),
                    None,
                )
                owned = changed & after
                if owned:
                    if own is None:
                        own = {
                            "manager": manager,
                            "operation": "Update",
                            "apiVersion": api_version,
                            "fieldsType": "FieldsV1",
                            "fieldsV1": {},
                        }
                        entries.append(own)
                    own["fieldsV1"] = fields_v1(paths_of(own["fieldsV1"]) | owned)
        else:
            force = query.get("force") == ["true"]
            if query.get("force") not in (["true"], ["false"]):
                return 422, {}
            applied = field_paths(body)
            conflicts = False
            for entry in entries:
                if (entry["manager"], entry["operation"]) == (manager, "Apply"):
                    continue
                if entry.get("subresource") == "status":
                    continue
                owned = paths_of(entry["fieldsV1"])
                clash = {
                    path
                    for path in owned & applied
                    if value_at(current, path) != value_at(body, path)
                }
                if clash and not force:
                    conflicts = True
                entry["fieldsV1"] = fields_v1(owned - clash)
            if conflicts:
                return 409, {}
            result = _ssa_merge(current, body)
            own = next(
                (
                    e
                    for e in entries
                    if e["manager"] == manager and e["operation"] == "Apply"
                ),
                None,
            )
            if own is not None:
                others = set().union(
                    *(
                        paths_of(e["fieldsV1"])
                        for e in entries
                        if e is not own and e.get("subresource") != "status"
                    )
                )
                _prune(
                    result,
                    paths_of(own["fieldsV1"]) - applied - others,
                    applied | others,
                )
            else:
                own = {
                    "manager": manager,
                    "operation": "Apply",
                    "apiVersion": api_version,
                    "fieldsType": "FieldsV1",
                    "fieldsV1": {},
                }
                entries.append(own)
            own["fieldsV1"] = fields_v1(applied)
        result["metadata"]["managedFields"] = [
            entry for entry in entries if entry.get("fieldsV1")
        ]
        result["metadata"]["uid"] = current["metadata"]["uid"]
        result["metadata"]["resourceVersion"] = str(self.version + 1)
        changed_spec = {
            k: v for k, v in result.items() if k not in {"metadata", "status"}
        }
        before_spec = {
            k: v for k, v in current.items() if k not in {"metadata", "status"}
        }
        result["metadata"]["generation"] = current["metadata"].get("generation", 1) + (
            1 if changed_spec != before_spec else 0
        )
        return self._commit(query, kind, result["metadata"]["name"], result, 200)


@contextmanager
def serve(api: FakeAPI | None = None) -> Iterator[tuple[FakeAPI, str]]:
    """Serve ``api`` (a new :class:`FakeAPI` by default) on a loopback port.

    Yields ``(api, url)`` where ``url`` is ``http://127.0.0.1:<port>``. The
    server stops when the block exits.
    """
    api = api or FakeAPI()
    server = ThreadingHTTPServer(("127.0.0.1", 0), api.handler())
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield api, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def provider_at(url: str, **kwargs: Any) -> KubernetesProvider:
    """A :class:`KubernetesProvider` for the fake server at ``url``.

    It targets :data:`TARGET` with the field manager ``piceli-acceptance`` and
    the owner id ``acceptance-owner``; ``kwargs`` override provider settings
    (for example ``inherited_owner_ids``). Close ``provider.client`` when done.
    """
    from kubernetes.client import ApiClient, Configuration

    configuration = Configuration()
    configuration.host = url
    configuration.proxy = None
    configuration.retries = 0
    client = ApiClient(configuration)
    return KubernetesProvider(
        client,
        target=TARGET,
        field_manager="piceli-acceptance",
        owner_id="acceptance-owner",
        source=EvidenceSource.LOOPBACK,
        cluster_uid="cluster-uid",
        namespace_uid="namespace-uid",
        **kwargs,
    )


def write_kubeconfig(url: str, path: FilePath, *, context: str = "fake") -> FilePath:
    """Write a kubeconfig for the fake server at ``url`` and return ``path``.

    The context (default ``fake``) has no credentials. Commands that read an
    explicit kubeconfig use it with the ``loopback-http`` transport (for
    ``piceli release``: ``[target] transport = "loopback-http"``; for
    ``piceli import live``: ``--transport loopback-http``).
    """
    path = FilePath(path)
    path.write_text(
        textwrap.dedent(
            f"""\
            apiVersion: v1
            kind: Config
            clusters: [{{name: {context}, cluster: {{server: "{url}"}}}}]
            users: [{{name: nobody, user: {{}}}}]
            contexts: [{{name: {context}, context: {{cluster: {context}, user: nobody}}}}]
            """
        )
    )
    return path


@dataclass
class FakeCluster:
    """A running fake server: the state, its URL and a ready provider.

    :param api: The server state (seed objects with ``api.put``).
    :param url: The loopback URL.
    :param provider: A provider bound to :data:`TARGET` (see :func:`provider_at`).
    """

    api: FakeAPI
    url: str
    provider: KubernetesProvider

    @property
    def namespace(self) -> str:
        """The namespace every namespaced object lives in."""
        return TARGET.namespace

    def kubeconfig(self, path: FilePath, *, context: str = "fake") -> FilePath:
        """Write a kubeconfig for this server (see :func:`write_kubeconfig`)."""
        return write_kubeconfig(self.url, path, context=context)


@contextmanager
def fake_cluster(
    api: FakeAPI | None = None, **provider_settings: Any
) -> Iterator[FakeCluster]:
    """Run a fake server and a provider for it for the duration of a block.

    The helper for a consumer's own tests::

        import pytest
        from piceli.testing import fake_cluster

        @pytest.fixture
        def cluster():
            with fake_cluster() as running:
                yield running

    or enable the ready-made fixture with
    ``pytest_plugins = ["piceli.testing.pytest_plugin"]``.
    """
    with serve(api) as (served, url):
        provider = provider_at(url, **provider_settings)
        try:
            yield FakeCluster(served, url, provider)
        finally:
            provider.client.close()


def _selected(value: dict[str, Any], selector: str) -> bool:
    """Whether ``value`` matches a label selector (``k``, ``!k``, ``k=v``, ``k!=v``)."""
    labels = value.get("metadata", {}).get("labels") or {}
    for term in filter(None, (item.strip() for item in selector.split(","))):
        if term.startswith("!"):
            if term[1:] in labels:
                return False
        elif "!=" in term:
            key, _, wanted = term.partition("!=")
            if labels.get(key) == wanted:
                return False
        elif "=" in term:
            key, _, wanted = term.partition("=")
            if labels.get(key) != wanted.lstrip("="):
                return False
        elif term not in labels:
            return False
    return True


def _merge_patch(current: Any, patch: Any) -> Any:
    """Apply the object subset needed by the fake Kubernetes merge-patch route."""
    if not isinstance(current, dict) or not isinstance(patch, dict):
        return copy.deepcopy(patch)
    result = copy.deepcopy(current)
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = _merge_patch(result.get(key), value)
    return result
