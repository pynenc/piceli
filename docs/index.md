# Piceli Documentation

**Piceli: Reusable Programmable Infrastructure, Delivery, and Cluster Operations for Kubernetes.**

## Introduction

Piceli is an owner-operated infrastructure delivery and cluster operations framework. It provides declarative deployment planning, dependency-ordered execution graphs, streamed OCI artifact delivery, safe reference-counted garbage collection, and a laptop-local operations control plane without requiring external database services or public registries.

Users define infrastructure using Python composition, Piceli templates, or native Kubernetes manifests. Piceli reconciles desired intent against live cluster discovery, generating an explicit preflight deployment plan that accounts for dependencies, immutable execution journals, and safe pruning.

```{toctree}
:hidden:
:maxdepth: 2
:caption: Table of Contents

overview
deployment_planning
artifact_delivery
operations_lens
operator_workflow
getting_started/index
kubernetes_model/index
cli/index
changelog
license
```

## Core Architecture

- **Declarative Planning & Discovery Contract (v2)**: Preflight discovery with scope/API coverage validation, topological DAG execution order, and immutable journals.
- **Owner-Operated Delivery & Safe GC**: Direct OCI blob/manifest streaming to in-cluster or node-local registries without public pushes; reference-counted GC ensuring unknown inventory never deletes.
- **Laptop-Local Operations Control Plane**: `piceli operator serve` and `piceli observe serve` run locally on loopback (`http://127.0.0.1:9876`), bridging cluster services via supervised loopback port forwards with 1-click shortcuts (Kabuki, Task Monitor, Poet, Shibuya).
- **Git & PR Automation with Untrusted PR Isolation**: Opt-in branch watching, zero rebuild digest promotion, dependency-safe rollouts, and strict security isolation ensuring untrusted PR code never accesses deployment credentials.
- **Single-Instance Atomic Persistence**: Default file-backed state store (`0o600` / `0o700`) with exclusive `fcntl.flock` locking and compressed `.tar.gz` backup/restore.

## Operator Quick Start

Start the local operator web interface and REST control plane:

```bash
piceli operator serve --kubeconfig target/k-lab-p2/kubeconfig --namespace infinite-haiku-p2 --port 9876
```

Open `http://127.0.0.1:9876` in your browser to inspect cluster inventory, manage releases, view bounded logs, and toggle one-click port forwards directly to Kabuki Studio and the Rustvello/Pynenc Task Monitor.

## License

Piceli is released under the MIT License. For details, see {doc}`license`.
