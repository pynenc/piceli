from typing import Optional

from kubernetes import client

from piceli.k8s.templates.auxiliary import names
from piceli.k8s.templates.auxiliary.labels import Labels
from piceli.k8s.templates.deployable import base


def get_role_binding(
    role_binding_cls: type[client.V1RoleBinding | client.V1ClusterRoleBinding],
    name: names.Name,
    service_account_name: Optional[str],
    users: Optional[list[str]],
    role_name: names.Name,
    labels: Optional[Labels],
) -> client.V1RoleBinding | client.V1ClusterRoleBinding:
    """gets a service account"""
    kind = (
        "RoleBinding"
        if role_binding_cls == client.V1RoleBinding
        else "ClusterRoleBinding"
    )
    ref_kind = "Role" if role_binding_cls == client.V1RoleBinding else "ClusterRole"
    subjects = []
    if service_account_name:
        subjects.append(
            client.RbacV1Subject(kind="ServiceAccount", name=service_account_name)
        )
    if users:
        for user in users:
            subjects.append(client.RbacV1Subject(kind="User", name=user))
    if not subjects:
        raise ValueError("service_account_name and users cannot be both None")
    return role_binding_cls(
        api_version="rbac.authorization.k8s.io/v1",
        kind=kind,
        metadata=client.V1ObjectMeta(name=name, labels=labels),
        subjects=subjects,
        role_ref=client.V1RoleRef(
            api_group="rbac.authorization.k8s.io", kind=ref_kind, name=role_name
        ),
    )


class RoleBinding(base.Deployable):
    """
    Represents a Kubernetes RoleBinding, allowing you to assign a role to a set of users within a namespace.

    :param names.Name name: The name of the RoleBinding.
    :param str role_name: The name of the Role being bound.
    :param Optional[str] service_account_name: The ServiceAccount name, if any, the role is being assigned to.
    :param list[str] users: The list of users being granted the role.
    :param list[str] resource_names: Specific resource names the role applies to.
    :param Optional[Labels] labels: Custom labels for organizational purposes.
    """

    name: names.Name
    role_name: str
    service_account_name: Optional[str] = None
    users: list[str] = []
    resource_names: list[str] = []
    labels: Optional[Labels] = None

    def get(self) -> list[client.V1RoleBinding]:
        """gets the Job definition"""
        obj = get_role_binding(
            client.V1RoleBinding,
            self.name,
            self.service_account_name,
            self.users,
            self.role_name,
            self.labels,
        )
        return [obj]


class ClusterRoleBinding(base.Deployable):
    """
    Defines a Kubernetes ClusterRoleBinding for assigning a ClusterRole to users cluster-wide.

    :param names.Name name: The name of the ClusterRoleBinding.
    :param names.Name role_name: The name of the ClusterRole being assigned.
    :param Optional[str] service_account_name: ServiceAccount being granted the ClusterRole.
    :param list[str] users: List of users being assigned the ClusterRole.
    :param Optional[Labels] labels: Custom labels for identification and organization.
    """

    name: names.Name
    role_name: names.Name
    service_account_name: Optional[str] = None
    users: list[str] = []
    labels: Optional[Labels] = None

    def get(self) -> list[client.V1ClusterRoleBinding]:
        """gets the Job definition"""
        obj = get_role_binding(
            client.V1ClusterRoleBinding,
            self.name,
            self.service_account_name,
            self.users,
            self.role_name,
            self.labels,
        )
        return [obj]
