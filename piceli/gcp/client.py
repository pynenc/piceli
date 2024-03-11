# pylint: disable=no-member
import base64
import datetime
import json
import time
from abc import ABC
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, ClassVar, Optional, TypeVar

import google.auth
import logger
import yaml  # type: ignore[import]
from google.api_core import exceptions
from google.cloud import container_v1, resourcemanager_v3, storage  # type: ignore
from google.oauth2 import service_account
from googleapiclient import discovery
from infra_client.gcp import gcp_constants as const
from infra_client.gcp.gcp_client_cloud_sql import SQLClient
from infra_client.gcp.gcp_client_secret_manager import GSMClient
from infra_client.gcp.gcp_directory import DirectoryClient
from infra_client.gcp.gcp_firebase import FirebaseClient
from infra_client.gcp.gcp_iam import IAMClient
from tenacity import (
    after_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_fixed,
)

log = logger.get_logger(__name__)


class ResourceLifecycleState(Enum):
    """GCP Resources common lifecycle states"""

    LIFECYCLE_STATE_UNSPECIFIED = "LIFECYCLE_STATE_UNSPECIFIED"
    # Unspecified state. This is only used/useful for distinguishing unset values.
    ACTIVE = "ACTIVE"
    # The normal and active state.
    DELETE_REQUESTED = "DELETE_REQUESTED"
    # The project has been marked for deletion by the user (by invoking projects.delete) or by the system (Google Cloud Platform). This can generally be reversed by invoking projects.undelete.


Resource = TypeVar(
    "Resource",
    resourcemanager_v3.Organization,
    resourcemanager_v3.Folder,
    resourcemanager_v3.Project,
)
ResourceClient = TypeVar(
    "ResourceClient",
    resourcemanager_v3.OrganizationsClient,
    resourcemanager_v3.FoldersClient,
    resourcemanager_v3.ProjectsClient,
)


class ResourceManagerType(Enum):
    """Type of Resources modeled on the resourcemanager_v3 API"""

    ORGANIZATION = "Organization"
    FOLDER = "Folder"
    PROJECT = "Project"


