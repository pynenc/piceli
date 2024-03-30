# Pod Template

Defines a common Pod template within Kubernetes, as utilized by Piceli. This model encapsulates the configuration necessary to create standardized Pod specifications, facilitating the deployment of containers and services within the Kubernetes ecosystem.

## Overview

The `Pod` class is a foundational element in defining the behavior and characteristics of containers that run on Kubernetes. It includes specifications for containers, init containers, service accounts, security contexts, and more, ensuring a comprehensive and secure deployment configuration.

## Features

- **Containers**: Define the main containers that will run in the Pod.
- **Init Containers**: Specify containers that run before the main containers are started.
- **Service Account**: Associate a service account with the Pod for Kubernetes API access.
- **Automount Service Account Token**: Control whether a service account token is automatically mounted into the Pod.
- **Restart Policy**: Set the Pod's restart policy, controlling how the containers should be restarted on failure.
- **Security Context**: Apply security settings at the Pod level, configuring user and group IDs, and enforcing best security practices.
- **Template Labels**: Attach labels to the Pod for organization and selection.
- **Image Pull Secrets**: Specify secrets needed to pull container images from private registries.
- **Termination Grace Period**: Define how long to wait before forcibly terminating a container.

## Properties

- `container_map`: A dictionary mapping container names to their configurations, aiding in the management and retrieval of specific container settings.

## Methods

- `get_pod_spec()`: Generates the `V1PodTemplateSpec` object for the Pod, assembling the container specifications, security settings, labels, and more into a cohesive Kubernetes Pod template.
- `get_label_selector()`: Constructs a label selector string from the Pod's labels, useful for identifying the Pod within Kubernetes operations.

This Pod model is crucial for defining the structural and operational aspects of containers in Kubernetes through Piceli, ensuring deployments are consistent, secure, and aligned with best practices.

## Usage in Piceli

The Pod model is integral to several key Piceli deployments:

- {replica_manager}`../deployable/replica_manager`: Utilized within `Deployment` and `StatefulSet` configurations to define the underlying Pod templates.
- {job}`../deployable/job`: Employed in defining Job and CronJob specifications, ensuring consistency and functionality across scheduled and one-off tasks.

This Pod model is crucial for defining the structural and operational aspects of containers in Kubernetes through Piceli, ensuring deployments are consistent, secure, and aligned with best practices.
