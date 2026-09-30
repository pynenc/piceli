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
            if _status(error) == 404:
                return None
            raise EnvError(
                "env-cluster-unavailable",
                f"the Kubernetes API refused a request (HTTP {_status(error)})",
                failed=True,
            ) from None

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
        body = {"metadata": {"labels": dict(labels), "annotations": dict(annotations)}}
        self._call(self._core().patch_namespace, name, body)

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
        return self._call(self._core().delete_namespace, name) is not None

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
            self._call(
                core.replace_namespaced_config_map,
                RECORD_NAME,
                namespace,
                body,
                field_manager=FIELD_MANAGER,
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

    # -------------------------------------------------------------- claims
    def claims(self, namespace: str) -> list[dict[str, Any]]:
        body = self._call(
            self._core().list_namespaced_persistent_volume_claim, namespace
        )
        return [
            item for item in (body or {}).get("items") or () if isinstance(item, dict)
        ]

    def delete_claim(self, namespace: str, name: str) -> None:
        self._call(
            self._core().delete_namespaced_persistent_volume_claim, name, namespace
        )

    def volumes(self) -> list[dict[str, Any]]:
        body = self._call(self._core().list_persistent_volume)
        return [
            item for item in (body or {}).get("items") or () if isinstance(item, dict)
        ]

    def delete_volume(self, name: str) -> None:
        self._call(self._core().delete_persistent_volume, name)
