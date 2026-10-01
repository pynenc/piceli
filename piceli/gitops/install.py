"""Install and remove the GitOps controller: render, plan with a hash, apply.

``piceli gitops enable`` renders the controller's objects
(:func:`render_controller`), plans them against the live cluster
(:func:`plan_objects`: ``create``, ``apply`` or ``no-op`` per object) and
applies the plan only when the caller presents its hash. ``piceli gitops
disable`` plans the deletion of the same objects the same way; it never
deletes an environment, the namespace or (without ``delete_state``) the
state volume.

What the controller is allowed to do (the least it needs):

- in its namespace (Role): ConfigMaps (status, requests), Leases, Jobs,
  Pods and their logs, PersistentVolumeClaims and Events: the cluster
  build Jobs and their caches run here;
- cluster-wide (ClusterRole ``piceli-gitops``): read nodes (build and
  delivery facts); create, read and delete namespaces (one per branch); and
  create RoleBindings that bind **only** the ``piceli-gitops-deployer``
  ClusterRole (the ``bind`` verb is limited to that name), which is how it
  gets rights inside each environment's namespace and nowhere else;
- ``piceli-gitops-deployer`` (a ClusterRole used only through those
  RoleBindings): the namespaced kinds an app deploys;
- with ``cluster_rbac=True`` (the owner's opt-in, for apps that declare
  ClusterRoles): ClusterRoles and ClusterRoleBindings too.

The Git credentials stay in the owner's Secret: the Deployment mounts it as
files; nothing here reads it, and the ConfigMap never holds a credential.

Importing this module is side-effect free.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from piceli.gitops import GitOpsError
from piceli.gitops.config import ControllerConfig

PLAN_SCHEMA = "piceli.gitops-install-plan.v1"
NAME = "piceli-gitops"
DEPLOYER = "piceli-gitops-deployer"
STATE_CLAIM = "piceli-gitops-state"
CONFIG_MAP = "piceli-gitops-config"
MANAGED = {"app.kubernetes.io/managed-by": "piceli", "piceli.io/component": "gitops"}
ENV_LABEL = "piceli.io/gitops-env"
CONFIG_DIR = "/etc/piceli-gitops"
STATE_DIR = "/var/lib/piceli-gitops"
CREDENTIALS_DIR = "/var/run/piceli-gitops/git"
_IMAGE = re.compile(r"[a-z0-9][a-z0-9./:_-]*@sha256:[0-9a-f]{64}")
_QUANTITY = re.compile(r"[0-9]+(?:Ki|Mi|Gi|Ti)")
_NAME = re.compile(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?")
_RBAC = "rbac.authorization.k8s.io"

#: Namespaced resources an app deploys (the deployer role).
DEPLOYER_RULES: tuple[dict[str, Any], ...] = (
    {
        "apiGroups": [""],
        "resources": [
            "configmaps", "secrets", "services", "serviceaccounts",
            "persistentvolumeclaims", "pods", "pods/log", "pods/exec",
            "pods/portforward", "events", "resourcequotas", "limitranges",
        ],
        "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"],
    },
    {
        "apiGroups": ["apps"],
        "resources": ["deployments", "statefulsets", "daemonsets", "replicasets"],
        "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"],
    },
    {
        "apiGroups": ["batch"],
        "resources": ["jobs", "cronjobs"],
        "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"],
    },
    {
        "apiGroups": ["networking.k8s.io"],
        "resources": ["networkpolicies", "ingresses"],
        "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"],
    },
    {
        "apiGroups": ["policy"],
        "resources": ["poddisruptionbudgets"],
        "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"],
    },
    {
        "apiGroups": ["autoscaling"],
        "resources": ["horizontalpodautoscalers"],
        "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"],
    },
    {
        "apiGroups": [_RBAC],
        "resources": ["roles", "rolebindings"],
        "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"],
    },
    {
        "apiGroups": ["coordination.k8s.io"],
        "resources": ["leases"],
        "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"],
    },
)  # fmt: skip


@dataclass(frozen=True)
class InstallSettings:
    """How the controller runs (the ``enable`` options besides the config).

    :param image: The Piceli image, pinned by digest (``repo@sha256:…``).
    :param credentials_secret: The Secret with the Git credentials
        (``username``/``password`` or ``ssh-privatekey``/``known_hosts``).
    :param storage: Size of the state volume.
    :param storage_class: Its StorageClass (default: the cluster's).
    :param cluster_rbac: Allow ClusterRoles/ClusterRoleBindings (apps that
        declare cluster-scoped RBAC).
    :param node: Pin the controller to this node (``kubernetes.io/hostname``,
        a composition's ``Controller(on=)``); default: any node.
    """

    image: str
    credentials_secret: str | None = None
    storage: str = "10Gi"
    storage_class: str | None = None
    cluster_rbac: bool = False
    node: str | None = None

    def __post_init__(self) -> None:
        if not _IMAGE.fullmatch(self.image):
            raise GitOpsError(
                "gitops-image-unpinned",
                "--image must be pinned by digest: registry/repo@sha256:<64 hex>",
            )
        if self.credentials_secret is not None and not _NAME.fullmatch(
            self.credentials_secret
        ):
            raise GitOpsError(
                "gitops-config-invalid", "invalid --credentials-secret name"
            )
        if not _QUANTITY.fullmatch(self.storage):
            raise GitOpsError("gitops-config-invalid", "--storage must be like 10Gi")
        if self.storage_class is not None and not _NAME.fullmatch(self.storage_class):
            raise GitOpsError("gitops-config-invalid", "invalid --storage-class")
        if self.node is not None and not _NAME.fullmatch(self.node):
            raise GitOpsError("gitops-config-invalid", "invalid controller node")

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "image": self.image,
            "credentials_secret": self.credentials_secret,
            "storage": self.storage,
            "storage_class": self.storage_class,
            "cluster_rbac": self.cluster_rbac,
        }
        if self.node is not None:
            body["node"] = self.node
        return body


def _meta(name: str, namespace: str | None = None) -> dict[str, Any]:
    meta: dict[str, Any] = {"name": name, "labels": dict(MANAGED)}
    if namespace is not None:
        meta["namespace"] = namespace
    return meta


def render_controller(
    config: ControllerConfig | Any, settings: InstallSettings
) -> list[dict[str, Any]]:
    """The controller's objects, in apply order.

    ``config`` is a :class:`ControllerConfig` or a composition's
    :class:`piceli.infra.controller.CompositionConfig` (its ``namespace`` and
    ``to_dict()`` are used).
    """
    return render_foundation(
        config.namespace,
        storage=settings.storage,
        storage_class=settings.storage_class,
        cluster_rbac=settings.cluster_rbac,
    ) + _render_workload(config, settings)


def render_foundation(
    namespace: str,
    *,
    storage: str = "10Gi",
    storage_class: str | None = None,
    cluster_rbac: bool = False,
) -> list[dict[str, Any]]:
    """What the controller needs before it runs: namespace, identity, RBAC, state.

    ``piceli cluster init`` installs these; ``piceli gitops enable`` adds the
    configuration and the Deployment (:func:`render_controller` = both).
    """
    ns = namespace
    cluster_rules: list[dict[str, Any]] = [
        {"apiGroups": [""], "resources": ["nodes"], "verbs": ["get", "list", "watch"]},
        {
            "apiGroups": [""],
            "resources": ["namespaces"],
            "verbs": ["get", "list", "watch", "create", "patch", "delete"],
        },
        {
            "apiGroups": [_RBAC],
            "resources": ["rolebindings"],
            "verbs": ["get", "list", "create", "patch", "delete"],
        },
        {
            "apiGroups": [_RBAC],
            "resources": ["clusterroles"],
            "resourceNames": [DEPLOYER],
            "verbs": ["bind"],
        },
    ]
    if cluster_rbac:
        cluster_rules.append(
            {
                "apiGroups": [_RBAC],
                "resources": ["clusterroles", "clusterrolebindings"],
                "verbs": ["get", "list", "watch", "create", "update", "patch", "delete", "bind", "escalate"],
            }
        )  # fmt: skip
    own_rules = [
        {
            "apiGroups": [""],
            "resources": ["configmaps", "persistentvolumeclaims", "pods", "pods/log", "events"],
            "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"],
        },
        {
            "apiGroups": ["batch"],
            "resources": ["jobs"],
            "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"],
        },
        {
            "apiGroups": ["coordination.k8s.io"],
            "resources": ["leases"],
            "verbs": ["get", "list", "watch", "create", "update", "patch", "delete"],
        },
    ]  # fmt: skip
    claim: dict[str, Any] = {
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": storage}},
    }
    if storage_class:
        claim["storageClassName"] = storage_class
    subject = [{"kind": "ServiceAccount", "name": NAME, "namespace": ns}]
    return [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": _meta(ns)},
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": _meta(NAME, ns)},
        {
            "apiVersion": f"{_RBAC}/v1",
            "kind": "Role",
            "metadata": _meta(NAME, ns),
            "rules": own_rules,
        },
        {
            "apiVersion": f"{_RBAC}/v1",
            "kind": "RoleBinding",
            "metadata": _meta(NAME, ns),
            "roleRef": {"apiGroup": _RBAC, "kind": "Role", "name": NAME},
            "subjects": subject,
        },
        {
            "apiVersion": f"{_RBAC}/v1",
            "kind": "ClusterRole",
            "metadata": _meta(NAME),
            "rules": cluster_rules,
        },
        {
            "apiVersion": f"{_RBAC}/v1",
            "kind": "ClusterRoleBinding",
            "metadata": _meta(NAME),
            "roleRef": {"apiGroup": _RBAC, "kind": "ClusterRole", "name": NAME},
            "subjects": subject,
        },
        {
            "apiVersion": f"{_RBAC}/v1",
            "kind": "ClusterRole",
            "metadata": _meta(DEPLOYER),
            "rules": [dict(rule) for rule in DEPLOYER_RULES],
        },
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": _meta(STATE_CLAIM, ns),
            "spec": claim,
        },
    ]


def _render_workload(
    config: ControllerConfig | Any, settings: InstallSettings
) -> list[dict[str, Any]]:
    """The controller's configuration and Deployment."""
    ns = config.namespace
    volumes: list[dict[str, Any]] = [
        {"name": "config", "configMap": {"name": CONFIG_MAP}},
        {"name": "state", "persistentVolumeClaim": {"claimName": STATE_CLAIM}},
        {"name": "tmp", "emptyDir": {}},
    ]
    mounts: list[dict[str, Any]] = [
        {"name": "config", "mountPath": CONFIG_DIR, "readOnly": True},
        {"name": "state", "mountPath": STATE_DIR},
        {"name": "tmp", "mountPath": "/tmp"},
    ]
    args = [
        "gitops", "run",
        "--config", f"{CONFIG_DIR}/config.json",
        "--state-dir", STATE_DIR,
        "--service-account",
        "--namespace", ns,
    ]  # fmt: skip
    if settings.credentials_secret:
        volumes.append(
            {
                "name": "git-credentials",
                "secret": {
                    "secretName": settings.credentials_secret,
                    "defaultMode": 0o400,
                },
            }
        )
        mounts.append(
            {"name": "git-credentials", "mountPath": CREDENTIALS_DIR, "readOnly": True}
        )
        args += ["--credentials-dir", CREDENTIALS_DIR]
    selector = {"app.kubernetes.io/name": NAME}
    deployment: dict[str, Any] = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": _meta(NAME, ns),
        "spec": {
            "replicas": 1,
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": selector},
            "template": {
                "metadata": {"labels": {**selector, **MANAGED}},
                "spec": {
                    "serviceAccountName": NAME,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 65532,
                        "runAsGroup": 65532,
                        "fsGroup": 65532,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "controller",
                            "image": settings.image,
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["piceli"],
                            "args": args,
                            "env": [
                                {"name": "HOME", "value": "/tmp"},
                                {"name": "TMPDIR", "value": "/tmp"},
                            ],
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "256Mi"},
                                "limits": {"memory": "1Gi"},
                            },
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "volumeMounts": mounts,
                        }
                    ],
                    "volumes": volumes,
                },
            },
        },
    }
    if settings.node is not None:
        pod = deployment["spec"]["template"]["spec"]
        pod["nodeSelector"] = {"kubernetes.io/hostname": settings.node}
        # Pinned by name: a control-plane node is a deliberate choice.
        pod["tolerations"] = [
            {
                "key": "node-role.kubernetes.io/control-plane",
                "operator": "Exists",
                "effect": "NoSchedule",
            }
        ]
    return [
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": _meta(CONFIG_MAP, ns),
            "data": {
                "config.json": json.dumps(config.to_dict(), sort_keys=True, indent=2)
            },
        },
        deployment,
    ]


