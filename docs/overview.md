# Overview

Piceli provides programmable infrastructure, source-pinned artifact delivery, and cluster operations for Kubernetes. It separates intent and preview from explicit execution grants, ensuring deployments remain reproducible, inspectable, and safely recoverable.

## Design Philosophy

Piceli is built on five core architectural principles:

1. **Explicit Authority and Durable Sessions**:
   Importing Piceli modules never contacts infrastructure or loads ambient credentials. Deployment actions require an explicit `DeploymentSession`, an authorized `ExecutionBundle`, and produce immutable journals.

2. **Owner-Operated Delivery Without Public Registries**:
   Containers are streamed directly via OCI Distribution Spec v2 to in-cluster or node-local registries. No hosted third-party registries or Docker daemon dependencies on Kubernetes worker nodes are required.

3. **Safe Garbage Collection**:
   Images are tracked across multiple dimensions (source commits, test runs, active releases, running cluster pods, and rollback targets). Invariant: **Unknown inventory never licenses deletion.** Transient inspection failures abort GC immediately.

4. **Laptop-Local Control Plane**:
   `piceli operator serve` / `piceli observe serve` operates as a developer/operator tool on `127.0.0.1`. It does not require an in-cluster controller or database server. It bridges developer access through supervised loopback `kubectl port-forward` subprocesses with 1-click shortcuts for core services.

5. **Agent and Human Ergonomics**:
   Every capability is unified across the Python library, CLI commands, versioned REST endpoints (`/v1/*`), and the reactive web UI with full semantic `data-testid` coverage for AI agents and rich visual telemetry for human operators.

## System Boundaries

- **Rustvello** owns generic scheduling, task persistence, recovery, and multi-host distribution.
- **Piceli** owns reusable programmable infrastructure, deployment planning, image delivery, execution journals, and deployment telemetry.
- **Infinite Haiku** owns domain operations, telemetry meaning, product composition, and acceptance assertions.
