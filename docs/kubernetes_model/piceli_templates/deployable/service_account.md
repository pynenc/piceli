# ServiceAccount

**Overview of Managing Kubernetes ServiceAccounts with Piceli**

A Kubernetes `ServiceAccount` provides an identity for processes that run in a Pod, allowing those processes to interact with the Kubernetes API. The `ServiceAccount` class in Piceli facilitates the creation, management, and association of roles and role bindings with service accounts for fine-grained access control within Kubernetes clusters.

## Properties

- `name`: The unique name of the service account.
- `roles`: A sequence of `K8sRole` objects (`Role` or `ClusterRole`) associated with the service account. These roles define the permissions granted to the service account.
- `annotations`: Optional annotations to add metadata to the service account. Annotations allow you to attach arbitrary non-identifying metadata.
- `labels`: Optional labels for organizing and selecting service accounts within Kubernetes.

## Creating a ServiceAccount

The `get` method constructs a `V1ServiceAccount` object along with the necessary role bindings based on the specified roles. This comprehensive approach ensures the service account is ready for deployment with all associated permissions correctly configured.

### Example

```python
from piceli.k8s import templates

# Roles for a cronjob
CRONJOB = templates.CronJob(
    name="test-cronjob",
    containers=[
        templates.Container(
            name="test-cronjob", image="docker-image", command=["python", "--version"]
        )
    ],
    schedule=templates.crontab.daily_at_x(hour=6, minute=0),
)
roles = templates.Role.from_deployable(CRONJOB)

# Service account
templates.ServiceAccount(name="test-sa", roles=roles)
```

In this example, a ServiceAccount named "my-service-account" is defined, associated with a "pod-reader" role that grants read access to pods. The service account is annotated and labeled for additional context and organization.

## Handling Role Bindings

Piceli's ServiceAccount class automatically generates appropriate RoleBinding or ClusterRoleBinding objects based on the types of roles associated with the service account, simplifying the setup of access control policies.