# ---------------------------------------------------------------- the API

_PLURALS = {
    "Namespace": "namespaces",
    "ServiceAccount": "serviceaccounts",
    "ConfigMap": "configmaps",
    "Secret": "secrets",
    "PersistentVolumeClaim": "persistentvolumeclaims",
    "Deployment": "deployments",
    "DaemonSet": "daemonsets",
    "Service": "services",
    "Job": "jobs",
    "NetworkPolicy": "networkpolicies",
    "Role": "roles",
    "RoleBinding": "rolebindings",
    "ClusterRole": "clusterroles",
    "ClusterRoleBinding": "clusterrolebindings",
}
CLUSTER_KINDS = frozenset({"Namespace", "ClusterRole", "ClusterRoleBinding"})


def object_path(manifest: Mapping[str, Any], *, collection: bool = False) -> str:
    kind = manifest["kind"]
    api = manifest["apiVersion"]
    root = f"/api/{api}" if "/" not in api else f"/apis/{api}"
    plural = _PLURALS[kind]
    meta = manifest["metadata"]
    base = (
        f"{root}/{plural}"
        if kind in CLUSTER_KINDS
        else f"{root}/namespaces/{meta['namespace']}/{plural}"
    )
    return base if collection else f"{base}/{meta['name']}"


class Api:
    """A tiny JSON client on a Kubernetes ``ApiClient`` (explicit kubeconfig)."""

    def __init__(
        self,
        client: Any,
        *,
        request_seconds: float = 10.0,
        field_manager: str = "piceli-gitops",
    ) -> None:
        self.client = client
        self.request_seconds = request_seconds
        self.field_manager = field_manager

    def call(
        self,
        path: str,
        method: str,
        body: Any = None,
        content: str = "application/json",
        *,
        missing_ok: bool = True,
    ) -> Any:
        from kubernetes.client.exceptions import ApiException

        try:
            response = self.client.call_api(
                path,
                method,
                query_params=(
                    [("fieldManager", self.field_manager)]
                    if method in {"POST", "PATCH"}
                    else []
                ),
                header_params={"Accept": "application/json", "Content-Type": content},
                body=body,
                auth_settings=["BearerToken"],
                _preload_content=False,
                _request_timeout=self.request_seconds,
            )
        except ApiException as error:
            if error.status == 404 and missing_ok:
                return None
            raise GitOpsError(
                "gitops-cluster-failed",
                f"the API refused {method} {path.rsplit('/', 2)[-2]} (HTTP {error.status})",
            ) from None
        except OSError:
            raise GitOpsError(
                "gitops-cluster-failed", "the API server is unreachable"
            ) from None
        raw = response[0] if isinstance(response, tuple) else response
        return json.loads(raw.data or b"null")

    def text(self, path: str, query: Sequence[tuple[str, str]] = ()) -> str | None:
        """A plain-text read (a pod log); ``None`` when it is missing or refused."""
        import urllib3
        from kubernetes.client.exceptions import ApiException

        try:
            response = self.client.call_api(
                path,
                "GET",
                query_params=list(query),
                header_params={"Accept": "*/*"},
                auth_settings=["BearerToken"],
                _preload_content=False,
                _request_timeout=self.request_seconds,
            )
        except (ApiException, OSError, urllib3.exceptions.HTTPError):
            return None
        raw = response[0] if isinstance(response, tuple) else response
        data = raw.data or b""
        return data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data)

    def get(self, manifest: Mapping[str, Any]) -> dict[str, Any] | None:
        value = self.call(object_path(manifest), "GET")
        return value if isinstance(value, dict) else None

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if callable(close):
            close()


