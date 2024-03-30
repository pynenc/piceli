# Piceli Templates for Kubernetes Objects

Piceli templates offer a structured, Pythonic way to define Kubernetes objects, enhancing readability and maintainability of your infrastructure code. These templates are part of Piceli's Python module and are divided into deployable objects and auxiliary objects to support their definition.

## Deployable Objects

Deployable objects can be directly applied to a Kubernetes cluster. They encompass the essential Kubernetes resources necessary for deploying and managing applications.

```{toctree}
:hidden:
:maxdepth: 2
:caption: Deployable Objects

deployable/autoscaler
deployable/configmap
deployable/cronjob
deployable/deployment
deployable/job
deployable/role
deployable/role_binding
deployable/secret
deployable/service
deployable/service_account
deployable/stateful_set
deployable/volume
```

- **Autoscaler**: Automatically scale your applications based on metrics. {doc}`./deployable/autoscaler`
- **ConfigMap**: Manage application configurations and settings. {doc}`./deployable/configmap`
- **CronJob**: Schedule jobs to run at specific times or intervals. {doc}`./deployable/cronjob`
- **Deployment**: Manage stateless applications deployed across your cluster. {doc}`./deployable/deployment` (based in {replica_manager}`./auxiliary/replica_manager`)
- **Job**: Execute short-lived or batch jobs within your cluster. {doc}`./deployable/job`
- **Role and ClusterRole**: Define permissions for accessing Kubernetes resources. {doc}`./deployable/role`
- **RoleBinding** and **ClusterRoleBinding**: Associate roles with specific users or groups. {doc}`./deployable/role_binding`
- **Secret**: Securely store and manage sensitive information. {doc}`./deployable/secret`
- **Service**: Define how to access applications running within your cluster. {doc}`./deployable/service`
- **ServiceAccount**: Provide an identity for processes running within your cluster. {doc}`./deployable/service_account`
- **StatefulSet**: Manage stateful applications and their storage requirements. {doc}`./deployable/stateful_set` (based in {replica_manager}`./auxiliary/replica_manager`)
- **Volume**: Manage persistent storage for your applications. {doc}`./deployable/volume`

## Auxiliary Objects

Auxiliary objects provide additional configurations and are often used in conjunction with deployable objects to fine-tune resource definitions.

:hidden:
:maxdepth: 2
:caption: Auxiliary Objects

auxiliary/container
auxiliary/crontab
auxiliary/env_vars
auxiliary/labels
auxiliary/names
auxiliary/pod
auxiliary/pod_security_context
auxiliary/port
auxiliary/quantity
auxiliary/replica_manager
auxiliary/resource_request

- **Container**: Specify container images, commands, and arguments. {doc}`./auxiliary/container`
- **Crontab**: Define the schedule for running CronJobs. {doc}`./auxiliary/crontab`
- **EnvVars**: Set environment variables for your containers. {doc}`./auxiliary/env_vars`
- **Labels**: Apply labels to Kubernetes objects for organization and selection. {doc}`./auxiliary/labels`
- **Names**: Establish naming conventions for your Kubernetes objects. {doc}`./auxiliary/names`
- **Pod**: Configure the pods that house your containers. {doc}`./auxiliary/pod`
- **PodSecurityContext**: Set security settings at the pod level. {doc}`./auxiliary/pod_security_context`
- **Port**: Define networking ports for your services and containers. {doc}`./auxiliary/port`
- **Quantity**: Specify resources like CPU and memory allocations. {doc}`./auxiliary/quantity`
- **ReplicaManager**: Abstract class with the common implementation of Deployment and StatefulSet. {doc}`./auxiliary/replica_manager`
- **ResourceRequest**: Detail resource requests and limits for optimal scheduling. {doc}`./auxiliary/resource_request`

Piceli templates not only facilitate a more organized and readable codebase but also enhance the manageability of Kubernetes resources. Explore the detailed documentation for each template to fully leverage Piceli in your Kubernetes deployments.

## Examples

Each object type is accompanied by examples demonstrating its use. Explore the examples to learn how to define and use these objects in your Piceli projects.

Piceli templates simplify the definition of Kubernetes objects, making it easy to manage your infrastructure as code. The examples provided offer a starting point for integrating Piceli templates into your project.
