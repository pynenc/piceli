# Piceli Documentation

**Kubernetes infrastructure as typed Python: model it, plan it, apply it safely, and observe it.**

```{image} _static/img/deploy-flow.gif
:alt: piceli deploy: plan, approve the plan hash, stream each stage to ready, then piceli status
:width: 720px
:align: center
```

Piceli lets you describe a Kubernetes application in typed Python (a typed
`App`, Piceli templates, the official `kubernetes` client models, or plain
YAML/JSON), shows you exactly what would change in the live cluster, and
applies only the plan you approved, in dependency order, with a durable journal
that makes every run resumable. `piceli deploy` takes an app from source to a
running, checked release in one command. It is a Python-native alternative to
hand-maintained YAML, Kustomize overlays and Helm templates.

```{admonition} Project status: pre-alpha
:class: warning

Piceli is under active development and its APIs change between releases. Every
cluster change goes through one recoverable engine: server-side apply with
preconditions, a durable journal and resume, always against an explicit
kubeconfig file and context. See {doc}`overview` for the architecture and
{doc}`roadmap` for the status of each feature.
```

## A first taste

```python
# app.py
from piceli import App, Checks, Pipeline, Target

target = Target.kubeconfig("hello.kubeconfig", context="kind-hello", namespace="hello")

app = App("hello")
web = app.deployment(
    "web",
    image="docker.io/library/nginx:1.27@sha256:<digest>",
    ports=[80],
    ready=app.probe.http("/", 80),
)
app.service(web, port=80, access=app.access.forward(local=18080, health="/"))

pipeline = Pipeline(app, target, checks=Checks.http(web, "/", expect=200))
```

```bash
piceli render app.py:app --namespace hello               # manifests, no cluster
piceli deploy app.py:pipeline --plan                     # review; prints a combined hash
piceli deploy app.py:pipeline --approve <combined-hash>  # runs exactly that plan
piceli status app.py:pipeline                            # is it up, which URLs
```

{doc}`getting_started/index` runs this end to end on a disposable `kind`
cluster.

## Where to start

::::{grid} 1 2 2 3
:gutter: 3

:::{grid-item-card} Getting started
:link: getting_started/index
:link-type: doc

From nothing to a running, checked app in a disposable cluster.
:::

:::{grid-item-card} Overview and architecture
:link: overview
:link-type: doc

The mental model (model → plan → execute → observe), the engine and a
glossary.
:::

:::{grid-item-card} Deploy from source
:link: deploy
:link-type: doc

`piceli deploy`: build, deliver, plan, apply and check in one resumable run.
:::

:::{grid-item-card} Releases from a spec
:link: release_cli
:link-type: doc

`release.toml`, adoption, secrets, rollback and resume.
:::

:::{grid-item-card} Coming from kubectl or YAML
:link: migrate_from_kubectl
:link-type: doc

Import a live namespace or manifest files as a typed app.
:::

:::{grid-item-card} Using Piceli from an agent
:link: agents
:link-type: doc

Safe commands, approvals, the output contract and error recovery.
:::

:::{grid-item-card} Command reference
:link: reference/cli
:link-type: doc

Every command and option, with side effects and approval rules.
:::

:::{grid-item-card} Error codes
:link: reference/errors
:link-type: doc

Every refusal code: cause, fix and whether a retry can succeed.
:::

:::{grid-item-card} Roadmap
:link: roadmap
:link-type: doc

The maturity of every feature, and where the project is heading.
:::
::::

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
:caption: Deploy and operate

deploy
ci
typed_apps
release_cli
checks
plans_and_diffs
secrets
managed_clusters
access
migrate_from_kubectl
testing
```

```{toctree}
:hidden:
:maxdepth: 2
:caption: Build and deliver images

source_identity
containerized_builds
node_delivery
node_local_registry
artifact_delivery
```

```{toctree}
:hidden:
:maxdepth: 2
:caption: Engine and advanced

deployment_planning
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
agents
apidocs/index
```

```{toctree}
:hidden:
:maxdepth: 1
:caption: Project

roadmap
faq
changelog
contributing/index
license
```