@contextmanager
def connect(
    kubeconfig: Path,
    context: str,
    *,
    transport: str = "https",
    exec_policy: Any = None,
) -> Iterator[Api]:
    """An :class:`Api` for exactly ``context`` in ``kubeconfig``."""
    from piceli.k8s.ops.provider_factory import api_client_from_kubeconfig

    try:
        client = api_client_from_kubeconfig(
            kubeconfig,
            context,
            transport=transport,  # type: ignore[arg-type]
            exec_policy=exec_policy,
        )
    except ValueError as error:
        raise GitOpsError(
            "gitops-cluster-failed", f"kubeconfig refused: {error}"
        ) from None
    api = Api(client)
    try:
        yield api
    finally:
        api.close()


def _subset(desired: Any, live: Any) -> bool:
    """Whether every desired field has the same value in ``live``."""
    if isinstance(desired, Mapping):
        return isinstance(live, Mapping) and all(
            _subset(value, live.get(key)) for key, value in desired.items()
        )
    if isinstance(desired, list):
        return (
            isinstance(live, list)
            and len(desired) == len(live)
            and all(_subset(a, b) for a, b in zip(desired, live, strict=True))
        )
    return bool(desired == live)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


@dataclass(frozen=True)
class ObjectPlan:
    """The plan of one controller object."""

    operation: str  # create | apply | no-op | delete | absent
    manifest: Mapping[str, Any]
    live_uid: str | None = None

    def describe(self) -> dict[str, Any]:
        meta = self.manifest["metadata"]
        item: dict[str, Any] = {
            "operation": self.operation,
            "kind": self.manifest["kind"],
            "name": meta["name"],
            "cluster_scoped": self.manifest["kind"] in CLUSTER_KINDS,
        }
        if "namespace" in meta:
            item["namespace"] = meta["namespace"]
        return item


