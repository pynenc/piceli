# Roadmap

Piceli aims to be the reference open-source way to manage Kubernetes applications
and platform architecture in Python. That means replacing hand-written YAML,
Kustomize overlays and Helm templates with typed code, then growing into
Terraform-style infrastructure lifecycle and Argo CD-style continuous delivery.

This page summarises where the project stands and the direction it is heading.
Priorities may change. Progress is tracked in
[GitHub issues](https://github.com/pynenc/piceli/issues).

## Where Piceli stands today

| Capability | Status |
| --- | --- |
| Define resources in Python, YAML or JSON | ✅ Available |
| Typed templates for common workloads | 🟡 Core kinds only; no Ingress/Gateway, NetworkPolicy, DaemonSet, PDB, Namespace or cluster-scoped RBAC yet |
| Dependency-ordered plan and apply | ✅ Available |
| Server-side apply with preconditions, journal and resume | 🟡 Python API only; not yet used by the CLI |
| Field-level diff | 🟡 `deploy detail` (CLI engine) only |
| Safe pruning of removed resources | 🟡 Recoverable engine only, opt-in |
| Adopt or replace objects created by other tools (`release --adopt`, `--adopt-all-desired`, `--replace`) | 🟡 Preview |
| Environments and overlays (Kustomize equivalent) | ❌ Not yet: plain Python functions for now |
| Reusable, versioned packages (Helm equivalent) | ❌ Not yet |
| Custom resources (CRDs) | 🟡 As raw manifests in the recoverable engine |
| Local operations UI and JSON API | 🟡 Early preview |
| Continuous reconciliation from Git (Argo CD equivalent) | ❌ Not yet |
| Cloud infrastructure lifecycle (Terraform/OpenTofu equivalent) | ❌ Not yet; GKE cluster helpers only |

## How Piceli compares

| | Kustomize | Helm | OpenTofu / Terraform | Argo CD | Piceli (goal) |
| --- | --- | --- | --- | --- | --- |
| Language | YAML patches | Go templates over YAML | HCL | YAML (Application CRDs) | Typed Python |
| Validation before apply | Schema only | Schema only | Provider schemas | Diff in UI | Pydantic models, pure plans and admission dry-run |
| Reuse | Bases and overlays | Charts | Modules | Wraps the others | Python packages of components |
| State | None (cluster only) | Release secrets | State file and backend | Git plus cluster | Journals, revisions and release catalog |
| Drift handling | Manual | Manual | `plan` | Continuous | `plan` now, continuous later |

## Direction

The work is grouped into milestones. Each one makes Piceli usable for a bigger
audience.

### 1. Foundations

- Modern packaging and tooling: `uv`, PEP 621 metadata, `ruff`, supported Python
  3.12+, a CI matrix, trusted publishing to PyPI.
- Keep the published documentation in sync with every release.
- Remove code specific to one deployment and dependencies that are no longer used.
- Security hardening of the local operations server.

### 2. One engine

- Move `piceli deploy` onto the recoverable engine: live discovery, server-side
  apply, journal, resume, and field-level diff output.
- Keep the old delete-and-recreate behaviour only as an explicit opt-in strategy.
- Support standard kubeconfig authentication (exec plugins for GKE, EKS and AKS).
- Fix and fully test every template.

### 3. A complete model

- Templates for Namespace, Ingress and Gateway API, NetworkPolicy, DaemonSet,
  PodDisruptionBudget, ClusterRole/ClusterRoleBinding, ResourceQuota and
  LimitRange.
- A typed generic resource for any kind, including CRDs, with optional generated
  models.
- Namespaces, annotations and rollout strategies on every template.

### 4. Composition and environments

- Reusable, parameterised components: the Python counterpart of a Helm chart.
- Environment overlays (dev, staging, production) as typed configuration: the
  Python counterpart of Kustomize.
- `piceli render` to produce plain YAML, so teams can adopt Piceli gradually and
  feed its output into existing tools.

### 5. Continuous delivery

- An in-cluster controller that reconciles Git or OCI-published compositions,
  with health assessment, sync status, history and rollback.
- A web UI with a resource tree, diff and release history.

### 6. Beyond Kubernetes

- Pluggable providers for cloud resources (clusters, networks, databases, DNS),
  with shared state and locking. The aim is to interoperate with the
  OpenTofu/Terraform ecosystem rather than replace it.

## Getting involved

Feedback on priorities is welcome. Open an issue or a discussion on
[GitHub](https://github.com/pynenc/piceli).