@dataclass
class ResourceManagerClient(ABC):
    """Class to manage resourcemanager_v3 clients for Organization, Project and Folders"""

    # https://cloud.google.com/python/docs/reference/cloudresourcemanager/latest

    credentials: service_account.Credentials
    resource: ClassVar[ResourceManagerType]
    cache_key: ClassVar[str]

    def __post_init__(self) -> None:
        # api_client: Type[resourcemanager_v3.OrganizationsClient| resourcemanager_v3.FoldersClient| resourcemanager_v3.ProjectsClient]
        self.api_keyword = self.resource.value.lower()
        self.api_obj = getattr(resourcemanager_v3, self.resource.value)
        self._api: Any = None
        self._cache: dict[str, Optional[Any]] = {}
        self._cache_deleted: dict[str, Any] = {}

    @property
    def api(self) -> ResourceClient:  # type: ignore
        """lazy evaluation of the resourcemanager_v3 API"""
        if not self._api:
            self._api = getattr(resourcemanager_v3, f"{self.resource.value}sClient")(
                credentials=self.credentials
            )
        return self._api

    def get_cache_key(self, obj: Resource) -> str:
        """key to use in the cache"""
        return getattr(obj, self.cache_key)

    def _get(self, key: str) -> Optional[Resource]:
        """Get the project from the cache or GCP API"""
        if key in self._cache:
            return self._cache[key]
        self._cache[key] = None
        api_method = f"search_{self.api_keyword}s"
        log.info(f"Calling to {self.api} method {api_method}")
        for obj in getattr(self.api, api_method)():
            if self.get_cache_key(obj) == key:
                if obj.state == self.api_obj.State.DELETE_REQUESTED:
                    self._cache_deleted[key] = obj
                elif obj.state == self.api_obj.State.ACTIVE:
                    self._cache[key] = obj
                else:
                    log.warning(
                        f"Ignoring non Active {self.api_keyword} {key}, detail: {obj}"
                    )
                break
        return self._cache.get(key)

    def get_name(self, key: str, only_check: bool) -> str:
        """gets the obj name: 'organizations/2234', 'folders/test', 'project/test-000',..."""
        if resource := self._get(key):
            return resource.name
        if only_check:
            # if only checking:
            #   - this is probably a parent reference that will be applied on deployment
            #   - just return a temporary name to keep checking other dependent resources
            if key in self._cache_deleted:
                log.warning(f"{self.api_keyword} deleted, will be undeleted")
                return f"[to-be-undeleted-{self.api_keyword}/{key}]"
            log.warning(f"{self.api_keyword} do not exists, will created")
            return f"[to-be-created-{self.api_keyword}/{key}]"
        # Otherwise resource should exists or raise RuntimeError!
        raise RuntimeError(f"{self.api_keyword} {key} does not exists")

    def _exists(self, obj: Resource) -> bool:
        """Checks if the object exists"""
        return bool(self._get(self.get_cache_key(obj)))

    @retry(
        # Retry because when it creates folders/subfolders the parent may take a few seconds to be acive:
        # google.api_core.exceptions.FailedPrecondition: 400 Parent is not in ACTIVE state. [resource_name: "folders/872258871641"]
        retry=retry_if_exception_type(exceptions.FailedPrecondition),
        stop=stop_after_attempt(2),
        wait=wait_fixed(5),
        after=after_log(log, logger.logging.WARNING),
    )
    def _apply(self, obj: Resource, only_check: bool) -> None:
        """Create the object and updates the cache"""

        def run_api_op(key: str, api_op: str, **kwargs: dict) -> None:
            api_method = f"{api_op}_{self.api_keyword}"
            msg = f"{self.api} method {api_method} with kwargs {kwargs}"
            if only_check:
                log.warning("Will call to " + msg)
            else:
                log.warning("Calling to " + msg)
                operation = getattr(self.api, api_method)(**kwargs)
                self._cache[key] = operation.result()
                self._cache_deleted.pop(key, None)

        def same_parent(other: Any) -> bool:
            return getattr(obj, "parent", "noParent...") == getattr(
                other, "parent", "noParent..."
            )

        # Start calling method _get to fill the caches if it's the first call
        current_obj = self._get(key := self.get_cache_key(obj))
        # Check if object was create and restore it
        if key in self._cache_deleted:
            log.warning(f"{self.api_keyword} {key} marked for deletion, undeleting it.")
            run_api_op(key, "undelete", name=self._cache_deleted[key].name)
            if only_check and not same_parent(self._cache_deleted[key]):
                log.info(
                    f"{self.api_keyword} {key} will be moved after undeleteing it."
                )
        # Check cache again, if object was undeleted, check if has to change folder
        if current_obj := self._get(key):
            if not same_parent(current_obj):
                log.warning(
                    f"Moving {self.api_keyword} {key} from {current_obj.parent} to {obj.parent}"
                )  # type: ignore
                run_api_op(
                    key, "move", name=current_obj.name, destination_parent=obj.parent
                )  # type: ignore
            else:
                log.info(f"No changes required for {self.api_keyword} {key}")
        # New object, create it
        else:
            log.warning(f"{self.api_keyword} {key} does not exists and will be created")
            run_api_op(key, "create", **{self.api_keyword: obj})


class OrganizationClient(ResourceManagerClient):
    """resourcemanager_v3.services.organizations"""

    # https://cloud.google.com/python/docs/reference/cloudresourcemanager/latest/google.cloud.resourcemanager_v3.services.organizations

    resource = ResourceManagerType.ORGANIZATION
    cache_key = "display_name"

    def exists(self, display_name: str) -> bool:
        """Check if the organization exists"""
        return self._exists(resourcemanager_v3.Organization(display_name=display_name))

    def apply(self, display_name: str, only_check: bool) -> None:
        """If the organization does not exists, raises an exception"""
        if only_check:
            self._apply(
                resourcemanager_v3.Organization(display_name=display_name), only_check
            )
        if not self._get(display_name):
            raise RuntimeError(f"Organization {display_name} does not exists")


class FolderClient(ResourceManagerClient):
    """resourcemanager_v3.services.folders"""

    # https://cloud.google.com/python/docs/reference/cloudresourcemanager/latest/google.cloud.resourcemanager_v3.services.folders
    resource = ResourceManagerType.FOLDER
    cache_key = "display_name"

    def exists(self, display_name: str, parent_name: str) -> bool:
        """Check if the organization exists"""
        return self._exists(
            resourcemanager_v3.Folder(parent=parent_name, display_name=display_name)
        )

    def apply(self, display_name: str, parent_name: str, only_check: bool) -> None:
        """Creates the GCP folder resource if does not already exists"""
        self._apply(
            resourcemanager_v3.Folder(parent=parent_name, display_name=display_name),
            only_check,
        )


