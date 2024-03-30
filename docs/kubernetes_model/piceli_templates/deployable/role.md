# Roles and Role Bindings in Kubernetes with Piceli

**A guide to using `K8sRole`, `Role`, and `ClusterRole` in Piceli for Kubernetes access control.**

Kubernetes roles and role bindings are fundamental in defining and applying access control policies within a Kubernetes cluster. Piceli simplifies the definition and management of these resources, catering to both namespace-scoped (`Role`) and cluster-wide (`ClusterRole`) permissions.

## K8sRole (Abstract Base Class)

`K8sRole` serves as an abstract base for `Role` and `ClusterRole` classes, encapsulating shared attributes and methods. It's not intended to be used directly but provides a foundation for the specific role types.

## Role

A `Role` in Kubernetes is a namespace-scoped resource that grants permissions to resources within the same namespace. In Piceli, the `Role` class allows for the programmatic creation of such roles, specifying the permissions that should be granted.

## ClusterRole

A `ClusterRole` is a cluster-wide resource that grants permissions across all namespaces. The `ClusterRole` class in Piceli enables the definition of broad access control policies applicable cluster-wide or to specific resources.

## Common Attributes

- `name`: The name of the role or cluster role, unique within its scope.
- `service_account_name`: (Optional) The name of the ServiceAccount tied to the role.
- `users`: A list of usernames that are granted the permissions defined by the role.
- `labels`: (Optional) Custom labels for organizational purposes.

## Defining a Role or ClusterRole

The process for defining a `Role` or `ClusterRole` in Piceli mirrors Kubernetes' declarative approach, specifying the permissions (verbs, API groups, and resources) that should be granted through the role.

### Example

```python
from piceli.k8s import templates

# Define a namespace-scoped Role
role = templates.Role(
    name="pod-reader",
    permissions=[{"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "watch", "list"]}],
    namespace="default"
)

# Define a cluster-wide ClusterRole
cluster_role = templates.ClusterRole(
    name="node-watcher",
    permissions=[{"apiGroups": [""], "resources": ["nodes"], "verbs": ["get", "watch", "list"]}]
)

# Define a read only Role for another deployable template
CRONJOB = templates.CronJob(
    name="test-cronjob",
    containers=[
        templates.Container(
            name="test-cronjob", image="docker-image", command=["python", "--version"]
        )
    ],
    schedule=templates.crontab.daily_at_x(hour=6, minute=0),
)
# It will create a role with read only permissions to access the cronjob
roles = templates.Role.from_deployable(
        CRONJOB, constants.APIRequestVerb.get_read_only()
    )

```

These examples demonstrate defining a Role that allows reading pods within the default namespace and a ClusterRole that permits watching nodes cluster-wide.
