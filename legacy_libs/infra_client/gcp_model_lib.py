from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

from infra_client.gcp import gcp_client
from infra_client.gcp.gcp_client import SQLClient
from infra_client.gcp.gcp_iam import IAMClient
from infra_client import constants as const
from k8s_client.k8s_client import Kubernetes, DryRun
from k8s_client import k8s_model_lib as k8s_lib
import logger

log = logger.get_logger(__name__)


@dataclass
class Resource(ABC):
    """Abstract GCP Infrastructure Resource"""

    @abstractmethod
    def get_name(self, client: gcp_client.GCPClient, only_check: bool) -> str:
        """gets the Resource name ie: Organization/2345223..."""

    @abstractmethod
    def apply(self, client: gcp_client.GCPClient, only_check: bool) -> None:
        """Create resource if not exists"""

    @abstractmethod
    def exists(self, client: gcp_client.GCPClient) -> bool:
        """Check if the project exists"""


@dataclass
class ResourceContainer(Resource):
    """resources containers that can be managed using Resource Manager"""

    # https://cloud.google.com/resource-manager/docs/cloud-platform-resource-hierarchy
    # https://cloud.google.com/resource-manager/reference/rest
    display_name: str


@dataclass
class Organization(ResourceContainer):
    """The organization resource represents an organization (for example, a company)
    and is the root node in the Google Cloud resource hierarchy when present.
    The organization resource is the hierarchical ancestor of folder and project resources.
    """

    # https://cloud.google.com/resource-manager/docs/cloud-platform-resource-hierarchy
    # https://cloud.google.com/resource-manager/reference/rest

    def get_name(self, client: gcp_client.GCPClient, only_check: bool) -> str:
        return client.api_organization.get_name(self.display_name, only_check)

    def apply(self, client: gcp_client.GCPClient, only_check: bool) -> None:
        client.api_organization.apply(self.display_name, only_check)

    def exists(self, client: gcp_client.GCPClient) -> bool:
        return client.api_organization.exists(self.display_name)


# {
#   "creationTime": "2020-01-07T21:59:43.314Z",
#   "displayName": "my-organization",
#   "lifecycleState": "ACTIVE",
#   "name": "organizations/34739118321",
#   "owner": {
#     "directoryCustomerId": "C012ba234"
#   }
# }


@dataclass
class Folder(ResourceContainer):
    """Folder resources optionally provide an additional grouping mechanism and isolation boundaries between projects.
    They can be seen as sub-organizations within the organization resource.
    Folder resources can be used to model different legal entities, departments, and teams within a company
    """

    parent: "ResourceContainer"

    def get_name(self, client: gcp_client.GCPClient, only_check: bool) -> str:
        return client.api_folders.get_name(self.display_name, only_check)

    def apply(self, client: gcp_client.GCPClient, only_check: bool) -> None:
        self.parent.apply(client, only_check)
        client.api_folders.apply(
            self.display_name, self.parent.get_name(client, only_check), only_check
        )

    def exists(self, client: gcp_client.GCPClient) -> bool:
        return self.parent.exists(client) and client.api_folders.exists(
            self.display_name, self.parent.get_name(client, False)
        )


@dataclass
class Project(Resource):
    """The project resource is the base-level organizing entity.
    Organization and folder resources may contain multiple projects.
    A project resource is required to use Google Cloud,
    and forms the basis for creating, enabling, and using all Google Cloud services, =
    managing APIs, enabling billing, adding and removing collaborators, and managing permissions.
    """

    project_id: str
    parent: ResourceContainer
    # TODO project valid characters
    #      a-b,0-9,-

    def get_name(self, client: gcp_client.GCPClient, only_check: bool) -> str:
        return client.api_projects.get_name(self.project_id, only_check)

    def get_number(self, client: gcp_client.GCPClient, only_check: bool) -> str:
        """get the project number"""
        name = self.get_name(client, only_check)
        return name.split("/")[1]

    def create_service_default_identity(
        self, client: gcp_client.GCPClient, service: str, only_check: bool
    ) -> None:
        """Creates the identity service account for the service in the project"""
        client.api_projects.create_service_default_identity(
            self.project_id, service, only_check
        )

    def apply(self, client: gcp_client.GCPClient, only_check: bool) -> None:
        self.parent.apply(client, only_check)
        client.api_projects.apply(
            self.project_id, self.parent.get_name(client, only_check), only_check
        )

    def exists(self, client: gcp_client.GCPClient) -> bool:
        return self.parent.exists(client) and client.api_projects.exists(
            self.project_id, self.parent.get_name(client, False)
        )


