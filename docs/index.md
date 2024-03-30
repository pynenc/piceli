# Welcome to Piceli's Documentation

**Piceli: An Infrastructure Management Framework for Kubernetes and Beyond.**

## Introduction

Piceli is an infrastructure management framework aimed at simplifying the orchestration and deployment across both Kubernetes environments and cloud providers. In its current state, it focuses on managing Kubernetes resources, with the goal to include comprehensive management of cloud infrastructure, such as creating and managing GKE clusters on GCP. Users can define their infrastructure using YAML, Piceli templates, or directly through Kubernetes objects from the official Kubernetes library. The Piceli CLI tool is designed to parse these definitions, generating an automatic deployment plan that accounts for dependencies and execution order.

It offers deployment management by assessing the state of existing objects to determine necessary actions, including supporting patches, replacements, and implementing rollbacks to maintain system stability and efficiency. While the current version focuses on Kubernetes, future developments aim to manage the entire cloud infrastructure stack, ensuring that the Kubernetes cluster itself, along with any dependent resources, are provisioned and managed seamlessly.

```{toctree}
:hidden:
:maxdepth: 2
:caption: Table of Contents

overview
getting_started/index
kubernetes_model/index
cli/index
apidocs/index.rst
contributing/index
faq
changelog
license
```

## Key Features

- Comprehensive orchestration for Kubernetes and future cloud provider environments.
- Designed to manage the entire infrastructure stack, including provisioning and management of cloud resources and Kubernetes clusters.
- Intuitive deployment plans with automatic dependency resolution to ensure efficient and reliable deployments.
- Deployment management that supports patches, replacements, and rollbacks for enhanced stability.
- Detailed deployment plans that take into account the current state and nuances of the cluster configuration.
- Future development will introduce configurable deployment strategies for granular control over infrastructure rollouts.

## Installation

Piceli can be installed directly via pip:

```bash
pip install piceli
```

For a detailed installation guide, including prerequisites and environment setup, see the {doc}`getting_started/index` section.

## Quick Start

Here's a simple example to define a Kubernetes deployment using a Piceli template:

```python
from piceli.k8s import templates

job = templates.Job(
    name="job0",
    containers=[
        templates.Container(
            name="c0", command=["python", "--version"], image="python:latest",
        )
    ],
)
```

And deploy it with:

```bash
PICELI__MODULE_NAME=path.to.templates piceli deploy run
```

For a step-by-step guide to your first deployment, visit the {doc}`getting_started/index` section.

## Compatibility

Piceli is designed with Kubernetes in mind but aims to extend its support to various cloud providers and infrastructure services.

## Contact or Support

Need help or want to discuss Pynenc? Check out our [GitHub Issues](https://github.com/pynenc/piceli/issues) and [GitHub Discussions](https://github.com/pynenc/piceli/discussions).

## License

Piceli is released under the MIT License. For more details, see the {doc}`license` section.
