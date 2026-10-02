"""The cluster side of per-branch environments: namespaces, records, scaling, teardown.

:class:`EnvCluster` wraps one Kubernetes API client built from the pipeline
target's explicit kubeconfig and context (never the ambient one). Tests pass
an object with the same methods instead.

Importing this module is side-effect free.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from piceli.envs.model import ENV_OF_LABEL, MANAGED_BY, RECORD_NAME, EnvError

_WORKLOADS = (
    ("Deployment", "list_namespaced_deployment"),
    ("StatefulSet", "list_namespaced_stateful_set"),
)
FIELD_MANAGER = "piceli-envs"


def _status(error: BaseException) -> int | None:
    status = getattr(error, "status", None)
    return status if isinstance(status, int) else None


_VERBS = {"read": "get", "list": "list", "create": "create", "patch": "patch",
           "delete": "delete", "replace": "update"}  # fmt: skip


def request_of(method: Any, args: tuple[Any, ...]) -> dict[str, Any]:
    """What Piceli asked for, from a client method and its arguments.

    ``read_namespaced_persistent_volume_claim("data", "shop")`` is ``{"verb":
    "get", "resource": "persistentvolumeclaims", "namespace": "shop"}``. Only
    Piceli's own request: never anything the server answered.
    """
    action, _, rest = str(getattr(method, "__name__", "")).partition("_")
    namespaced = rest.startswith("namespaced_")
    noun = rest.removeprefix("namespaced_").replace("_", "")
    resource = (
        noun[:-1] + "ies"
        if noun.endswith("y")
        else noun + "es"
        if noun.endswith("s")
        else noun + "s"
    )
    namespace = None
    if namespaced:
        index = 0 if action in {"list", "create"} else 1
        value = args[index] if len(args) > index else None
        namespace = value if isinstance(value, str) else None
    return {
        "verb": _VERBS.get(action, action),
        "resource": resource,
        "namespace": namespace,
    }


class EnvCluster:
    """Reads and writes of the environment commands, across namespaces.

    :param client: A ``kubernetes.client.ApiClient`` (explicit kubeconfig).
    :param request_seconds: Per-request timeout.
    """

    def __init__(self, client: Any, *, request_seconds: float = 30.0) -> None:
        self.client = client
        self.request_seconds = request_seconds

    def close(self) -> None:
        self.client.close()

    def _core(self) -> Any:
        from kubernetes.client import CoreV1Api

        return CoreV1Api(self.client)

    def _apps(self) -> Any:
        from kubernetes.client import AppsV1Api

        return AppsV1Api(self.client)

    def _rbac(self) -> Any:
        from kubernetes.client import RbacAuthorizationV1Api

        return RbacAuthorizationV1Api(self.client)

    def _data(self, response: Any) -> dict[str, Any]:
        value = json.loads(response.data)
        return value if isinstance(value, dict) else {}

    def _call(self, method: Any, *args: Any, **query: Any) -> dict[str, Any] | None:
        """The JSON body, or ``None`` when the object does not exist (404)."""
        from kubernetes.client.exceptions import ApiException

        try:
            return self._data(
                method(
                    *args,
                    _preload_content=False,
                    _request_timeout=self.request_seconds,
                    **query,
                )
            )
        except ApiException as error:
            status = _status(error)
            if status == 404:
                return None
            asked = request_of(method, args)
            where = f" in namespace {asked['namespace']}" if asked["namespace"] else ""
            raise EnvError(
                "env-cluster-unavailable",
                f"the Kubernetes API refused {asked['verb']} {asked['resource']}"
                f"{where} (HTTP {status})",
                failed=True,
                # 401/403: RBAC denied it; anything else: the server refused.
                details={
                    "denied" if status in {401, 403} else "refused": {
                        **asked,
                        "status": status,
                    }
                },
            ) from None

    def _delete(self, read: Any, delete: Any, *names: str) -> bool:
        """Delete one object, guarded by its uid and resourceVersion; ``False`` if absent."""
        current = self._call(read, *names)
        if current is None:
            return False
        metadata = current.get("metadata") or {}
        body = {
            "apiVersion": "v1",
            "kind": "DeleteOptions",
            "preconditions": {
                "uid": metadata.get("uid"),
                "resourceVersion": metadata.get("resourceVersion"),
            },
            "propagationPolicy": "Background",
        }
        return self._call(delete, *names, body=body) is not None

    def _merge(
        self, read: Any, patch: Any, *names: str, change: dict[str, Any]
    ) -> None:
        """Merge-patch ``change`` into one object (guarded by its resourceVersion)."""
        current = self._call(read, *names)
        if current is None:
            raise EnvError(
                "env-cluster-unavailable",
                "an object changed while it was being updated; run the command again",
                failed=True,
            )
        metadata = current.get("metadata") or {}
        body = {
            **change,
            "metadata": {
                **change.get("metadata", {}),
                "uid": metadata.get("uid"),
                "resourceVersion": metadata.get("resourceVersion"),
            },
        }
        self._call(
            patch,
            *names,
            body,
            field_manager=FIELD_MANAGER,
            _content_type="application/merge-patch+json",
        )

    # --------------------------------------------------------- namespaces
    def namespaces(self, app: str) -> list[dict[str, Any]]:
        """Branch namespaces of ``app`` (label ``piceli.io/env-of``)."""
        body = self._call(
            self._core().list_namespace, label_selector=f"{ENV_OF_LABEL}={app}"
        )
        return [
            item for item in (body or {}).get("items") or () if isinstance(item, dict)
        ]

    def namespace(self, name: str) -> dict[str, Any] | None:
        return self._call(self._core().read_namespace, name)

    def create_namespace(
        self, name: str, labels: Mapping[str, str], annotations: Mapping[str, str]
    ) -> None:
        body = {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {
                "name": name,
                "labels": {**MANAGED_BY, **labels},
                "annotations": dict(annotations),
            },
        }
        self._call(self._core().create_namespace, body, field_manager=FIELD_MANAGER)

    def label_namespace(
        self, name: str, labels: Mapping[str, str], annotations: Mapping[str, str]
    ) -> None:
        """Add labels and annotations (adopts a namespace ``env push`` created)."""
        core = self._core()
        self._merge(
            core.read_namespace,
            core.patch_namespace,
            name,
            change={
                "metadata": {"labels": dict(labels), "annotations": dict(annotations)}
            },
        )

    def pushed(self, namespace: str, branch: str) -> dict[str, Any] | None:
        """The images ``piceli env push`` recorded for ``branch``, if any.

        ``{"images": {name: {digest, pull_ref?}}, "commit"?, "pushed_at"?}``
        from the ConfigMap ``piceli-env-<slug>``.
        """
        from piceli.k8s.cli.env_push import configmap_name

        body = self._call(
            self._core().read_namespaced_config_map, configmap_name(branch), namespace
        )
        if body is None:
            return None
        data = dict(body.get("data") or {})
        try:
            images = json.loads(data.get("images") or "{}")
        except ValueError:
            return None
        if not isinstance(images, dict) or not images:
            return None
        return {**data, "images": images}

    def delete_namespace(self, name: str) -> bool:
        core = self._core()
        return self._delete(core.read_namespace, core.delete_namespace, name)

    # ------------------------------------------------------------- record
    def record(self, namespace: str) -> dict[str, Any] | None:
        """The environment record (the ``piceli-env`` ConfigMap's ``record``)."""
        body = self._call(
            self._core().read_namespaced_config_map, RECORD_NAME, namespace
        )
        if body is None:
            return None
        try:
            value = json.loads((body.get("data") or {}).get("record") or "{}")
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}

    def write_record(self, namespace: str, record: Mapping[str, Any]) -> None:
        body = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": RECORD_NAME,
                "namespace": namespace,
                "labels": dict(MANAGED_BY),
            },
            "data": {"record": json.dumps(dict(record), sort_keys=True)},
        }
        core = self._core()
        if self._call(core.read_namespaced_config_map, RECORD_NAME, namespace) is None:
            self._call(
                core.create_namespaced_config_map,
                namespace,
                body,
                field_manager=FIELD_MANAGER,
            )
        else:
            self._merge(
                core.read_namespaced_config_map,
                core.patch_namespaced_config_map,
                RECORD_NAME,
                namespace,
                change={"data": body["data"]},
            )

    # ---------------------------------------------------------- workloads
    def workloads(self, namespace: str) -> list[dict[str, Any]]:
        """Deployments and StatefulSets of ``namespace`` (with ``kind`` set)."""
        found = []
        apps = self._apps()
        for kind, method in _WORKLOADS:
            body = self._call(getattr(apps, method), namespace)
            for item in (body or {}).get("items") or ():
                if isinstance(item, dict):
                    item["kind"] = kind
                    found.append(item)
        return found

    def scale(self, namespace: str, kind: str, name: str, replicas: int) -> None:
        from piceli.restore.cluster import RestoreCluster

        RestoreCluster(
            self.client, namespace, request_seconds=self.request_seconds
        ).scale(kind, name, replicas)

    def gitops_status(self) -> dict[str, Any] | None:
        """The GitOps controller's published status (``piceli-system``), or ``None``."""
        from piceli.gitops.state import read_status

        try:
            return read_status(self.client)
        except Exception:  # absent, forbidden or unreadable: the envs' own view
            return None

    # -------------------------------------------------------------- claims
    def claims(self, namespace: str) -> list[dict[str, Any]]:
        body = self._call(
            self._core().list_namespaced_persistent_volume_claim, namespace
        )
        return [
            item for item in (body or {}).get("items") or () if isinstance(item, dict)
        ]

    def delete_claim(self, namespace: str, name: str) -> None:
        core = self._core()
        self._delete(
            core.read_namespaced_persistent_volume_claim,
            core.delete_namespaced_persistent_volume_claim,
            name,
            namespace,
        )

    def volumes(self) -> list[dict[str, Any]]:
        body = self._call(self._core().list_persistent_volume)
        return [
            item for item in (body or {}).get("items") or () if isinstance(item, dict)
        ]

    def delete_volume(self, name: str) -> None:
        core = self._core()
        self._delete(core.read_persistent_volume, core.delete_persistent_volume, name)

    def api_endpoints(self) -> tuple[tuple[str, int], ...]:
        """``(address, port)`` of the API server (EndpointSlice ``default/kubernetes``)."""
        from kubernetes.client import DiscoveryV1Api

        body = self._call(
            DiscoveryV1Api(self.client).read_namespaced_endpoint_slice,
            "kubernetes",
            "default",
        )
        ports = [
            int(port["port"])
            for port in (body or {}).get("ports") or ()
            if isinstance(port, dict) and isinstance(port.get("port"), int)
        ]
        found = {
            (str(address), port)
            for endpoint in (body or {}).get("endpoints") or ()
            if isinstance(endpoint, dict)
            and (endpoint.get("conditions") or {}).get("ready") is not False
            for address in endpoint.get("addresses") or ()
            for port in ports
        }
        if not found:
            raise EnvError(
                "env-config-invalid",
                "allow_api: the EndpointSlice default/kubernetes lists no API "
                "server address",
            )
        return tuple(sorted(found))

    def cluster_objects(self, namespace: str) -> list[tuple[str, str]]:
        """``(kind, name)`` of the ClusterRoles/Bindings Piceli made for ``namespace``.

        An environment's cluster-scoped objects carry the label
        ``piceli.io/env-namespace=<namespace>`` (earlier releases: the name
        ``<namespace>:<app>:<name>``) and Piceli's ``piceli.io/owner`` mark;
        objects without that mark are never listed. A controller not allowed
        to list them (no ``--cluster-rbac``) could not have made any: empty.
        """
        from piceli.envs.isolation import ENV_NAMESPACE_LABEL

        rbac = self._rbac()
        found: list[tuple[str, str]] = []
        for kind, method in (
            ("ClusterRole", rbac.list_cluster_role),
            ("ClusterRoleBinding", rbac.list_cluster_role_binding),
        ):
            try:
                body = self._call(method)
            except EnvError as error:
                if "denied" in (error.details or {}):
                    return []
                raise
            for item in (body or {}).get("items") or ():
                metadata = (item or {}).get("metadata") or {}
                name = str(metadata.get("name") or "")
                labelled = (metadata.get("labels") or {}).get(ENV_NAMESPACE_LABEL)
                ours = labelled == namespace or name.startswith(f"{namespace}:")
                if ours and (metadata.get("annotations") or {}).get("piceli.io/owner"):
                    found.append((kind, name))
        return sorted(found)

    def delete_cluster_object(self, kind: str, name: str) -> None:
        rbac = self._rbac()
        if kind == "ClusterRole":
            self._delete(rbac.read_cluster_role, rbac.delete_cluster_role, name)
        elif kind == "ClusterRoleBinding":
            self._delete(
                rbac.read_cluster_role_binding, rbac.delete_cluster_role_binding, name
            )
        else:
            raise ValueError(f"not a cluster object Piceli removes: {kind}")
