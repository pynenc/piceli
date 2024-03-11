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
    """Role Binding"""

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
    """Cluster Role Binding"""

    name: names.Name
    role_name: names.Name
    service_account_name: Optional[str] = None
    users: list[str] = []
    labels: Optional[Labels] = None
    # API: ClassVar[str] = "rbacauthorization"
    # API_FUNC: ClassVar[str] = "cluster_role_binding"
    # NAMESPACED: ClassVar[bool] = False

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
