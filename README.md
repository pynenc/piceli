<p align="center">
  <img src="https://raw.githubusercontent.com/pynenc/piceli/main/resources/piceli.logo.motherboard.000.webp" alt="Piceli" width="300">
</p>
<h1 align="center">Piceli</h1>
<p align="center">
    <em>Infrastructure management for python</em>
</p>
<p align="center">
    <a href="https://pypi.org/project/piceli" target="_blank">
        <img src="https://img.shields.io/pypi/v/piceli?color=%2334D058&label=pypi%20package" alt="Package version">
    </a>
    <a href="https://pypi.org/project/piceli" target="_blank">
        <img src="https://img.shields.io/pypi/pyversions/piceli.svg?color=%2334D058" alt="Supported Python versions">
    </a>
    <a href="https://github.com/pynenc/piceli/commits/main">
        <img src="https://img.shields.io/github/last-commit/pynenc/piceli" alt="GitHub last commit">
    </a>
    <a href="https://github.com/pynenc/piceli/graphs/contributors">
        <img src="https://img.shields.io/github/contributors/pynenc/piceli" alt="GitHub contributors">
    </a>
    <a href="https://github.com/pynenc/piceli/issues">
        <img src="https://img.shields.io/github/issues/pynenc/piceli" alt="GitHub issues">
    </a>
    <a href="https://github.com/pynenc/piceli/blob/main/LICENSE">
        <img src="https://img.shields.io/github/license/pynenc/piceli" alt="GitHub license">
    </a>
    <a href="https://github.com/pynenc/piceli/stargazers">
        <img src="https://img.shields.io/github/stars/pynenc/piceli?style=social" alt="GitHub Repo stars">
    </a>
    <a href="https://github.com/pynenc/piceli/network/members">
        <img src="https://img.shields.io/github/forks/pynenc/piceli?style=social" alt="GitHub forks">
    </a>
</p>

---

**Documentation**: <a href="https://docs.pynenc.org/projects/piceli/" target="_blank">https://docs.pynenc.org/projects/piceli</a>

**Source Code**: <a href="https://github.com/pynenc/piceli" target="_blank">https://github.com/pynenc/piceli</a>

---