@dataclass
class ProjectResource(Resource):
    """Common class for GCP Resources within a Project"""

    name: str
    project: Project


def gb_to_mb(value: int) -> int:
    """Transform GB to MB"""
    return value << 10


@dataclass
class CloudSQL(ProjectResource):
    """GCP Cloud SQL"""

    region: str
    tier: str
    highly_available: bool
    disk_size: int  # disk size in GB, can only be increased (min 10GB)
    disk_type: SQLClient.DataDiskType = SQLClient.DataDiskType.PD_SSD
    backup_transaction_log_retention_days: int = 7
    backup_num_retained_backups: int = 7

    def __post_init__(self) -> None:
        if (
            self.backup_transaction_log_retention_days > 7
            or self.backup_transaction_log_retention_days < 1
        ):
            raise ValueError(
                "Unexpected backup_num_retained_backups: The number of days of transaction logs we retain for point in time restore, from 1-7."
            )
        if (
            self.backup_num_retained_backups > 365
            or self.backup_num_retained_backups < 1
        ):
            raise ValueError(
                "Unexpected backup_num_retained_backups: Automatica backups from 1 to 365"
            )
        if self.disk_size < 10:
            raise ValueError("Unexpected disk_size: disk size should be at least 10GB")
        if self.tier not in ["db-f1-micro", "db-g1-small"]:
            if self.tier.startswith("db-custom"):
                if len(split := self.tier.split("-")) == 4:
                    if (_cpus := split[-2]).isdigit() and (_mem := split[-1]).isdigit():
                        cpus, mem = int(_cpus), int(_mem)
                        if cpus < 1 or cpus > 32:
                            raise ValueError(
                                f"Cloud SQL number of CPUs must be between 1 and 32, not {cpus}"
                            )
                        if mem < 1 or mem > gb_to_mb(128) or mem % 256 != 0:
                            raise ValueError(
                                f"Cloud SQL memory must be a 256 MiB multiple between 1 and 128GB, not {mem}"
                            )
                    else:
                        raise ValueError(
                            f"Cloud SQL custom tier cpus(value: {split[-2]}, type: {type(split[-2]).__name__}) and memory(value: {split[-1]}, type: {type(split[-1]).__name__}) must be integers"
                        )
                else:
                    raise ValueError(
                        "Cloud SQL tier incorrect format: should match db-instance-[num CPUs]-[RAM]"
                    )
            else:
                raise ValueError(
                    "Postgresql Cloud SQL only accepts custom machine instance type (db-custom-CPU-RAM) and shared-core instance (db-f1-micro, db-g1-small)"
                )

    def get_name(self, client: gcp_client.GCPClient, only_check: bool) -> str:
        del client, only_check
        return self.name

    @property
    def instance_connection_string(self) -> str:
        """Gets the connection string necessary to connect to the instance"""
        return f"{self.project.project_id}:{self.region}:{self.name}"

    def apply(self, client: gcp_client.GCPClient, only_check: bool) -> None:
        self.project.apply(client, only_check)
        client.api_sql.apply(
            self.name,
            self.project.project_id,
            self.tier,
            self.region,
            self.highly_available,
            self.disk_size,
            self.disk_type,
            self.backup_transaction_log_retention_days,
            self.backup_num_retained_backups,
            only_check,
        )

    def exists(self, client: gcp_client.GCPClient) -> bool:
        return self.project.exists(client) and client.api_sql.exists(
            self.name, self.project.project_id
        )

    def create_user_for_sa(
        self, client: gcp_client.GCPClient, sa_name: str, only_check: bool
    ) -> None:
        """creates a new database user for an existing service account"""
        self.project.apply(client, only_check)
        self.apply(client, only_check)
        client.api_sql.create_user_for_sa(
            self.name, self.project.project_id, sa_name, only_check
        )


