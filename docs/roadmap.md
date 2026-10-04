# Roadmap

Piceli aims to be the reference open-source way to manage Kubernetes applications
and platform architecture in Python. That means replacing hand-written YAML,
Kustomize overlays and Helm templates with typed code, then growing into
Terraform-style infrastructure lifecycle. Continuous delivery from Git, per-branch
environments and a web UI are already part of it.

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
| Shared state and release lock for team CI (`state="cluster"`, plan files) | 🟡 Preview: plan on one runner, apply or resume on another |
| Server-side apply with preconditions, journal and resume | ✅ The only engine: `piceli release` and the Python API |
| Field-level diff and true no-op plans | 🟡 Preview: `release plan`/`release diff` (server dry runs) |
| Safe pruning of removed resources | 🟡 Opt-in (`prune = true`) |
| Adopt or replace objects created by other tools (`release --adopt`, `--adopt-all-desired`, `--replace`) | 🟡 Preview |
| Post-deploy checks (http, exec, metric, Python) with automatic rollback | 🟡 Preview |
| Import a live namespace or manifest files as a typed app module (`piceli import live`, `piceli import yaml`) | 🟡 Preview |
| Public fake Kubernetes API for consumers' tests (`piceli.testing`) | 🟡 Preview |
| Environments and overlays (Kustomize equivalent) | 🟡 Preview: typed `Environment` overrides, `--env`, `--diff-env` |
| Reusable, versioned packages (Helm equivalent) | ❌ Not yet |
| Custom resources (CRDs) | 🟡 Preview: `app.resource` with models from `piceli codegen crd` |
| Local operations UI and JSON API | 🟡 Early preview |
| Web workspace: infrastructure topology, compact Sources, resource inspection and saved deployment evidence | 🟡 Experimental: {doc}`ui`; one-command preview, exact version/configuration comparison, plans, journal transitions and captured logs |
| Continuous delivery from Git | 🟡 Experimental: `piceli gitops enable` installs a controller that keeps one environment per branch ({doc}`gitops`, {doc}`environments`); `piceli publish` and `render --out` export manifests |
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
| Custom resources and generated models (`app.resource`, `piceli codegen crd`) | {doc}`crds` | preview |
| Environments (`app.environment`, `--env`, `--diff-env`) | {doc}`environments` | preview |
| Reference app: dev/staging/prod in one typed module (`examples/reference`, tested on kind) | {doc}`reference_app` | preview |
| When to use Piceli, and the same app in Helm, Kustomize, cdk8s and Pulumi (`examples/comparisons`, rendered and compared in CI) | {doc}`when_to_use`, {doc}`comparisons` | preview |
| Engine: discovery, plans, executor, journals (Python API) | {doc}`deployment_planning` | preview |
| Deploy from source (`piceli deploy`, `piceli.pipeline`) | {doc}`deploy` | preview |
| Deploy a commit (`piceli deploy --ref`) | {ref}`deploy-ref` | preview |
| Deploy from CI with an approval step (GitHub Actions recipe) | {doc}`ci` | preview |
| Manifest export (`piceli publish`, `piceli render --out`) | {doc}`gitops` | preview |
| GitOps controller and per-branch environments (`piceli gitops`, `piceli env`, `piceli envs`, `piceli promote`) | {doc}`gitops`, {doc}`environments` | experimental |
| Shared deployment state, release lock, plan files, `piceli state` | {doc}`state` | preview |
| Runner hygiene: temporary-file cleanup, `piceli cache status/prune`, `cache_budget=`, `piceli doctor`, run summaries and `piceli runs` | {doc}`maintenance` | preview |
| Releases from a spec (`piceli release`) | {doc}`release_cli` | preview |
| Post-deploy checks and automatic rollback (`[[checks]]`, `piceli.checks`, `release check`) | {doc}`checks` | preview |
| Field-level diffs and no-op detection (`release plan`, `release diff`) | {doc}`plans_and_diffs` | preview |
| Supported Kubernetes versions (last four minors, kind matrix) and shared ownership (autoscalers, operators, webhooks) | {doc}`compatibility` | preview |
| Managed-cluster credentials: exec plugins for GKE, EKS, AKS, OIDC (`[target] allow_exec`) | {doc}`managed_clusters` | preview |
| Source identity (`piceli inputs`) | {doc}`source_identity` | preview |
| Containerized builds (`piceli artifacts build-spec`) | {doc}`containerized_builds` | preview |
| Pre-rollout and upgrade checks before a workload changes (`App.pre_rollout`, `UpgradeCheck`, the `prerollout` deploy stage) | {doc}`pre_rollout_checks` | experimental |
| Builds without a VM and target node facts (`Build.spec(builder="host")`) | {doc}`host_builds` | experimental |
| Multi-platform images published to a hosted registry, attested and signed (`piceli artifacts publish`) | {doc}`publishing_images` | experimental |
| Deterministic OCI builds (artifact API) | {doc}`artifact_delivery` | preview |
| Image delivery (`piceli artifacts deliver`) | {doc}`node_delivery` | preview |
| Node-local registry template | {doc}`node_local_registry` | preview |
| In-cluster registry (`Registry.in_cluster`, `piceli registry`) | {doc}`cluster_registry` | preview |
| Cluster init (`piceli.infra.Cluster`, `piceli cluster init`, `status`, `piceli secrets git`) | {doc}`cluster_init` | preview |
| Machines with OpenTofu (`piceli.infra.Server`, `Infrastructure`, `piceli infra`, Hetzner) | {doc}`infrastructure` | preview |
| Mirror third-party images by digest (`mirror=`) and take over a live node registry (`adopt=`/`replace=`) | {doc}`deploy` | preview |
| Access and status from the model (`app.access.forward`, `piceli access`, `piceli status`) | {doc}`access` | preview |
| Operations lens (`piceli observe`) | {doc}`operations_lens` | preview |
| Web workspace, saved-plan archive, revision comparison and deployment journals | {doc}`ui` | experimental |
| OIDC-scoped in-cluster UI installation | {doc}`ui_cluster_install` | experimental |
| CLI contract: `piceli explain`, `piceli help-json`, error codes | {doc}`agents` | preview |
| Owner-declared approval policy (`auto_approve`, `--approve-if-policy`) | {ref}`deploy-approval-policy` | preview |
| Agent skill (`skills/piceli`, run in CI against the built wheel) | {doc}`agents` | preview |
| Import and migration kit (`piceli import live`, `piceli import yaml`, `App.override`) | {doc}`migrate_from_kubectl` | preview |
| Test double: fake Kubernetes API (`piceli.testing`) | {doc}`testing` | preview |
| Cross-model eval of agents using Piceli (`evals/`, contributor tooling) | {doc}`contributing/evals` | experimental |
| Operator workflow (`piceli operator`) | {doc}`operator_workflow` | experimental |