class ProjectClient(ResourceManagerClient):
    """resourcemanager_v3.services.projects"""

    # https://cloud.google.com/python/docs/reference/cloudresourcemanager/latest/google.cloud.resourcemanager_v3.services.projects
    # https://cloud.google.com/python/docs/reference/cloudresourcemanager/latest/google.cloud.resourcemanager_v3.services.projects.ProjectsClient
    resource = ResourceManagerType.PROJECT
    cache_key = "project_id"
    REQUIRED_APIS: ClassVar[set[str]] = {
        # GKE Cluster
        "container.googleapis.com",
        # Secret Manager API
        "secretmanager.googleapis.com",
        # Cloud SQL
        "sqladmin.googleapis.com",
        # Service Networking API,
        "servicenetworking.googleapis.com",
        # Admin SDK API
        "admin.googleapis.com",
        # Cloud Pub/Sub API
        "pubsub.googleapis.com",
        # Firebase/Firestore API
        "firebase.googleapis.com",
        "firestore.googleapis.com",
        # Artifact Registry API
        "artifactregistry.googleapis.com",
        # Cloud Build API
        "cloudbuild.googleapis.com",
        # Cloud Functions API
        "cloudfunctions.googleapis.com",
    }

    def __post_init__(self) -> None:
        super().__post_init__()
        self._billing_cache: dict[str, dict] = {}
        self._billing_account: Optional[dict] = None
        self._enabled_apis_cache: dict[str, set[str]] = defaultdict(set)

    def is_billing_enabled(self, project: resourcemanager_v3.Project) -> bool:
        """Get billing info for the project"""
        if project.name not in self._billing_cache:
            billing_api = discovery.build(
                "cloudbilling", "v1", credentials=self.credentials
            )
            self._billing_cache[project.name] = (
                billing_api.projects().getBillingInfo(name=project.name).execute()
            )
        return self._billing_cache[project.name]["billingEnabled"]

    @property
    def billing_account_name(self) -> str:
        """Gets the name of a billing account"""
        if not self._billing_account:
            billing_api = discovery.build(
                "cloudbilling", "v1", credentials=self.credentials
            )
            billing_accounts = billing_api.billingAccounts().list().execute()
            if len(billing_accounts["billingAccounts"]) == 0:
                raise RuntimeError(
                    "Organization has no billing accounts or service account has no permissions to see them"
                )
            self._billing_account = billing_accounts["billingAccounts"][0]
        return self._billing_account["name"]  # type: ignore

    def apply_billing(
        self, project: resourcemanager_v3.Project, only_check: bool
    ) -> None:
        """Add the project to a billing account if necessary"""
        if self.is_billing_enabled(project):
            log.info(f"billing is enabled in project {project.project_id}")
        elif only_check:
            log.warning(
                f"Project {project.project_id} has no billing enabled, will be added to a billing account"
            )
        else:
            log.warning(
                f"Enabling billing for project {project.project_id} in billing account {self.billing_account_name}"
            )
            billing_api = discovery.build(
                "cloudbilling", "v1", credentials=self.credentials
            )
            billing_info = {
                "name": project.name,
                "projectId": project.project_id,
                "billingAccountName": self.billing_account_name,
                "billingEnabled": True,
            }
            billing_api.projects().updateBillingInfo(
                name=project.name, body=billing_info
            ).execute()

    def missing_apis(self, project: resourcemanager_v3.Project) -> set[str]:
        """Gets a list of disabled APIs that should be enabled on the project"""
        if project.name not in self._enabled_apis_cache:
            service_api = discovery.build(
                "serviceusage", "v1", credentials=self.credentials
            )
            request = service_api.services().list(
                parent=project.name, filter="state:ENABLED"
            )
            while request is not None:
                response = request.execute()
                for service in response.get("services", {}):
                    self._enabled_apis_cache[project.name].add(
                        service["config"]["name"]
                    )
                request = service_api.services().list_next(request, response)
        return self.REQUIRED_APIS - self._enabled_apis_cache[project.name]

    def enable_missing_apis(
        self, project: resourcemanager_v3.Project, only_check: bool
    ) -> None:
        """Enable the missing REQUIRED_APIS for the project"""
        if not (missing_apis := self.missing_apis(project)):
            log.info(
                f"Project {project.project_id} has all the required APIs({self.REQUIRED_APIS}) enabled"
            )
        elif only_check:
            log.warning(
                f"Project {project.project_id} APIs({missing_apis}) will be enabled"
            )
        else:
            log.warning(f"Enabling Project {project.project_id} APIs:{missing_apis}")
            service_api = discovery.build(
                "serviceusage", "v1", credentials=self.credentials
            )
            body = {"serviceIds": list(missing_apis)}
            service_api.services().batchEnable(parent=project.name, body=body).execute()

    def create_service_default_identity(
        self, project_id: str, service: str, only_check: bool
    ) -> None:
        """creates a service account for a managed service"""
        if only_check:
            if service not in self.REQUIRED_APIS:
                raise RuntimeError(
                    f"Service {service} is not in the list of required APIs"
                )
            log.info(
                f"Default Service account identity for {service} will be created if not exists"
            )
            return
        service_api = discovery.build(
            "serviceusage", "v1beta1", credentials=self.credentials
        )
        service_api.services().generateServiceIdentity(
            parent=f"projects/{project_id}/services/{service}"
        ).execute()

    def exists(self, project_id: str, parent_name: str) -> bool:
        """Check if the organization exists"""
        return self._exists(
            resourcemanager_v3.Project(
                parent=parent_name, project_id=project_id, display_name=project_id
            )
        )

    def apply(self, project_id: str, parent_name: str, only_check: bool) -> None:
        """Creates the GCP project resource if does not already exists"""
        self._apply(
            resourcemanager_v3.Project(
                parent=parent_name, project_id=project_id, display_name=project_id
            ),
            only_check,
        )
        if project := self._get(project_id):
            self.apply_billing(project, only_check)
            self.enable_missing_apis(project, only_check)

    def appy_project_iam_binding_role_to_service_account(
        self,
        role_name: str,
        service_account_name: str,
        project_id: str,
        check_only: bool,
    ) -> None:
        """update project.iamPolicy -> add binding project.role to project.sa"""
        project_role_name = IAMClient.get_project_role_name(role_name, project_id)
        required_member = f"serviceAccount:{IAMClient.get_service_account_email(service_account_name, project_id)}"
        self._appy_project_iam_binding_role_to_member(
            project_role_name, required_member, project_id, check_only
        )

    def appy_project_predefined_role_to_service_account_email(
        self,
        role_name: str,
        service_account_email: str,
        project_id: str,
        check_only: bool,
    ) -> None:
        """update project.iamPolicy -> add binding project.role to project.sa"""
        project_role_name = IAMClient.get_predefined_role_name(role_name)
        required_member = f"serviceAccount:{service_account_email}"
        self._appy_project_iam_binding_role_to_member(
            project_role_name, required_member, project_id, check_only
        )

    def appy_project_iam_binding_role_to_group(
        self, role_name: str, group_email: str, project_id: str, check_only: bool
    ) -> None:
        """update project.iamPolicy -> add binding project.role to group"""
        project_role_name = IAMClient.get_project_role_name(role_name, project_id)
        self._appy_project_iam_binding_role_to_member(
            project_role_name, f"group:{group_email}", project_id, check_only
        )

    def appy_project_iam_binding_predefined_role_to_group(
        self, role_name: str, group_email: str, project_id: str, check_only: bool
    ) -> None:
        """update project.iamPolicy -> add binding role to group"""
        role_name = IAMClient.get_predefined_role_name(role_name)
        self._appy_project_iam_binding_role_to_member(
            role_name, f"group:{group_email}", project_id, check_only
        )

    def _appy_project_iam_binding_role_to_member(
        self, role_name: str, required_member: str, project_id: str, check_only: bool
    ) -> None:
        """update project.iamPolicy -> add binding project.role to group"""
        api = discovery.build(
            "cloudresourcemanager", "v1", credentials=self.credentials
        )
        IAMClient.appy_iam_policy_binding(
            api.projects(), role_name, required_member, project_id, check_only
        )

    def appy_project_service_account_iam_binding_workload_identity(
        self, service_account_name: str, project_id: str, check_only: bool
    ) -> None:
        """update project.serviceAccount.iamPolicy -> at project.service_account level, binds role/workloadId to k8s.sa"""
        # https://cloud.google.com/kubernetes-engine/docs/concepts/workload-identity
        # step 6, glcoud command:
        # gcloud iam service-accounts add-iam-policy-binding GSA_NAME@GSA_PROJECT.iam.gserviceaccount.com \
        #       --role roles/iam.workloadIdentityUser \
        #       --member "serviceAccount:PROJECT_ID.svc.id.goog[NAMESPACE/KSA_NAME]"
        role_name = "roles/iam.workloadIdentityUser"
        # serviceAccount:PROJECT_ID.svc.id.goog[KUBERNETES_NAMESPACE/KUBERNETES_SERVICE_ACCOUNT]
        required_member = (
            f"serviceAccount:{project_id}.svc.id.goog[default/{service_account_name}]"
        )
        resource = IAMClient.get_service_account_project_name(
            service_account_name, project_id
        )
        api = discovery.build("iam", "v1", credentials=self.credentials)
        IAMClient.appy_iam_policy_binding(
            api.projects().serviceAccounts(),
            role_name,
            required_member,
            resource,
            check_only,
        )


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
        after=after_log(log, logger.logging.WARNING),
    )
    def apply(
        self, cluster_id: GKEClusterId, project_id: str, only_check: bool
    ) -> None:
        """Creates GKE cluster if doesn't exists"""
        try:
            self.get(cluster_id)
            log.info(f"Nothing to do, {cluster_id} already exists")
        except exceptions.NotFound:
            if only_check:
                log.warning(f"GKE cluster {cluster_id} don't exits and will be created")
                return
            log.warning(f"Creating GKE cluster {cluster_id}")
            # https://cloud.google.com/python/docs/reference/container/latest/google.cloud.container_v1.types.Cluster
            cluster = container_v1.Cluster(
                name=cluster_id.cluster_name,
                description=f"Cluster created by infra_client on project {cluster_id.project_name} and region {cluster_id.region}",
                initial_node_count=1,
                autopilot=container_v1.Autopilot(enabled=True),
                node_config=container_v1.NodeConfig(
                    machine_type=const.GKE_NODE_MACHINE_TYPE
                ),
                # only on non autopilot clusters to enable workload identity
                workload_identity_config=container_v1.WorkloadIdentityConfig(
                    workload_pool=f"{project_id}.svc.id.goog"
                ),
            )
            request = container_v1.CreateClusterRequest(
                parent=f"{cluster_id.project_name}/locations/{cluster_id.region}",
                cluster=cluster,
            )
            client = container_v1.ClusterManagerClient(credentials=self.credentials)
            _ = client.create_cluster(request=request)
            self._cache.pop(cluster_id, None)

    def wait(self, cluster_id: GKEClusterId) -> None:
        """Wait until the cluster is ready"""
        log.info("Waiting for" + (msg := f"cluster {cluster_id}"))
        timeout = (start := time.time()) + 60 * const.WAIT_GKE_RUNNING_MINUTES
        while True:
            gke_cluster = self.get(cluster_id, force_refresh=True)
            if gke_cluster.status == container_v1.Cluster.Status.RUNNING:
                log.info(f"{msg}: RUNNING")
                break
            if gke_cluster.status == container_v1.Cluster.Status.ERROR:
                log.error(
                    "GKE Cluster privision can take up to 10-15min, retry pipeline to save Github Actions minutes"
                )
                raise RuntimeError(f"Error creating {msg}: {gke_cluster.conditions}")
            if time.time() > timeout:
                log.error(
                    msg
                    := f"After more than {const.WAIT_GKE_RUNNING_MINUTES} min, {msg} still:{gke_cluster.status.name}"
                )
                raise TimeoutError(msg)
            elapsed = datetime.timedelta(seconds=time.time() - start)
            log.warning(
                f"{elapsed}... Still waiting for {msg}: {gke_cluster.status.name} {gke_cluster.conditions}"
            )
            time.sleep(5)

    def get_kube_config(self, cluster_id: GKEClusterId) -> dict:
        """gets the kubeconfig to connect the kubernetes client to the gke cluster"""
        cluster = self.get(cluster_id)
        # Refresh service_account token to inject in kubeconfig for the service account
        # self.credentials.refresh(google.auth.transport.requests.Request())
        config = (
            "apiVersion: v1\n"
            "kind: Config\n"
            "clusters:\n"
            "- name: {name}\n"
            "  cluster:\n"
            "    certificate-authority-data: {cert}\n"
            "    server: https://{server}\n"
            "contexts:\n"
            "- name: {name}\n"
            "  context:\n"
            "    cluster: {name}\n"
            "    user: {name}\n"
            "current-context: {name}\n"
            "users:\n"
            "- name: {name}\n"
            "  user:\n"
            "    auth-provider:\n"
            "      name: gcp\n"
            "      config:\n"
            # "        token: {token}\n"
            "        scopes: https://www.googleapis.com/auth/cloud-platform\n"
        ).format(
            name=f"kube-config-{cluster.name}",
            cert=cluster.master_auth.cluster_ca_certificate,
            server=cluster.endpoint,
            # token=self.credentials.token,
        )
        return yaml.safe_load(config)