@dataclass
class Bucket(ProjectResource):
    """GCP Storage Bucket"""

    location: str
    delete_after_days: Optional[int] = None

    # TODO: bucket name requirements
    # https://cloud.google.com/storage/docs/buckets#naming

    def get_name(self, client: gcp_client.GCPClient, only_check: bool) -> str:
        del client, only_check
        return self.name

    def apply(self, client: gcp_client.GCPClient, only_check: bool) -> None:
        self.project.apply(client, only_check)
        client.apply_bucket(
            self.name,
            self.project.project_id,
            self.delete_after_days,
            self.location,
            only_check,
        )

    def exists(self, client: gcp_client.GCPClient) -> bool:
        return self.project.exists(client) and client.exists_bucket(self.name)


@dataclass
class WorkloadIdentity:
    """Links GKE cluster service account to Project service account"""

    # https://cloud.google.com/kubernetes-engine/docs/how-to/workload-identity#gcloud

    name: str
    permissions: list[str]
    k8s_roles: list[k8s_lib.K8sRole] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.name.endswith("-sa"):
            raise ValueError(
                "The name of the of the WI is also the name of the SA, should finish with '-sa'"
            )

    @property
    def role_name(self) -> str:
        """Get the name of the role from workloadIdentity name"""
        names = self.name.split("-")
        return "".join(map(str.capitalize, names)) + "Role"


@dataclass
class GroupMembersRoles:
    """Specify GKE roles for the members of a GCP group"""

    # https://cloud.google.com/kubernetes-engine/docs/how-to/workload-identity#gcloud

    group_name: str
    k8s_roles: list[k8s_lib.K8sRole] = field(default_factory=list)