@dataclass(frozen=True)
class InstallPlan:
    """What ``enable``/``disable`` would do, and the hash that approves it."""

    action: str  # enable | disable
    objects: tuple[ObjectPlan, ...]
    cluster_uid: str | None
    config: Mapping[str, Any]
    schema: str = PLAN_SCHEMA

    @property
    def plan_hash(self) -> str:
        body = {
            "schema": self.schema,
            "action": self.action,
            "cluster_uid": self.cluster_uid,
            "config": self.config,
            "objects": [
                {
                    **item.describe(),
                    "live_uid": item.live_uid,
                    "manifest": item.manifest,
                }
                for item in self.objects
            ],
        }
        return "sha256:" + hashlib.sha256(_canonical(body)).hexdigest()

    @property
    def changes(self) -> bool:
        return any(item.operation not in {"no-op", "absent"} for item in self.objects)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "action": self.action,
            "plan_hash": self.plan_hash,
            "cluster_uid": self.cluster_uid,
            "changes": [item.describe() for item in self.objects],
        }


def _cluster_uid(api: Api) -> str | None:
    found = api.call("/api/v1/namespaces/kube-system", "GET")
    return (
        (found or {}).get("metadata", {}).get("uid")
        if isinstance(found, dict)
        else None
    )


def plan_objects(
    api: Api,
    objects: Sequence[Mapping[str, Any]],
    *,
    action: str,
    config: Mapping[str, Any],
    schema: str = PLAN_SCHEMA,
) -> InstallPlan:
    """Compare ``objects`` with the live cluster (``enable``) or plan their removal."""
    planned: list[ObjectPlan] = []
    for manifest in objects:
        live = api.get(manifest)
        uid = (live or {}).get("metadata", {}).get("uid") if live else None
        if action == "disable":
            operation = "delete" if live is not None else "absent"
        elif live is None:
            operation = "create"
        elif _subset(manifest, live):
            operation = "no-op"
        else:
            operation = "apply"
        planned.append(ObjectPlan(operation, manifest, uid))
    return InstallPlan(action, tuple(planned), _cluster_uid(api), dict(config), schema)


