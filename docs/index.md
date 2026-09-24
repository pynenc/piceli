# Piceli Documentation

**Kubernetes infrastructure as typed Python: model it, plan it, apply it safely, and observe it.**

Piceli lets you describe Kubernetes resources with Python (typed templates, the
official `kubernetes` client models, or plain YAML/JSON), compare that desired state
with a live cluster, and apply the difference in dependency order. It is a
Python-native alternative to hand-maintained YAML, Kustomize overlays and Helm
templates, and it has a recoverable execution engine with durable journals and
explicit authorization.

```{admonition} Project status: pre-alpha
:class: warning

Piceli is under active development and its APIs change between releases. Two
execution paths exist today:

- the **CLI path** (`piceli deploy detail/run`), which is simple and works with the
  templates, but *replaces* existing objects (delete then create) instead of
  patching them;
- the **recoverable engine** (`DeploymentSession`, `PlanExecutor`), which uses
  server-side apply, preconditions, journals and resume, but is currently a
  Python API with no CLI command.

See {doc}`overview` for how the two relate and {doc}`roadmap` for where the
project is going.
```

## Where to start

::::{grid} 1 2 2 2
:gutter: 3

:::{grid-item-card} Getting started
:link: getting_started/index
:link-type: doc

Install Piceli, define your first objects and preview a deployment.
:::

:::{grid-item-card} Overview and architecture
:link: overview
:link-type: doc

The mental model (model → plan → execute → observe), the main building blocks,
and a glossary.
:::

:::{grid-item-card} Kubernetes model
:link: kubernetes_model/index
:link-type: doc

Templates, `kubernetes` client objects, and YAML/JSON definitions.
:::

:::{grid-item-card} Recoverable deployments
:link: deployment_planning
:link-type: doc

Discovery, pure plans, authorized execution, sessions, revisions and releases.
:::

:::{grid-item-card} Operations
:link: operations_lens
:link-type: doc

A local web UI and JSON API for inventory, logs and port forwards.
:::

:::{grid-item-card} Roadmap
:link: roadmap
:link-type: doc

Status and direction compared with Kustomize, Helm, OpenTofu/Terraform and Argo CD.
:::
::::

## A first taste

```python
# myapp/infra.py
from piceli.k8s import templates

settings = templates.ConfigMap(name="report-settings", data={"LOG_LEVEL": "info"})

nightly_report = templates.CronJob(
    name="nightly-report",
    schedule=templates.crontab.daily_at_x(hour=2, minute=0),
    containers=[
        templates.Container(
            name="report",
            image="ghcr.io/example/report:1.4.2",
            command=["python", "-m", "report"],
        )
    ],
)
```

```bash
# List what Piceli loaded, then compare it with the cluster
piceli --module-name myapp.infra model list
piceli --module-name myapp.infra --namespace my-app deploy detail
```

## Part of the Pynenc ecosystem

Piceli is developed alongside [Pynenc](https://docs.pynenc.org), a distributed task
orchestration library, but it does not depend on it. It works with any Kubernetes
workload.

```{toctree}
:hidden:
:maxdepth: 2
:caption: Learn

getting_started/index
overview
kubernetes_model/index
```

```{toctree}
:hidden:
:maxdepth: 2
:caption: Guides

deployment_planning
release_cli
source_identity
containerized_builds
artifact_delivery
node_delivery
operations_lens
operator_workflow
```

```{toctree}
:hidden:
:maxdepth: 2
:caption: Reference

cli/index
apidocs/index
```

```{toctree}
:hidden:
:maxdepth: 1
:caption: Project

roadmap
faq
contributing/index
changelog
license
```
