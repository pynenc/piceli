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
| Typed apps (`App`, `piceli render`) that render to release intents | 🟡 Preview: Deployments, Services, ConfigMaps, Secrets, NetworkPolicies (per workload or by label), ServiceAccounts with typed RBAC (ClusterRole/ClusterRoleBinding scoped per namespace), app-level pod defaults |
| Typed templates for common workloads | 🟡 Core kinds only; no Ingress/Gateway, NetworkPolicy, DaemonSet, PDB, Namespace or cluster-scoped RBAC yet |
| Dependency-ordered plan and apply | ✅ Available |
| Image handoff by digest (build → deliver → release, immutable references only) | 🟡 Preview |
| One command from source to a verified release (`piceli deploy`) | 🟡 Preview: journaled, resumable, skips unchanged stages |
| Server-side apply with preconditions, journal and resume | ✅ The only engine: `piceli release` and the Python API |
| Field-level diff and true no-op plans | 🟡 Preview: `release plan`/`release diff` (server dry runs) |
| Safe pruning of removed resources | 🟡 Opt-in (`prune = true`) |
| Adopt or replace objects created by other tools (`release --adopt`, `--adopt-all-desired`, `--replace`) | 🟡 Preview |
| Post-deploy checks (http, exec, metric, Python) with automatic rollback | 🟡 Preview |
| Import a live namespace or manifest files as a typed app module (`piceli import live`, `piceli import yaml`) | 🟡 Preview |
| Public fake Kubernetes API for consumers' tests (`piceli.testing`) | 🟡 Preview |
| Environments and overlays (Kustomize equivalent) | ❌ Not yet: plain Python functions for now |
| Reusable, versioned packages (Helm equivalent) | ❌ Not yet |
| Custom resources (CRDs) | 🟡 As raw manifests in a composition |
| Local operations UI and JSON API | 🟡 Early preview |
| Continuous reconciliation from Git (Argo CD equivalent) | ❌ Not yet |
| Cloud infrastructure lifecycle (Terraform/OpenTofu equivalent) | ❌ Not yet; GKE cluster helpers only |

## Feature status

Every feature page starts with its maturity, and this table lists them all:

- **stable**: no breaking change within a major version; JSON output only
  gains fields.
- **preview**: works and is tested; options, file formats and JSON fields may
  still change in a minor release, always with a changelog entry.
- **experimental**: incomplete or not wired end to end; may change or be
  removed without notice.

| Feature | Page | Maturity |
| --- | --- | --- |
| Object model: templates, `kubernetes` client models, YAML/JSON (loader) | {doc}`kubernetes_model/index` | stable |
| CLI overview | {doc}`cli/index` | preview |
| Typed apps (`piceli.App`, `piceli render`) | {doc}`typed_apps` | preview |
| Engine: discovery, plans, executor, journals (Python API) | {doc}`deployment_planning` | preview |
| Deploy from source (`piceli deploy`, `piceli.pipeline`) | {doc}`deploy` | preview |
| Deploy a commit (`piceli deploy --ref`) | {ref}`deploy-ref` | preview |
| Deploy from CI with an approval step (GitHub Actions recipe) | {doc}`ci` | preview |
| Releases from a spec (`piceli release`) | {doc}`release_cli` | preview |
| Post-deploy checks and automatic rollback (`[[checks]]`, `piceli.checks`, `release check`) | {doc}`checks` | preview |
| Field-level diffs and no-op detection (`release plan`, `release diff`) | {doc}`plans_and_diffs` | preview |
| Managed-cluster credentials: exec plugins for GKE, EKS, AKS, OIDC (`[target] allow_exec`) | {doc}`managed_clusters` | preview |
| Source identity (`piceli inputs`) | {doc}`source_identity` | preview |
| Containerized builds (`piceli artifacts build-spec`) | {doc}`containerized_builds` | preview |
| Deterministic OCI builds (artifact API) | {doc}`artifact_delivery` | preview |
| Image delivery (`piceli artifacts deliver`) | {doc}`node_delivery` | preview |
| Node-local registry template | {doc}`node_local_registry` | preview |
| Mirror third-party images by digest (`mirror=`) and take over a live node registry (`adopt=`/`replace=`) | {doc}`deploy` | preview |
| Access and status from the model (`app.access.forward`, `piceli access`, `piceli status`) | {doc}`access` | preview |
| Operations lens (`piceli observe`) | {doc}`operations_lens` | preview |
| CLI contract: `piceli explain`, `piceli help-json`, error codes | {doc}`agents` | preview |
| Import and migration kit (`piceli import live`, `piceli import yaml`, `App.override`) | {doc}`migrate_from_kubectl` | preview |
| Test double: fake Kubernetes API (`piceli.testing`) | {doc}`testing` | preview |
| Operator workflow (`piceli operator`) | {doc}`operator_workflow` | experimental |

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

- ✅ Modern packaging and tooling: `uv`, PEP 621 metadata, `ruff`, supported
  Python 3.12+, a CI matrix, trusted publishing to PyPI.
- ✅ A machine-readable CLI contract for agents and CI: `piceli help-json`,
  `piceli explain`, fixed error codes and generated reference pages.
- Keep the published documentation in sync with every release.
- Remove code specific to one deployment and dependencies that are no longer used.
- Security hardening of the local operations server.

### 2. One engine

- ✅ One engine: the delete-and-recreate CLI engine (`piceli model`,
  `piceli deploy run/plan/detail`) is removed. Every change goes through live
  discovery, server-side apply, the journal and resume.
- ✅ Delete-and-recreate only as an explicit per-object opt-in (`--replace`).
- ✅ `piceli deploy`: build, delivery and release as one resumable command (preview; see {doc}`deploy`).
- ✅ Field-level diffs and true no-op plans (`piceli release diff`; preview, see
  {doc}`plans_and_diffs`).
- ✅ Standard kubeconfig authentication through exec plugins for GKE, EKS, AKS
  and OIDC (preview, see {doc}`managed_clusters`).
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
- ✅ `piceli render` produces plain YAML, so teams can adopt Piceli gradually and
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

Feedback on priorities is welcome: open an
[issue on GitHub](https://github.com/pynenc/piceli/issues).
