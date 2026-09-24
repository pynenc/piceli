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

Piceli is under active development and its APIs change between releases. Every
cluster change goes through one recoverable engine: server-side apply with
preconditions, a durable journal and resume, always against an explicit
kubeconfig file and context. See {doc}`overview` for the architecture and
{doc}`roadmap` for the status of each feature.
```

## Where to start

::::{grid} 1 2 2 2
:gutter: 3

:::{grid-item-card} Getting started
:link: getting_started/index
:link-type: doc

Install Piceli, describe a typed app, render it and release it.
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
# infra.py
from piceli import App


def build(ctx):
    app = App("hello")
    web = app.deployment("web", image=ctx.image("web"), ports=[80])
    app.service(web, port=80)
    return app.composition(ctx)
```

```bash
# Print the manifests without a cluster, then plan and apply a release
piceli render --spec release.toml
piceli release plan --spec release.toml
piceli release apply --spec release.toml --approve <plan-hash>
```

See {doc}`getting_started/index` for the `release.toml` that names the image,
the target cluster and the namespace.

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

typed_apps
deployment_planning
release_cli
checks
plans_and_diffs
secrets
source_identity
containerized_builds
artifact_delivery
node_delivery
node_local_registry
access
operations_lens
operator_workflow
```

```{toctree}
:hidden:
:maxdepth: 2
:caption: Reference

cli/index
reference/cli
reference/errors
apidocs/index
```

```{toctree}
:hidden:
:maxdepth: 1
:caption: Agents

agents
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