Piceli manages Kubernetes infrastructure as typed Python. Define resources with
Piceli templates, the official `kubernetes` client models or plain YAML/JSON. Compare
them with a live cluster, and apply the changes in dependency order with a durable,
resumable execution journal. The goal is a Python-native replacement for
hand-maintained YAML, Kustomize and Helm, later growing into Terraform-style
infrastructure lifecycle and Argo CD-style continuous delivery (see the
[roadmap](https://docs.pynenc.org/projects/piceli/en/latest/roadmap.html)).

> **Status: pre-alpha.** APIs change between releases. Every cluster change goes
> through one engine: server-side apply with preconditions, a durable journal and
> resume, against an explicit kubeconfig file and context. See the
> [overview](https://docs.pynenc.org/projects/piceli/en/latest/overview.html).

## Key Features

- **Recoverable Execution API**: Pure target-bound plans, validated discovery,
  private secret versions and journaled apply/readiness/cancel/resume/compensation.
  See the [execution guide](docs/deployment_planning.md). Run
  `make test-acceptance` for the fault-injected API acceptance suite;
  this does not contact a live cluster.

- **Modern Streamed Container Pipeline & Micro-Images**: Decouple monolithic
  runtimes into specialized 20-30MB micro-images sharing cached base layers.
  Stream layers directly into node containerd runtimes over secure transport or
  in-cluster OCI registries (`registry:2`), eliminating multi-gigabyte disk
  archives and remote checksum stalls. Supports granular single-component
  rollouts without restarting stateful datastores.

- **Bounded Artifact Delivery**: Immutable public-source pins, deterministic
  offline OCI layouts, explicit tool grants, local-engine import, cancellation,
  secret-safe receipts and bounded OTLP deployment telemetry. See the
  [artifact delivery guide](docs/artifact_delivery.md). Nothing pushes or deploys
  implicitly.

- **Local Operations Lens**: Reconcile a durable deployment session archive with
  an explicitly selected kubeconfig, identify declared, missing, and
  archive-undeclared resources, and retain non-secret per-user loopback
  port-forward preferences. The same read-only status model is available as a
  Python library, JSON CLI, and loopback REST service. See
  [operations lens](docs/operations_lens.md).

- **Typed apps**: describe Deployments, Services, config, secrets and network
  policies with typed Python (`piceli.App`), or use templates, `kubernetes`
  client objects and YAML/JSON. `piceli render` prints the manifests without a
  cluster.

- **Releases from a spec**: `piceli release plan` shows what would change,
  `apply --approve <hash>` runs exactly that plan, and `rollback`, `resume` and
  `stop` work on every release. `piceli deploy` runs build, delivery and release
  as one resumable command.

- **Checks, status and access**: `[[checks]]` in the spec run after every
  apply (`piceli release check` reruns them), `piceli status` says whether the
  app is up and how to reach it, and `piceli access` forwards its declared
  ports to `127.0.0.1`.

- **Import**: `piceli import live` and `piceli import yaml` generate a typed
  app module from running objects or existing manifests.

- **Extensive Documentation**: Get up and running quickly with detailed guides and examples in the Piceli documentation.

## Installation

Piceli does not require a public package registry or hosted container registry.
For a private checkout, install the reviewed source directly:

```bash
python -m pip install -e /path/to/piceli
```

An organisation may package the same reviewed commit in its own package system
when that is useful, but Piceli's OCI workflow is intentionally local-first:

1. capture source and tool pins;
2. build and inspect a deterministic OCI layout locally;
3. explicitly import that layout into an approved local engine or node runtime;
4. execute a separately granted `DeploymentSession` against the target cluster.

No step pushes to a public registry, watches a repository, or changes a cluster
without an explicit command and grant. `docs/artifact_delivery.md` records the
current local import boundary; K3s/node image import remains an explicit adapter
chosen by the operator, not an ambient side effect.

To install a published release instead:

```bash
pip install piceli
```

This will install Piceli and its dependencies, preparing you for your Kubernetes management tasks.

## Local Operations Lens

The initial operations interface runs on the operator laptop. It is deliberately
small and read-only: it does not replace `DeploymentSession`, grants, journals,
or Kubernetes RBAC with a dashboard.

```bash
# Reconcile one recorded deployment with its selected cluster. Output is JSON.
piceli observe status \
  --archive ./session.archive.json \
  --kubeconfig ~/.kube/config --context my-cluster

# Save a non-secret user preference and run the resulting loopback forward.
piceli observe forward-save --user "$USER" --name api \
  --namespace my-app --target service/api \
  --local-port 18080 --remote-port 8080
piceli observe forward-run --user "$USER" --name api \
  --kubeconfig ~/.kube/config --context my-cluster

# Inspect one workload's current or previous bounded log tail.
piceli observe logs-run --namespace my-app \
  --target deployment/api --tail 200 \
  --kubeconfig ~/.kube/config --context my-cluster

# Local browser UI plus JSON API. Passing --user restores only that user's
# saved, loopback-only forwards and supervises processes Piceli starts itself.
piceli observe serve --archive ./session.archive.json \
  --kubeconfig ~/.kube/config --context my-cluster --user "$USER" --port 9876
```

Open `http://127.0.0.1:9876/` to inspect the session, live resources and saved
forwards. The status report distinguishes resources declared by the archive,
resources missing from the cluster, and objects that are visible in the namespace
but absent from that archive. "Undeclared" is information, not permission to
adopt or delete an object.

## Modern Container Pipeline & Micro-Image Delivery

For multi-tier architectures,
Piceli supports modular micro-image delivery instead of monolithic archives:

1. **Modular OCI Images**: Decompose services (frontend, gateway, workers, datastores)
   into lean, single-purpose containers (20–30 MB) built on a shared base layer.
2. **Streamed Node Import**: Stream layer bytes directly from builder stdout to the
   remote container runtime (`docker image save <tag> | ssh <node> sudo k3s ctr images import -`),
   bypassing intermediate host disk I/O and remote SD/disk checksum bottlenecks.
3. **In-Cluster OCI Layer Registry**: Optional lightweight in-cluster registry (`registry:2`)
   for zero-copy layer deduplication and instant `<1s` container restarts.
4. **Selective Rollouts**: Rebuilding and updating a stateless UI or signalling service
   rolls out only that deployment in seconds, leaving PVC-backed stateful stores
   completely warm and undisturbed.

## Quick Start Example

Describe the app in `infra.py`:

```python
from piceli import App


def build(ctx):
    app = App("hello")
    web = app.deployment(
        "web",
        image=ctx.image("web"),
        ports=[80],
        ready=app.probe.http("/", 80),
    )
    app.service(web, port=80)
    return app.composition(ctx)
```

Name the cluster, namespace and images in `release.toml`:

```toml
[target]
kubeconfig = "hello.kubeconfig"   # an explicit file; never ~/.kube/config
context = "kind-hello"            # an explicit context; never current-context
namespace = "hello"

[release]
name = "hello"
owner = "hello"
field_manager = "hello"
composition = "infra.py:build"
state_dir = ".piceli-release"

[images]
web = "docker.io/library/nginx@sha256:<digest>"
```

Render, plan and apply:

```bash
piceli render --spec release.toml                         # manifests, no cluster
piceli release plan --spec release.toml                   # prints a plan hash
piceli release apply --spec release.toml --approve <hash> # runs exactly that plan
```

Existing YAML/JSON manifests, templates and `kubernetes` client objects can be
loaded into the same composition; see the
[getting started guide](https://docs.pynenc.org/projects/piceli/en/latest/getting_started/index.html).

For more information and detailed guides, check out the [Piceli Documentation](https://docs.piceli.org/).

## Requirements

Python 3.12 or later, and a kubeconfig file with a context for the target
cluster. Piceli never uses `~/.kube/config`, `KUBECONFIG` or the current context
implicitly: the file and context are named in the release spec or on the
command line.

## License

Piceli is made available under the [MIT License](https://github.com/pynenc/piceli/blob/main/LICENSE).
