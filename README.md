<p align="center">
  <img src="https://raw.githubusercontent.com/pynenc/piceli/main/docs/_static/logo.webp" alt="Piceli" width="300">
</p>
<h1 align="center">Piceli</h1>
<p align="center">
    <em>Kubernetes infrastructure as typed Python: model it, plan it, apply it safely, and observe it</em>
</p>
<p align="center">
    <a href="https://pypi.org/project/piceli" target="_blank">
        <img src="https://img.shields.io/pypi/v/piceli?color=%2334D058&label=pypi%20package" alt="Package version">
    </a>
    <a href="https://pypi.org/project/piceli" target="_blank">
        <img src="https://img.shields.io/pypi/pyversions/piceli.svg?color=%2334D058" alt="Supported Python versions">
    </a>
    <a href="https://github.com/pynenc/piceli/actions/workflows/ci.yml">
        <img src="https://github.com/pynenc/piceli/actions/workflows/ci.yml/badge.svg" alt="CI">
    </a>
    <a href="https://docs.pynenc.org/projects/piceli/en/stable/" target="_blank">
        <img src="https://img.shields.io/readthedocs/piceli" alt="Documentation">
    </a>
    <a href="https://github.com/pynenc/piceli/commits/main">
        <img src="https://img.shields.io/github/last-commit/pynenc/piceli" alt="GitHub last commit">
    </a>
    <a href="https://github.com/pynenc/piceli/issues">
        <img src="https://img.shields.io/github/issues/pynenc/piceli" alt="GitHub issues">
    </a>
    <a href="https://github.com/pynenc/piceli/blob/main/LICENSE">
        <img src="https://img.shields.io/github/license/pynenc/piceli" alt="GitHub license">
    </a>
</p>

---

**Documentation**: <a href="https://docs.pynenc.org/projects/piceli/en/stable/" target="_blank">https://docs.pynenc.org/projects/piceli/en/stable/</a>

**Source Code**: <a href="https://github.com/pynenc/piceli" target="_blank">https://github.com/pynenc/piceli</a>

---

<p align="center">
  <img src="https://raw.githubusercontent.com/pynenc/piceli/main/docs/_static/img/deploy-flow.gif" alt="piceli deploy: plan, approve the plan hash, stream each stage to ready, then piceli status" width="720">
</p>

Piceli describes a Kubernetes application in typed Python, shows you exactly
what would change in the cluster, and applies only the plan you approved. Every
change is journaled, so an interrupted deploy resumes where it stopped and a
bad release rolls back. One command, `piceli deploy`, takes an app from source
to a running, checked release; `piceli status` and `piceli access` tell you
whether it is up and forward its ports to your laptop.

