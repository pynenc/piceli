from abc import abstractmethod
from typing import Optional

from kubernetes import client
from pydantic import BaseModel

from piceli.k8s.constants.verbs import APIRequestVerb
from piceli.k8s.templates.auxiliary import names
from piceli.k8s.templates.auxiliary.labels import Labels
from piceli.k8s.templates.deployable import base
from piceli.k8s.utils import utils_object


class K8sRole(BaseModel):
    """
    Abstract base class for defining Kubernetes roles (`Role` and `ClusterRole`).

    :param Name name: The unique name of the role, following DNS subdomain name conventions,
        used in resource URLs, and provided by clients at creation time. This name is
        intended to be human-friendly and should be unique within a given scope at a
        particular time, with a maximum length of 63 characters.
    :param str api_group: The name of the API group that the resources belong to.
    :param str resource: The name of the resources within the API group that the role applies to.
    :param list[APIRequestVerb] verbs: A list of actions that are allowed on the resources.
    """

    name: names.Name
    api_group: str
    resource: str
    verbs: list[APIRequestVerb]

    @abstractmethod
    def get(self) -> list[client.V1Role]:
        """gets the Job definition"""


def get_role(
    role_cls: type[client.V1Role | client.V1ClusterRole],
    name: str,
    api_group: str,
    resource: str,
    resource_names: list[str],
    verbs: list[APIRequestVerb],
    labels: Optional[Labels] = None,
) -> client.V1Role | client.V1ClusterRole:
    kind = "Role" if role_cls == client.V1Role else "ClusterRole"
    api_group = "" if api_group == "core" else api_group
    return role_cls(
        api_version="rbac.authorization.k8s.io/v1",
        kind=kind,
        metadata=client.V1ObjectMeta(name=name, labels=labels),
        rules=[
            client.V1PolicyRule(
                api_groups=[api_group],
                resources=[resource],
                resource_names=resource_names if resource_names else None,
                verbs=[v.value for v in verbs],
            )
        ],
    )


class Role(K8sRole, base.Deployable):
    """
    Represents a Kubernetes Role for namespace-scoped access control.

    A Role defines permissions within a specific namespace, granting specified
    actions on resources to users or service accounts.

    Inherits common attributes from K8sRole and adds namespace-specific configuration.
    """

    api_group: str
    resource: str
    verbs: list[APIRequestVerb]
    resource_names: list[str] = []
    labels: Optional[Labels] = None

    def get(self) -> list[client.V1Role]:
        """gets the Job definition"""
        obj = get_role(
            role_cls=client.V1Role,
            name=self.name,
            api_group=self.api_group,
            resource=self.resource,
            resource_names=self.resource_names,
            verbs=self.verbs,
            labels=self.labels,
        )
        return [obj]

    @classmethod
    def from_deployable(
        cls,
        template: "base.Deployable",
        auth_verbs: Optional[list[APIRequestVerb]] = None,
    ) -> list["Role"]:
        """Creates a Role from a deployable"""
        return get_template_auth_roles(template, auth_verbs)


class ClusterRole(K8sRole, base.Deployable):
    """
    Represents a Kubernetes ClusterRole for cluster-wide access control.

    ClusterRoles grant permissions on resources across all namespaces in the cluster,
    or on a specified set of resources, irrespective of their namespace.

    Inherits from K8sRole, focusing on cluster-wide permissions without namespace specification.
    """

    api_group: str
    resource: str
    verbs: list[APIRequestVerb]
    resource_names: list[str] = []
    labels: Optional[Labels] = None

    def get(self) -> list[client.V1ClusterRole]:
        """gets the Job definition"""
        obj = get_role(
            role_cls=client.V1ClusterRole,
            name=self.name,
            api_group=self.api_group,
            resource=self.resource,
            resource_names=self.resource_names,
            verbs=self.verbs,
            labels=self.labels,
        )
        return [obj]


# Replace get_auth_role in legacy_lib
def get_template_auth_roles(
    template: base.Deployable, verbs: Optional[list[APIRequestVerb]] = None
) -> list[Role]:
    """gets the role necessaries to authorize a service account on this the K8s object"""
    roles = []
    for object in template.api_data():
        group, _ = utils_object.get_object_group_and_version(object)
        kind = object["kind"].lower()
        if verbs:
            name = kind + "-" + "-".join(sorted(v.value for v in verbs))
            if len(name) > 30:
                name = kind + "-" + "".join(sorted(v.value[0] for v in verbs))
        else:
            name = kind + "-full"
        roles.append(
            Role(
                name=name,
                api_group=group.lower(),
                resource=kind + "s",
                verbs=verbs or list(APIRequestVerb),
            )
        )
    return roles