@dataclass
class Cluster(ProjectResource):
    """GKE Cluster"""

    region: str
    # initial_node_count: int = 1
    # autopilot: bool = True
    workload_identities: list[WorkloadIdentity] = field(default_factory=list)
    group_members_roles: list[GroupMembersRoles] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._cluster_id: Optional[gcp_client.GKEClusterId] = None

    def _get_cluster_id(
        self, client: gcp_client.GCPClient, only_check: bool
    ) -> gcp_client.GKEClusterId:
        """Gets cached internal GKECLient used Cluster ID"""
        if self._cluster_id is None:
            self._cluster_id = gcp_client.GKEClusterId(
                self.name, self.project.get_name(client, only_check), self.region
            )
        return self._cluster_id

    def get_name(self, client: gcp_client.GCPClient, only_check: bool) -> str:
        """gets the name of the cluster used in GCP"""
        cluster = client.api_cluster.get(self._get_cluster_id(client, only_check))
        return cluster.name

    def get_kubeconfig(self, client: gcp_client.GCPClient, only_check: bool) -> dict:
        """Gets the kubeconfig for the kubernetes client"""
        return client.api_cluster.get_kube_config(
            self._get_cluster_id(client, only_check)
        )

    def exists(self, client: gcp_client.GCPClient) -> bool:
        return self.project.exists(client) and client.api_cluster.exists(
            self._get_cluster_id(client, False)
        )

    def _apply_workload_identities(
        self,
        client: gcp_client.GCPClient,
        only_check: bool,
        k8s_client: Kubernetes,
        dry_run: DryRun,
    ) -> None:
        for workload_identity in self.workload_identities:
            client.api_iam.apply_custom_role(
                workload_identity.role_name,
                self.project.project_id,
                workload_identity.permissions,
                only_check,
            )
            client.api_iam.apply_service_account(
                workload_identity.name, self.project.project_id, only_check
            )
            # update project.iamPolicy -> add binding project.role to project.sa
            # at project level, we add role to the (member) project.sa
            client.api_projects.appy_project_iam_binding_role_to_service_account(
                workload_identity.role_name,
                workload_identity.name,
                self.project.project_id,
                only_check,
            )
            # Create the service account for workload Identity in kubernetes
            k8s_lib.ServiceAccount(
                name=workload_identity.name,
                roles=workload_identity.k8s_roles,
                annotations={
                    "iam.gke.io/gcp-service-account": f"{workload_identity.name}@{self.project.project_id}.iam.gserviceaccount.com"
                },
            ).apply(k8s_client, dry_run=dry_run)
            # update project.serviceAccount.iamPolicy ->
            # at project.sa level, we add role WorkloadIdentity to be used by (member) k8s:sa
            client.api_projects.appy_project_service_account_iam_binding_workload_identity(
                workload_identity.name, self.project.project_id, only_check
            )

    def _apply_group_members_roles(
        self, client: gcp_client.GCPClient, k8s_client: Kubernetes, dry_run: DryRun
    ) -> None:
        for group_member_role in self.group_members_roles:
            group_email = group_member_role.group_name + "@" + const.FIRM_DOMAIN
            member_emails = client.api_directory.get_group_members(
                group_email=group_email
            )
            for role in group_member_role.k8s_roles:
                role.apply(k8s_client, dry_run=dry_run)
                rb_name = f"grp-{group_member_role.group_name}-role-{role.name}"
                rb_cls = (
                    k8s_lib.RoleBinding
                    if isinstance(role, k8s_lib.Role)
                    else k8s_lib.ClusterRoleBinding
                )
                rb_cls(name=rb_name, users=member_emails, role_name=role.name).apply(
                    k8s_client, dry_run=dry_run
                )

    def apply(self, client: gcp_client.GCPClient, only_check: bool) -> None:
        if not self.project.exists(client):
            log.warning(
                f"Both Project {self.project.project_id} and GKE cluser {self.name} will be created"
            )
            return
        self.project.apply(client, only_check)
        client.api_cluster.apply(
            self._get_cluster_id(client, only_check),
            self.project.project_id,
            only_check,
        )
        if not only_check:
            client.api_cluster.wait(self._get_cluster_id(client, only_check))
        # This will not work with AWS
        # - Workload Identity links K8S service accounts to GCP IAM service accounts
        # TODO: ON GLOBAL BUCKET/DB
        #       Automatically store global gce:bucket/aws:s3_storage in global vault
        #       code that can run in any platform will get required credentials from vault
        #       Integrate it on Storage class
        #       Remove workload_identity (or keep it for gce specific infrastructure software)
        # current:
        #       https://cloud.google.com/kubernetes-engine/docs/concepts/workload-identity
        if self.exists(client):
            dry_run = DryRun.ON if only_check else DryRun.OFF
            k8s_client = Kubernetes(self.get_kubeconfig(client, only_check))
            self._apply_workload_identities(client, only_check, k8s_client, dry_run)
            self._apply_group_members_roles(client, k8s_client, dry_run)


@dataclass
class PredefinedServiceAccount:
    """Represents a service account inside a project"""

    project: Project
    email: str

    def apply_roles(
        self,
        client: gcp_client.GCPClient,
        predefined_roles: list[str],
        only_check: bool,
    ) -> None:
        """Appy the roles to the service account"""
        self.project.apply(client, only_check)
        for role_name in predefined_roles:
            client.api_projects.appy_project_predefined_role_to_service_account_email(
                role_name, self.email, self.project.project_id, only_check
            )


@dataclass
class PredefinedRole:
    """Represents a predefined role in GCP"""

    name: str

    def validate(self, client: gcp_client.GCPClient) -> None:
        """A predefined role should already exists in GCP validate or raise an exception"""