It is a Python-native alternative to hand-maintained YAML, Kustomize overlays
and Helm templates, and it still accepts plain YAML/JSON and `kubernetes`
client objects, so you can migrate gradually.
[When to use Piceli](https://docs.pynenc.org/projects/piceli/en/latest/when_to_use.html)
says when it fits and when Helm, Kustomize, cdk8s or Pulumi fit better, and
[the same app in all five](https://docs.pynenc.org/projects/piceli/en/latest/comparisons.html)
compares them side by side (a CI test checks that all five render the same
objects).

> **Status: pre-alpha.** APIs change between releases, always with a
> [changelog](https://docs.pynenc.org/projects/piceli/en/stable/changelog.html)
> entry. Each documentation page states its maturity (stable, preview or
> experimental); the [roadmap](https://docs.pynenc.org/projects/piceli/en/stable/roadmap.html)
> lists them all.

## What's new in 0.4.0

- **`piceli deploy`**: build, deliver by digest, plan, apply and check in one
  journaled, resumable run that skips every unchanged stage.
- **Post-deploy checks** (`http`, `exec`, `metric`, Python) with automatic
  rollback when one fails.
- **Field-level diffs and true no-op plans** (`piceli release diff`).
- **`piceli status` and `piceli access`**: is the app up, which URLs, and
  supervised loopback port forwards declared in the model.
- **`piceli import live|yaml`**: turn a running namespace or a folder of
  manifests into a typed app module.
- **One engine**: the legacy delete-and-recreate engine is gone; every change
  goes through server-side apply, the journal and resume.

See the [changelog](https://docs.pynenc.org/projects/piceli/en/stable/changelog.html)
for the complete list, including breaking changes.

## Installation

```bash
pip install piceli        # or: uv add piceli
```

Python 3.12 or later. Optional extras: `piceli[telemetry]` (OTLP deployment
telemetry), `piceli[gcp]` (GKE cluster helpers) and `piceli[aws]` (AWS Secrets
Manager as a secret source). The PyPI release can lag
behind `main` while the project is pre-alpha; to follow `main`:
`pip install git+https://github.com/pynenc/piceli.git`.

## Quick start

You need a cluster reachable through an **explicit** kubeconfig file, and
`kubectl` on your `PATH` (for checks and port forwards). A disposable
[kind](https://kind.sigs.k8s.io/) cluster works:

```bash
kind create cluster --name hello --kubeconfig hello.kubeconfig
kubectl --kubeconfig hello.kubeconfig create namespace hello
```

Describe the app, where it runs and how to check it in `app.py`:

<!-- readme-example: examples/readme/app.py -->

```python
from piceli import App, Checks, Pipeline, Target

target = Target.kubeconfig(
    "hello.kubeconfig",  # an explicit file; never ~/.kube/config
    context="kind-hello",  # an explicit context; never current-context
    namespace="hello",
)

app = App("hello")
web = app.deployment(
    "web",
    image=(
        "docker.io/library/nginx:1.27"
        "@sha256:6784fb0834aa7dbbe12e3d7471e69c290df3e6ba810dc38b34ae33d3c1c05f7d"
    ),
    ports=[80],
    ready=app.probe.http("/", 80),
)
app.service(web, port=80, access=app.access.forward(local=18080, health="/"))

pipeline = Pipeline(app, target, checks=Checks.http(web, "/", expect=200))
```

Render it, review the plan, approve that exact plan, and reach the app:

<!-- readme-example: examples/readme/commands.sh -->

```bash
piceli render app.py:app --namespace hello   # the manifests; never contacts a cluster
piceli deploy app.py:pipeline --plan         # what would change, and a combined hash
piceli deploy app.py:pipeline --approve <combined-hash>   # runs exactly that plan
piceli status app.py:pipeline                # is it up, and at which URLs
piceli access app.py:pipeline                # forwards http://127.0.0.1:18080/
```

Run `piceli deploy` again without changes and the plan reports no changes, so
nothing is applied. Change the app and only what changed moves. If a run is
interrupted,
`piceli deploy app.py:pipeline --resume` continues it.

Next steps:

- [Getting started](https://docs.pynenc.org/projects/piceli/en/stable/getting_started/index.html)
  walks through the same app step by step.
- [Deploy an app from source](https://docs.pynenc.org/projects/piceli/en/stable/deploy.html)
  adds pinned image builds and delivery to a registry or node.
- [Releases from a spec](https://docs.pynenc.org/projects/piceli/en/stable/release_cli.html)
  covers `release.toml`, adoption, secrets, rollback and resume.
- [From kubectl scripts to Piceli](https://docs.pynenc.org/projects/piceli/en/stable/migrate_from_kubectl.html)
  imports what already runs in a namespace.

## Key features

- **Typed apps**: Deployments, Services, config, secrets, volumes and network
  policies declared with `piceli.App`, validated as you write them. Templates,
  `kubernetes` client objects and YAML/JSON can be mixed into the same release.
  `piceli render` prints plain manifests without a cluster.
- **Plan before apply**: every change is computed against the live cluster
  (with server-side dry runs, so defaults are not reported as changes), shown
  field by field, and executed only with an explicit approval of its hash.
- **One recoverable engine**: server-side apply with UID and resourceVersion
  preconditions, dependency ordering, readiness waits and a durable journal.
  `resume`, `stop` and `rollback` work on every release.
- **Safe by default**: Piceli only talks to the kubeconfig file and context you
  name, never changes objects it does not own unless you adopt them, never
  prunes Namespaces, PersistentVolumes, PersistentVolumeClaims or Secrets, and
  never prints secret values.
- **Source to release**: pinned containerized builds, source identity, and
  image delivery by digest to an OCI registry, a node-local registry or a
  node's containerd. Nothing is pushed or deployed implicitly.
- **Checks and rollback**: `http`, `exec`, `metric` and Python checks run after
  every apply; a failed check can re-apply the previous release automatically.
- **Status and access**: `piceli status` says whether the app is up and how to
  reach it; `piceli access` supervises the port forwards the model declares.
- **Import**: `piceli import live` and `piceli import yaml` generate a typed
  module from running objects or existing manifests.
- **Built for agents and CI**: machine output as JSON on stdout, fixed error
  codes explained by `piceli explain`, the whole command tree with side
  effects and approval rules from `piceli help-json`, and a public fake
  Kubernetes API (`piceli.testing`) for your own tests.

## For coding agents

Start with [Using Piceli from an agent](https://docs.pynenc.org/projects/piceli/en/stable/agents.html):
which commands only read, which need the owner's approval, the output contract
and how to recover from errors. The documentation index for language models is
[`llms.txt`](https://docs.pynenc.org/projects/piceli/en/stable/llms.txt). To
change Piceli itself, read [AGENTS.md](https://github.com/pynenc/piceli/blob/main/AGENTS.md).

## Requirements

- Python 3.12 or later.
- Kubernetes 1.34 to 1.37 (the four most recent minor versions, each tested
  on kind; see
  [Supported Kubernetes versions](https://docs.pynenc.org/projects/piceli/en/latest/compatibility.html#supported-kubernetes-versions)).
- A kubeconfig file with a context for the target cluster. Piceli never uses
  `~/.kube/config`, `KUBECONFIG` or the current context implicitly: the file and
  context are named in the pipeline, the release spec or on the command line.
- `kubectl` on `PATH` for checks, `piceli access` and the node-local registry;
  `docker` with `buildx` for containerized builds.

## Contributing

Contributions are welcome. See
[CONTRIBUTING.md](https://github.com/pynenc/piceli/blob/main/CONTRIBUTING.md)
for setup, tests and pull request guidelines.

## Community and support

- **[GitHub Issues](https://github.com/pynenc/piceli/issues)**: bug reports,
  feature requests and questions
- **[Documentation](https://docs.pynenc.org/projects/piceli/en/stable/)**: guides, command
  reference and error codes
- **[Roadmap](https://docs.pynenc.org/projects/piceli/en/stable/roadmap.html)**:
  feature maturity and direction
- **Security**: report vulnerabilities privately, see
  [SECURITY.md](https://github.com/pynenc/piceli/blob/main/SECURITY.md)

Piceli is developed alongside [Pynenc](https://docs.pynenc.org), but it does
not depend on it and works with any Kubernetes workload.

## License

Piceli is made available under the [MIT License](https://github.com/pynenc/piceli/blob/main/LICENSE).
