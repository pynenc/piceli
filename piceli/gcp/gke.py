import datetime
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable

from google.api_core import exceptions
from google.cloud import container_v1  # type: ignore
from google.oauth2 import service_account
from tenacity import (
    after_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_fixed,
)

from piceli.k8s.config import kubeconfig
from piceli.settings import WAIT_GKE_RUNNING_MINUTES

logger = logging.getLogger(__name__)


@dataclass(eq=True, frozen=True)
class GKEClusterId:
    """identifies the cluster"""

    cluster_name: str
    project_name: str
    region: str

    @property
    def request_name(self) -> str:
        """Gets the cluster name used in the api requests"""
        return (
            f"{self.project_name}/locations/{self.region}/clusters/{self.cluster_name}"
        )

    def __str__(self) -> str:
        return f"GKE Cluster('{self.cluster_name}' on '{self.project_name}' and region '{self.region}')"


@dataclass
class GKECluster:
    """Client for GCP container_v1 cluster API"""

    credentials: service_account.Credentials

    def __post_init__(self) -> None:
        self._cache: dict[GKEClusterId, container_v1.Cluster] = {}

    def get(
        self, cluster_id: GKEClusterId, force_refresh: bool = False
    ) -> container_v1.Cluster:
        """gets the GKE cluster instance or raises an exception"""
        if force_refresh or cluster_id not in self._cache:
            client = container_v1.ClusterManagerClient(credentials=self.credentials)
            get_cluster = container_v1.GetClusterRequest(name=cluster_id.request_name)
            self._cache[cluster_id] = client.get_cluster(request=get_cluster)
        return self._cache[cluster_id]

    def exists(self, cluster_id: GKEClusterId) -> bool:
        """Check if the cluster exists"""
        try:
            self.get(cluster_id)
            return True
        except exceptions.NotFound:
            return False

    @retry(
        retry=retry_if_exception_type(exceptions.PermissionDenied),
        stop=stop_after_attempt(10),
        wait=wait_fixed(5),
        after=after_log(logger, logging.WARNING),
    )
    def apply(
        self, cluster_id: GKEClusterId, project_id: str, only_check: bool
    ) -> None:
        """Creates GKE cluster if doesn't exists"""
        raise NotImplementedError("Review before testing this method")
        # try:
        #     self.get(cluster_id)
        #     logger.info(f"Nothing to do, {cluster_id} already exists")
        # except exceptions.NotFound:
        #     if only_check:
        #         logger.warning(
        #             f"GKE cluster {cluster_id} don't exits and will be created"
        #         )
        #         return
        #     logger.warning(f"Creating GKE cluster {cluster_id}")
        #     # https://cloud.google.com/python/docs/reference/container/latest/google.cloud.container_v1.types.Cluster
        #     cluster = container_v1.Cluster(
        #         name=cluster_id.cluster_name,
        #         description=f"Cluster created by infra_client on project {cluster_id.project_name} and region {cluster_id.region}",
        #         initial_node_count=1,
        #         autopilot=container_v1.Autopilot(enabled=True),
        #         node_config=container_v1.NodeConfig(
        #             machine_type=const.GKE_NODE_MACHINE_TYPE
        #         ),
        #         # only on non autopilot clusters to enable workload identity
        #         workload_identity_config=container_v1.WorkloadIdentityConfig(
        #             workload_pool=f"{project_id}.svc.id.goog"
        #         ),
        #     )
        #     request = container_v1.CreateClusterRequest(
        #         parent=f"{cluster_id.project_name}/locations/{cluster_id.region}",
        #         cluster=cluster,
        #     )
        #     client = container_v1.ClusterManagerClient(credentials=self.credentials)
        #     _ = client.create_cluster(request=request)
        #     self._cache.pop(cluster_id, None)

    def wait(self, cluster_id: GKEClusterId) -> None:
        """Wait until the cluster is ready"""
        logger.info("Waiting for" + (msg := f"cluster {cluster_id}"))
        timeout = (start := time.time()) + 60 * WAIT_GKE_RUNNING_MINUTES
        while True:
            gke_cluster = self.get(cluster_id, force_refresh=True)
            if gke_cluster.status == container_v1.Cluster.Status.RUNNING:
                logger.info(f"{msg}: RUNNING")
                break
            if gke_cluster.status == container_v1.Cluster.Status.ERROR:
                logger.error(
                    "GKE Cluster privision can take up to 10-15min, retry pipeline to save gitlab CI/CD minutes"
                )
                raise Exception(f"Error creating {msg}: {gke_cluster.conditions}")
            if time.time() > timeout:
                msg = f"After more than {WAIT_GKE_RUNNING_MINUTES} min, {msg} still:{gke_cluster.status.name}"
                logger.error(msg)
                raise TimeoutError(msg)
            elapsed = datetime.timedelta(seconds=time.time() - start)
            logger.warning(
                f"{elapsed}... Still waiting for {msg}: {gke_cluster.status.name} {gke_cluster.conditions}"
            )
            time.sleep(5)

    def get_kube_config(self, cluster_id: GKEClusterId) -> kubeconfig.KubeConfig:
        """gets the kubeconfig to connect the kubernetes client to the gke cluster"""
        cluster = self.get(cluster_id)
        return kubeconfig.KubeConfig(
            cluster_name=cluster.name,
            cert=cluster.master_auth.cluster_ca_certificate,
            endpoint=cluster.endpoint,
        )


def wait(
    msg: str,
    get_status: Callable,
    stop_condition: Callable,
    timeout_secs: int,
    sleep_secs: int = 5,
    print_status: bool = True,
) -> None:
    """helper to wait until timeour"""
    logger.info(f"Waiting for {msg}")
    timeout = (start := time.time()) + timeout_secs

    def status_str(status: Any) -> str:
        return f": {status=}" if print_status else ""

    while True:
        status = None
        if stop_condition(status := get_status()):
            logger.info(f"{msg} successful: {status_str(status)}")
            break
        if time.time() > timeout:
            logger.error(
                msg := f"After {timeout_secs} secs, still waiting for {msg}: {status_str(status)}"
            )
            raise TimeoutError(msg)
        elapsed = datetime.timedelta(seconds=time.time() - start)
        logger.warning(f"{elapsed}... Still waiting for {msg}: {status_str(status)}")
        time.sleep(sleep_secs)
