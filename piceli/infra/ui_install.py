"""The Piceli UI installed in the cluster by ``piceli cluster init`` (``Ui(access="forward")``).

:func:`render_ui` returns the UI's objects for a :class:`~piceli.infra.Cluster`.
They live in ``piceli-system`` next to the GitOps controller (which ``cluster
init`` installs, with the namespace, first):

- a single-replica Deployment running ``piceli ui forward-serve`` from a
  digest-pinned Piceli image (``Ui.image``, else ``Controller.image``), placed
  on ``Ui.on`` (else the controller's node). The server listens on the pod's
  loopback only; it is reached through ``piceli access ui``, which
  port-forwards to it and prints the one launch URL;
- a ClusterIP Service (the port-forward target). No NodePort, no Ingress and
  no OIDC: the launch token, which the UI writes into the Secret
  ``piceli-ui-launch`` when it starts, is the only way in;
- a ServiceAccount with read-only access to the controller's status
  ConfigMap and to the environments' workloads, pods and logs (never
  Secrets or ConfigMaps of the environments), plus write access to exactly
  two objects: the GitOps request inbox ``piceli-gitops-requests`` (the
  **Sync** button) and its own launch Secret.

Importing this module is side-effect free; rendering reads nothing.
"""

from __future__ import annotations

import re
from typing import Any

from piceli.infra import Cluster

__all__ = [
    "LAUNCH_SECRET",
    "NAME",
    "NAMESPACE",
    "PORT",
    "UiInstallError",
    "render_ui",
    "ui_image",
    "ui_node",
]

NAME = "piceli-ui"
NAMESPACE = "piceli-system"
#: The port the UI listens on in its pod (loopback) and on the laptop.
PORT = 8790
LAUNCH_SECRET = "piceli-ui-launch"
LAUNCH_KEY = "token"
STATUS_CONFIGMAP = "piceli-gitops-status"
REQUESTS_CONFIGMAP = "piceli-gitops-requests"
#: The declaration ``piceli cluster init`` stores (``cluster_init.CLUSTER_CONFIG``).
CLUSTER_CONFIG = "piceli-cluster"
READER = "piceli-ui-read"
MANAGED = {"app.kubernetes.io/managed-by": "piceli", "piceli.io/component": "ui"}
_RBAC = "rbac.authorization.k8s.io"
_IMAGE = re.compile(r"[a-z0-9][a-z0-9./:_-]*@sha256:[0-9a-f]{64}")
_READ = ["get", "list", "watch"]


