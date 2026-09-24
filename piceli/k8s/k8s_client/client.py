import base64
import json
import logging
import threading
from collections.abc import Callable
from functools import cached_property
from typing import Any, ClassVar, Optional

from google.auth.transport.requests import Request
from google.oauth2 import service_account
from kubernetes import client, config, watch

from piceli.k8s.config.kubeconfig import KubeConfig
from piceli.k8s.templates.auxiliary.resource_request import ClusterResources
from piceli.settings import GCE_SA_INFO

logger = logging.getLogger(__name__)

GCP_SCOPES = (
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
)


def gke_credentials_factory(
    sa_info: dict[str, Any],
) -> Callable[[], service_account.Credentials]:
    """Build refreshed GKE credentials in memory from service-account info.

    Passed to KubeConfigLoader as ``get_google_credentials`` so the gcp
    auth-provider never needs GOOGLE_APPLICATION_CREDENTIALS or a key file on
    disk; the loader calls it again whenever the token expires.
    """

    def get_credentials() -> service_account.Credentials:
        credentials = service_account.Credentials.from_service_account_info(
            sa_info, scopes=list(GCP_SCOPES)
        )
        credentials.refresh(Request())
        return credentials

    return get_credentials


class ClientManager:
    """Singleton to manage k8s client instances for different kubeconfigs"""

    _instance_lock = threading.Lock()
    _instance: ClassVar[Optional["ClientManager"]] = None
    _clients: dict[KubeConfig | None, client.ApiClient] = {}

    def __new__(cls) -> "ClientManager":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
        return cls._instance

    def get_client(self, kubeconfig: KubeConfig | None = None) -> client.ApiClient:
        if kubeconfig not in self._clients:
            # this it probably only work in GCP make this more generic when refactoring legacy libs
            if kubeconfig:
                logger.debug(f"connection to client using {kubeconfig=}")
                if not GCE_SA_INFO:
                    # TODO: if still necessary after refactoring, use cistell
                    raise ValueError("GCE_SA_INFO required for GKE kubeconfig")
                sa_info = json.loads(base64.b64decode(GCE_SA_INFO).decode("utf-8"))
                configuration = client.Configuration()
                loader = config.kube_config.KubeConfigLoader(
                    kubeconfig.as_dict,
                    get_google_credentials=gke_credentials_factory(sa_info),
                )
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

    def __init__(self, kubeconfig: KubeConfig | None = None):
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
    label_selector: dict[str, str] | None = None,
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
    ctx: ClientContext, Namespace: str, label_selector: dict[str, str] | None = None
) -> list[client.V1Pod]:
    """get the cluster resources"""
    _label_selector = None
    if label_selector:
        _label_selector = ",".join(f"{k}={v}" for k, v in label_selector.items())
    return ctx.core_api.list_namespaced_pod(
        namespace=Namespace, label_selector=_label_selector
    ).items
