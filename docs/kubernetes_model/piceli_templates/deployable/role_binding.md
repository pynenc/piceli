# RoleBinding and ClusterRoleBinding

## RoleBinding

**A concise representation of a Kubernetes RoleBinding in Piceli.**

A `RoleBinding` grants permissions defined by a role to a user or set of users. It is namespace-scoped and can be used to grant access to resources within a specific namespace.

### RoleBinding Properties

- `name`: The name of the RoleBinding.
- `role_name`: The name of the Role that the binding refers to.
- `service_account_name`: (Optional) Name of the ServiceAccount being granted the role.
- `users`: A list of usernames that the role is being granted to.
- `resource_names`: Specific resource names the role applies to, if any.
- `labels`: (Optional) Labels to apply to the RoleBinding for organizational purposes.

### RoleBinding Example

```python
from piceli.k8s import templates

role_binding = templates.RoleBinding(
    name="test-role-binding",
    role_name="pod-reader",
    service_account_name="default",
    users=["user1@example.com"],
    labels={"environment": "dev"}
)
```

This example defines a RoleBinding named "test-role-binding" that grants the "pod-reader" role to "<user1@example.com>" and the default service account within the namespace, with a label indicating the environment as development.

## ClusterRoleBinding

**A representation of a Kubernetes ClusterRoleBinding in Piceli.**

A `ClusterRoleBinding` grants permissions defined by a role to users at the cluster level, across all namespaces. It is used to grant cluster-wide access to resources.

### ClusterRoleBinding Properties

- `name`: The name of the ClusterRoleBinding.
- `role_name`: The name of the ClusterRole that the binding refers to.
- `service_account_name`: (Optional) Name of the ServiceAccount being granted the role.
- `users`: A list of usernames that the role is being granted to.
- `labels`: (Optional) Labels to apply to the ClusterRoleBinding for identification.

### ClusterRoleBinding Example

```python
from piceli.k8s import templates

cluster_role_binding = templates.ClusterRoleBinding(
    name="test-cluster-role-binding",
    role_name="cluster-admin",
    users=["admin@example.com"],
    labels={"purpose": "admin-access"}
)
```

This example illustrates the creation of a ClusterRoleBinding named "test-cluster-role-binding" that grants "cluster-admin" privileges to "<admin@example.com>" at the cluster level, labeled to indicate its purpose for admin access.