def wait(
    msg: str,
    get_status: Callable,
    stop_condition: Callable,
    timeout_secs: int,
    sleep_secs: int = 5,
    print_status: bool = True,
) -> None:
    """helper to wait until timeour"""
    log.info(f"Waiting for {msg}")
    timeout = (start := time.time()) + timeout_secs

    def status_str(status: Any) -> str:
        return f": {status=}" if print_status else ""

    while True:
        status = None
        if stop_condition(status := get_status()):
            log.info(f"{msg} successful: {status_str(status)}")
            break
        if time.time() > timeout:
            log.error(
                msg
                := f"After {timeout_secs} secs, still waiting for {msg}: {status_str(status)}"
            )
            raise TimeoutError(msg)
        elapsed = datetime.timedelta(seconds=time.time() - start)
        log.warning(f"{elapsed}... Still waiting for {msg}: {status_str(status)}")
        time.sleep(sleep_secs)


class GCPClient:
    """GCP APIs Client"""

    # https://github.com/googleapis/google-cloud-python

    def __init__(self, credentials: Optional[dict] = None) -> None:
        if not credentials and SETTINGS.gce_sa_info:
            # remove the first two chars and the last char in the key
            credentials = json.loads(
                base64.b64decode(SETTINGS.gce_sa_info).decode("utf-8")
            )
            self.credentials = service_account.Credentials.from_service_account_info(
                credentials, scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            self._default_project_id: Optional[str] = None
        # elif SETTINGS.gce_sa_file:
        #     raise NotImplementedError("TODO: Implement load credentials from file")
        else:
            try:
                self.credentials, self._default_project_id = google.auth.default()
            except google.auth.exceptions.DefaultCredentialsError as ex:
                raise ValueError(
                    "Not possible to determine Credentials (default or env vars: GCE_SA_INFO)"
                ) from ex

        # apis clients
        self.api_organization = OrganizationClient(self.credentials)
        self.api_folders = FolderClient(self.credentials)
        self.api_projects = ProjectClient(self.credentials)
        self.api_iam = IAMClient(self.credentials)
        self.api_cluster = GKECluster(self.credentials)
        self.api_sql = SQLClient(self.credentials)
        self.api_secret_manager = GSMClient(self.credentials)
        self.api_directory = DirectoryClient(self.credentials)
        self.api_firebase = FirebaseClient(self.credentials)

    def exists_bucket(self, name: str) -> bool:
        """Check if the bucket exists"""
        storage_client = storage.Client(credentials=self.credentials)
        try:
            storage_client.get_bucket(name)
            return True
        except exceptions.NotFound:
            return False

    def apply_bucket(
        self,
        name: str,
        project_id: str,
        delete_after_days: Optional[int],
        location: str,
        only_check: bool,
    ) -> None:
        """Create the bucket if necessary"""
        storage_client = storage.Client(credentials=self.credentials)
        bucket = None
        try:
            bucket = storage_client.get_bucket(name)
            if bucket.location != location:
                log.error(
                    f"Bucket {bucket} exists in a differnt location {bucket.location} than expected {location}"
                )
        except exceptions.NotFound:
            log.warning(f"Bucket {name} doesn't exists")
            if not only_check:
                log.warning(f"creating bucket {name} in location {location}")
                bucket = storage_client.create_bucket(
                    bucket_or_name=name, project=project_id, location=location
                )
        if bucket:
            if delete_after_days is None and bucket.lifecycle_rules:
                log.warning(f"Unexpected {bucket.lifecycle_rules=} in bucket {name}")
                if not only_check:
                    bucket.clear_lifecyle_rules()
                    bucket.update()
            elif (
                delete_after_days is not None
                and storage.bucket.LifecycleRuleDelete(age=delete_after_days)
                not in bucket.lifecycle_rules
            ):
                log.warning(
                    f"lifecycle delete rule({delete_after_days=}) will be added to bucket {name}"
                )
                if not only_check:
                    bucket.clear_lifecyle_rules()
                    bucket.add_lifecycle_delete_rule(age=delete_after_days)
                    bucket.update()
            if not bucket.iam_configuration.uniform_bucket_level_access_enabled:
                log.warning(
                    f"uniform level access will be enabled in the bucket {name}"
                )
                if not only_check:
                    bucket.iam_configuration.uniform_bucket_level_access_enabled = True
                    bucket.patch()