def execute(
    api: Api, plan: InstallPlan, log: Callable[[str], None] = lambda _: None
) -> list[dict[str, Any]]:
    """Run a plan (the caller checked its hash). Deletes go in reverse order."""
    done: list[dict[str, Any]] = []
    items = list(plan.objects)
    if plan.action == "disable":
        items.reverse()
    for item in items:
        manifest = dict(item.manifest)
        if item.operation == "create":
            api.call(
                object_path(manifest, collection=True),
                "POST",
                manifest,
                missing_ok=False,
            )
        elif item.operation in {"apply", "delete"}:
            live = api.get(manifest)
            if live is None or live["metadata"].get("uid") != item.live_uid:
                raise GitOpsError(
                    "gitops-plan-changed",
                    "an object changed since the plan; plan again",
                )
            meta = {
                "uid": live["metadata"]["uid"],
                "resourceVersion": live["metadata"]["resourceVersion"],
            }
            if item.operation == "apply":
                body = {**manifest, "metadata": {**manifest["metadata"], **meta}}
                api.call(
                    object_path(manifest),
                    "PATCH",
                    body,
                    "application/merge-patch+json",
                    missing_ok=False,
                )
            else:
                api.call(
                    object_path(manifest),
                    "DELETE",
                    {"preconditions": meta, "propagationPolicy": "Background"},
                )
        else:
            continue
        done.append(item.describe())
        log(f"  {item.operation} {manifest['kind']}/{manifest['metadata']['name']}")
    return done


