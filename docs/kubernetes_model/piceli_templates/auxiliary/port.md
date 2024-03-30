# Port Configuration

Defines the structure for specifying port configurations within Kubernetes resources managed by Piceli. This configuration is crucial for setting up network communication to and between pods.

## Overview

The `Port` class allows for the definition of network ports in containerized applications, specifying how services within the cluster can access pods. This setup is essential for creating accessible and efficient networking between different components of an application deployed on Kubernetes.

## Features

- **Port Number**: Specifies the port on which the container is listening.
- **Target Port**: Defines the port on the container to which the request should be forwarded. If not specified, it defaults to the same value as the port number.

This model facilitates the clear and concise definition of networking configurations, ensuring services are correctly exposed and communicated within and outside the Kubernetes cluster.

## Usage in Piceli

The Port configuration is an integral part of defining container communication within Piceli, especially in the context of how containers are accessed and communicate within the cluster:

- {container}`./container`: Ports defined using the `Port` class are utilized to specify how each container within a pod can be accessed, both from other components within the Kubernetes cluster and from external sources. This ensures that applications deployed using Piceli are networked correctly, with clear and manageable service exposure.