class UiInstallError(ValueError):
    """The cluster's ``Ui`` cannot be installed as declared (``code`` is fixed)."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def ui_image(cluster: Cluster) -> str:
    """The digest-pinned Piceli image the UI runs (``Ui.image``, else the controller's)."""
    ui = cluster.ui
    image = (ui.image if ui is not None else None) or (
        cluster.controller.image if cluster.controller is not None else None
    )
    if not image or not _IMAGE.fullmatch(image):
        raise UiInstallError(
            "the in-cluster UI needs a Piceli image pinned by digest "
            "(repo@sha256:<64 hex>): set Ui(image=...) or Controller(image=...)",
            code="ui-install-image-unpinned",
        )
    return image


def ui_node(cluster: Cluster) -> str | None:
    """The node the UI is placed on (``Ui.on``, else the controller's), or ``None``."""
    ui = cluster.ui
    node = (ui.on if ui is not None else None) or (
        cluster.controller.on if cluster.controller is not None else None
    )
    if node is None:
        return None
    if cluster.nodes and node not in {item.name for item in cluster.nodes}:
        raise UiInstallError(
            f"Ui(on={node!r}) names no node of the cluster: declare it in "
            "Cluster(nodes=[...]) or place the UI on a declared node",
            code="ui-install-node-unknown",
        )
    return node


def _meta(name: str, namespace: str | None = None, **labels: str) -> dict[str, Any]:
    meta: dict[str, Any] = {"name": name, "labels": {**MANAGED, **labels}}
    if namespace is not None:
        meta["namespace"] = namespace
    return meta


def render_ui(cluster: Cluster) -> list[dict[str, Any]]:
    """The UI's objects for ``cluster`` in apply order; ``[]`` when it declares no ``Ui``.

    Raises :class:`UiInstallError` (``ui-install-image-unpinned``,
    ``ui-install-node-unknown``) when the image or node cannot be used.
    """
    if cluster.ui is None:
        return []
    if cluster.ui.access != "forward":
        raise UiInstallError(
            'the in-cluster UI supports only Ui(access="forward")',
            code="ui-install-access-unsupported",
        )
    image = ui_image(cluster)
    node = ui_node(cluster)
    selector = {"app.kubernetes.io/name": NAME}
    pod: dict[str, Any] = {
        "serviceAccountName": NAME,
        "automountServiceAccountToken": True,
        "enableServiceLinks": False,
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 65532,
            "runAsGroup": 65532,
            "fsGroup": 65532,
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "containers": [
            {
                "name": "ui",
                "image": image,
                "imagePullPolicy": "IfNotPresent",
                "command": ["piceli"],
                "args": [
                    "ui",
                    "forward-serve",
                    "--namespace",
                    NAMESPACE,
                    "--port",
                    str(PORT),
                    "--launch-secret",
                    LAUNCH_SECRET,
                ],
                "ports": [{"name": "http", "containerPort": PORT}],
                "env": [
                    {"name": "HOME", "value": "/tmp"},
                    {"name": "TMPDIR", "value": "/tmp"},
                    {"name": "PICELI_IN_CLUSTER", "value": "1"},
                ],
                "resources": {
                    "requests": {"cpu": "50m", "memory": "192Mi"},
                    "limits": {"memory": "512Mi"},
                },
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "readOnlyRootFilesystem": True,
                    "capabilities": {"drop": ["ALL"]},
                },
                "volumeMounts": [{"name": "tmp", "mountPath": "/tmp"}],
            }
        ],
        "volumes": [{"name": "tmp", "emptyDir": {"medium": "Memory"}}],
    }
    if node is not None:
        pod["nodeSelector"] = {"kubernetes.io/hostname": node}
    subject = [{"kind": "ServiceAccount", "name": NAME, "namespace": NAMESPACE}]
    own_rules = [
        {
            "apiGroups": [""],
            "resources": ["configmaps"],
            # The controller's status, the requests it reads, and the
            # cluster declaration `piceli cluster init` stores (Cluster page).
            "resourceNames": [STATUS_CONFIGMAP, REQUESTS_CONFIGMAP, CLUSTER_CONFIG],
            "verbs": ["get"],
        },
        {
            "apiGroups": [""],
            "resources": ["configmaps"],
            "resourceNames": [REQUESTS_CONFIGMAP],
            "verbs": ["update", "patch"],
        },
        {
            "apiGroups": [""],
            "resources": ["secrets"],
            "resourceNames": [LAUNCH_SECRET],
            "verbs": ["get", "update", "patch"],
        },
    ]
    read_rules = [
        {
            "apiGroups": [""],
            "resources": ["namespaces", "pods", "services", "persistentvolumeclaims"],
            "verbs": _READ,
        },
        # Node names, arch, roles and readiness for the Cluster page; never
        # nodes/proxy (the kubelet API), so volume use stays unknown.
        {"apiGroups": [""], "resources": ["nodes"], "verbs": _READ},
        {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
        {
            "apiGroups": ["apps"],
            "resources": ["deployments", "replicasets", "statefulsets", "daemonsets"],
            "verbs": _READ,
        },
        {"apiGroups": ["batch"], "resources": ["jobs", "cronjobs"], "verbs": _READ},
    ]
    return [
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": _meta(NAME, NAMESPACE),
        },
        # Created empty: the UI writes its launch token here when it starts, and
        # a re-applied manifest (no ``data``) never overwrites it.
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": _meta(LAUNCH_SECRET, NAMESPACE),
            "type": "Opaque",
        },
        # The controller's request inbox, so that the UI never needs ``create``
        # on ConfigMaps; no ``data``: pending requests survive a re-apply.
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": REQUESTS_CONFIGMAP,
                "namespace": NAMESPACE,
                "labels": {
                    "app.kubernetes.io/managed-by": "piceli",
                    "piceli.io/component": "gitops",
                },
            },
        },
        {
            "apiVersion": f"{_RBAC}/v1",
            "kind": "Role",
            "metadata": _meta(NAME, NAMESPACE),
            "rules": own_rules,
        },
        {
            "apiVersion": f"{_RBAC}/v1",
            "kind": "RoleBinding",
            "metadata": _meta(NAME, NAMESPACE),
            "roleRef": {"apiGroup": _RBAC, "kind": "Role", "name": NAME},
            "subjects": subject,
        },
        {
            "apiVersion": f"{_RBAC}/v1",
            "kind": "ClusterRole",
            "metadata": _meta(READER),
            "rules": read_rules,
        },
        {
            "apiVersion": f"{_RBAC}/v1",
            "kind": "ClusterRoleBinding",
            "metadata": _meta(READER),
            "roleRef": {"apiGroup": _RBAC, "kind": "ClusterRole", "name": READER},
            "subjects": subject,
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": _meta(NAME, NAMESPACE),
            "spec": {
                "type": "ClusterIP",
                "selector": selector,
                "ports": [
                    {
                        "name": "http",
                        "port": PORT,
                        "targetPort": PORT,
                        "protocol": "TCP",
                    }
                ],
            },
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": _meta(NAME, NAMESPACE),
            "spec": {
                "replicas": 1,
                "strategy": {"type": "Recreate"},
                "selector": {"matchLabels": selector},
                "template": {
                    "metadata": {"labels": {**selector, **MANAGED}},
                    "spec": pod,
                },
            },
        },
    ]