def removable(
    objects: Sequence[Mapping[str, Any]], *, delete_state: bool
) -> list[Mapping[str, Any]]:
    """What ``disable`` removes: never the namespace, the state volume only on request."""
    return [
        item
        for item in objects
        if item["kind"] != "Namespace"
        and (delete_state or item["kind"] != "PersistentVolumeClaim")
    ]


def grant_env_access(
    kubeconfig: Path,
    context: str,
    namespace: str,
    *,
    controller_namespace: str,
    branch: str,
    transport: str = "https",
) -> None:
    """Create the env namespace (if missing) and bind the deployer role in it."""
    namespace_body = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": namespace,
            "labels": {**MANAGED, ENV_LABEL: _label_value(branch)},
        },
    }
    binding = {
        "apiVersion": f"{_RBAC}/v1",
        "kind": "RoleBinding",
        "metadata": _meta(DEPLOYER, namespace),
        "roleRef": {"apiGroup": _RBAC, "kind": "ClusterRole", "name": DEPLOYER},
        "subjects": [
            {"kind": "ServiceAccount", "name": NAME, "namespace": controller_namespace}
        ],
    }
    with connect(kubeconfig, context, transport=transport) as api:
        for manifest in (namespace_body, binding):
            if api.get(manifest) is None:
                api.call(
                    object_path(manifest, collection=True),
                    "POST",
                    manifest,
                    missing_ok=False,
                )


def _label_value(text: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", text)[:63].strip("-._")
    return value or "branch"


# ---------------------------------------------------------------- in the pod

SERVICE_ACCOUNT_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")


def service_account_kubeconfig(
    path: Path,
    *,
    account_dir: Path = SERVICE_ACCOUNT_DIR,
    environ: Mapping[str, str] | None = None,
) -> str:
    """Write an explicit kubeconfig for the pod's service account; its context.

    Used only by ``piceli gitops run --service-account`` inside the
    controller's pod: the token and CA the kubelet mounts, the API address
    from ``KUBERNETES_SERVICE_HOST``/``_PORT``. The file is 0600 and written
    again before each poll (the kubelet rotates the token).
    """
    env = os.environ if environ is None else environ
    host, port = env.get("KUBERNETES_SERVICE_HOST"), env.get("KUBERNETES_SERVICE_PORT")
    token_file = account_dir / "token"
    if not host or not port or not token_file.is_file():
        raise GitOpsError(
            "gitops-cluster-failed", "not running in a pod with a service account"
        )
    token = token_file.read_text().strip()
    server = f"https://{f'[{host}]' if ':' in host else host}:{port}"
    document = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [
            {
                "name": "in-cluster",
                "cluster": {
                    "server": server,
                    "certificate-authority": str(account_dir / "ca.crt"),
                },
            }
        ],
        "users": [{"name": NAME, "user": {"token": token}}],
        "contexts": [
            {"name": NAME, "context": {"cluster": "in-cluster", "user": NAME}}
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w") as stream:
        json.dump(document, stream)
    return NAME