## How Piceli compares

{doc}`comparisons` writes one app with Piceli, Helm, Kustomize, cdk8s and
Pulumi, tests that all five render the same objects, and compares their
size, steps and safety features; {doc}`when_to_use` is the short version.
The table below is the long-term view.

| | Kustomize | Helm | OpenTofu / Terraform | Piceli (goal) |
| --- | --- | --- | --- | --- |
| Language | YAML patches | Go templates over YAML | HCL | Typed Python |
| Validation before apply | Schema only | Schema only | Provider schemas | Pydantic models, pure plans and admission dry-run |
| Reuse | Bases and overlays | Charts | Modules | Python packages of components |
| State | None (cluster only) | Release secrets | State file and backend | Journals, revisions and release catalog |
| Drift handling | Manual | Manual | `plan` | `plan`, plus the GitOps controller's per-branch reconcile |

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
- ✅ A typed generic resource for any kind, including CRDs, with generated
  models (preview, see {doc}`crds`).
- Namespaces, annotations and rollout strategies on every template.

### 4. Composition and environments

- Reusable, parameterised components: the Python counterpart of a Helm chart.
- ✅ Environment overlays (dev, staging, production) as typed configuration:
  the Python counterpart of Kustomize (preview, see {doc}`environments`).
- ✅ `piceli render` produces plain YAML, so teams can adopt Piceli gradually and
  feed its output into existing tools.

### 5. Continuous delivery

- An in-cluster controller that reconciles Git or OCI-published compositions,
  with health assessment, sync status, history and rollback.
- ✅ Experimental web workspace with infrastructure topology, resource ownership
  diagrams, per-resource diffs, saved plans, revision comparisons and recorded
  deployment journals/logs (see {doc}`ui`). History remains bounded by the
  evidence the service stores.

### 6. Beyond Kubernetes

- Pluggable providers for cloud resources (clusters, networks, databases, DNS),
  with shared state and locking. The aim is to interoperate with the
  OpenTofu/Terraform ecosystem rather than replace it.

## Getting involved

Feedback on priorities is welcome: open an
[issue on GitHub](https://github.com/pynenc/piceli/issues).
