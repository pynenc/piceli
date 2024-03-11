import base64
import json
import logging
import os
import sys
import tempfile
import threading
from functools import cached_property
from typing import Any, Optional

from kubernetes import client, config, watch

sys.path.append("/Users/krossover/git/piceli")
from piceli.k8s.config.kubeconfig import KubeConfig
from piceli.k8s.templates.auxiliary.resource_request import ClusterResources
from piceli.settings import GCE_SA_INFO

# from piceli.k8s.k8s_client.tmp import kubeconfig as _tmp_kubeconfig # TMP FOR MINIKUBE REMOVE!

logger = logging.getLogger(__name__)


class ClientManager:
    """Singleton to manage k8s client instances for different kubeconfigs"""

    _instance_lock = threading.Lock()
    _clients: dict[Optional[KubeConfig], client.ApiClient] = {}

    def __new__(cls) -> "ClientManager":
        with cls._instance_lock:
            if not hasattr(cls, "_instance"):
                cls._instance = super().__new__(cls)
        return cls._instance

    def get_client(self, kubeconfig: Optional[KubeConfig] = None) -> client.ApiClient:
        # TODO REMOVE default to minikube when proper config is ready (pystell)
        # kubeconfig = _tmp_kubeconfig  # REMOVE THIS!!!
        if kubeconfig not in self._clients:
            # this it probably only work in GCP make this more generic
            if kubeconfig:
                logger.debug(f"connection to client using {kubeconfig=}")
                credentials = json.loads(base64.b64decode(GCE_SA_INFO).decode("utf-8"))
                with tempfile.TemporaryDirectory():
                    with open(
                        sa_json_name := "sa.json", "w", encoding="utf-8"
                    ) as sa_file:
                        json.dump(credentials, sa_file)
                    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = sa_json_name
                    configuration = client.Configuration()
                    loader = config.kube_config.KubeConfigLoader(kubeconfig.as_dict)
                    loader.load_and_set(configuration)
                self._clients[kubeconfig] = client.ApiClient(configuration)
            else:
                try:
                    config.load_incluster_config()
                    logger.debug("in cluster connection to k8s")
                except config.ConfigException:
                    config.load_kube_config()
                    logger.debug("local connection to k8s")
                self._clients[kubeconfig] = client.ApiClient()
        return self._clients[kubeconfig]


class ClientContext:
    """Context for handling the api's for a specifc kubeconfig client"""

    def __init__(self, kubeconfig: Optional[KubeConfig] = None):
        self.kubeconfig = kubeconfig
        self._api_cache: dict[str, Any] = {}

    @cached_property
    def api_client(self) -> client.ApiClient:
        return ClientManager().get_client(self.kubeconfig)

    @staticmethod
    def get_api_class(api_name: str) -> Any:
        return getattr(client, api_name)

    def get_api(self, api_name: str) -> Any:
        if api_name not in self._api_cache:
            self._api_cache[api_name] = self.get_api_class(api_name)(self.api_client)
        return self._api_cache[api_name]

    @cached_property
    def core_api(self) -> client.CoreV1Api:
        return client.CoreV1Api(self.api_client)

    @cached_property
    def batch_api(self) -> client.BatchV1Api:
        return client.BatchV1Api(self.api_client)

    @cached_property
    def apps_api(self) -> client.AppsV1Api:
        return client.AppsV1Api(self.api_client)

    @cached_property
    def auth_api(self) -> client.AuthorizationV1Api:
        return client.AuthorizationV1Api(self.api_client)

    @cached_property
    def rbacauthorization_api(self) -> client.RbacAuthorizationV1Api:
        return client.RbacAuthorizationV1Api(self.api_client)

    @cached_property
    def hpa_api(self) -> client.AutoscalingV1Api:
        return client.AutoscalingV1Api(self.api_client)

    @cached_property
    def custom_api(self) -> client.CustomObjectsApi:
        return client.CustomObjectsApi(self.api_client)

    @cached_property
    def extensions_api(self) -> client.ApiextensionsV1Api:
        return client.ApiextensionsV1Api(self.api_client)

    @cached_property
    def watch(self) -> watch.Watch:
        return watch.Watch()


def get_cluster_resources(
    ctx: ClientContext,
    Namespace: str,
    label_selector: Optional[dict[str, str]] = None,
    get_pods: bool = True,
) -> "ClusterResources":
    """get the cluster resources"""

    nodes = ctx.core_api.list_node()
    if get_pods:
        pods = get_cluster_pods(ctx, Namespace, label_selector)
        pods_metrics = ctx.custom_api.list_namespaced_custom_object(
            group="metrics.k8s.io",
            version="v1beta1",
            namespace=Namespace,
            plural="pods",
        )
        return ClusterResources.from_cluster_info(
            nodes.items, pods, pods_metrics["items"]
        )
    return ClusterResources.from_cluster_info(nodes.items, [], [])


def get_cluster_pods(
    ctx: ClientContext, Namespace: str, label_selector: Optional[dict[str, str]] = None
) -> list[client.V1Pod]:
    """get the cluster resources"""
    _label_selector = None
    if label_selector:
        _label_selector = ",".join(f"{k}={v}" for k, v in label_selector.items())
    return ctx.core_api.list_namespaced_pod(
        namespace=Namespace, label_selector=_label_selector
    ).items


if __name__ == "__main__":
    k8s = ClientManager()
    ctx = ClientContext()
    resources = get_cluster_resources(ctx, "default")
    pass