@dataclass
class CustomRole(ProjectResource):
    """Represents a predefined role in GCP"""

    permissions: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.permissions:
            raise ValueError("CustomRole should contain at least one permission")

    def get_name(self, client: gcp_client.GCPClient, only_check: bool) -> str:
        del client, only_check
        return IAMClient.get_project_role_name(self.name, self.project.project_id)

    def apply(self, client: gcp_client.GCPClient, only_check: bool) -> None:
        self.project.apply(client, only_check)
        client.api_iam.apply_custom_role(
            self.name, self.project.project_id, list(self.permissions), only_check
        )

    def exists(self, client: gcp_client.GCPClient) -> bool:
        return self.project.exists(client) and client.api_iam.exists_custom_role(
            self.name, self.project.project_id
        )


@dataclass
class Group:
    """Represents a group inside an organization"""

    name: str
    description: str

    @property
    def email(self) -> str:
        """Gets the group email"""
        return f"{self.name}@{const.FIRM_DOMAIN}"

    def apply(self, client: gcp_client.GCPClient, only_check: bool) -> None:
        """Check or Apply the group and its description"""
        client.api_directory.apply_group(self.email, self.description, only_check)

    def exists(self, client: gcp_client.GCPClient) -> bool:
        """Check if the group exists"""
        return client.api_directory.exists_group(self.email)

    def add_roles(
        self,
        client: gcp_client.GCPClient,
        project: Project,
        predefined_roles: list[str],
        project_roles: list[str],
        only_check: bool,
    ) -> None:
        """Check appy the roles to the Group"""
        # TODO refactor roles!!!
        for role_name in project_roles:
            client.api_projects.appy_project_iam_binding_role_to_group(
                role_name, self.email, project.project_id, only_check
            )
        for role_name in predefined_roles:
            client.api_projects.appy_project_iam_binding_predefined_role_to_group(
                role_name, self.email, project.project_id, only_check
            )


@dataclass
class SecretManager:
    """Represents the Google Secret Manager inside a project"""

    project: Project

    def create_secrets_if_not_exists(
        self, client: gcp_client.GCPClient, secret_ids: set[str], only_check: bool
    ) -> None:
        """create the secrets in the project if don't already exists"""
        self.project.apply(client, only_check)
        client.api_secret_manager.create_secrets_if_not_exists(
            self.project.project_id, secret_ids, only_check
        )

    def authorize_access_to_members(
        self,
        client: gcp_client.GCPClient,
        secret_id: str,
        service_account_names: list[str],
        group_names: list[str],
        # user_names: list[str], # we do not model user permissions, just groups
        only_check: bool,
    ) -> None:
        """authorize the service account to access the secret_ids"""
        typed_members: list[str] = []
        for sa_name in service_account_names:
            typed_members.append(
                f"serviceAccount:{gcp_client.IAMClient.get_service_account_email(sa_name, self.project.project_id)}"
            )
        for group_name in group_names:
            typed_members.append(f"group:{group_name}@{const.FIRM_DOMAIN}")
        # for user_name in user_names:
        #     typed_members.append(f"user:{user_name}@{const.FIRM_DOMAIN}")
        client.api_secret_manager.authorize_access_to_typed_member(
            self.project.project_id, secret_id, typed_members, only_check
        )

    def clean_up(
        self,
        client: gcp_client.GCPClient,
        expected_secret_ids: set[str],
        to_delete_secret_ids: set[str],
        only_check: bool,
    ) -> None:
        """It will clean up Google Secret Manager of any secret that is not expected
            - Cleans up unexpected secrets and old versions
        If any unexpected secret is not in to_delete_secret_ids -> raise an Exception
        So it avoid that we automatically deleted any secret that we didn't specify
        It will also crash if finds errors that are not in use anymore until either
        we add it again to the model or delete them"""
        client.api_secret_manager.clean_up(
            self.project.project_id,
            expected_secret_ids,
            to_delete_secret_ids,
            only_check,
        )


@dataclass
class Firebase:
    """GKE Firebase"""

    project: Project
    region: str

    def apply(self, client: gcp_client.GCPClient, only_check: bool) -> None:
        """Apply the Firebase"""
        self.project.apply(client, only_check)
        client.api_firebase.apply(self.project.project_id, self.region, only_check)
