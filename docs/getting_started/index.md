# Getting Started

This guide takes you from nothing to a running, checked app in a disposable
cluster: you describe a small app in typed Python, look at the manifests
without a cluster, review what would change, approve it, and reach the app
from your laptop. It takes about ten minutes.

## Installation

Install Piceli with pip:

```bash
pip install piceli
```

Or add it to a project managed with [uv](https://docs.astral.sh/uv/):

```bash
uv add piceli
```

To work from a source checkout, see {doc}`../contributing/index`.

```{note}
The PyPI release can lag behind the documentation while the project is pre-alpha.
If an API described here is missing, install from the `main` branch:
`pip install git+https://github.com/pynenc/piceli.git`.
```

## Prerequisites

- Python 3.12 or later.
- [kind](https://kind.sigs.k8s.io/) (or any cluster you may freely change)
  and `kubectl` on your `PATH`. Piceli uses `kubectl` for the post-deploy check
  and for port forwards.

Piceli only talks to the kubeconfig file and context you name. It never reads
`~/.kube/config`, `KUBECONFIG` or the current context. Create a cluster whose
kubeconfig lives in its own file, and the namespace the app runs in:

```bash
kind create cluster --name hello --kubeconfig hello.kubeconfig
kubectl --kubeconfig hello.kubeconfig create namespace hello
```

## 1. Describe the app

Create `app.py` next to `hello.kubeconfig`. It holds three things: the target
(which cluster and namespace), the app (what runs there) and the pipeline
(how it gets there and how to check it):

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

- `App(name)` collects declarations and validates each one as you make it.
  Relative paths, such as the kubeconfig, resolve from the directory of
  `app.py`.
- Images are pinned by digest, so a release always runs exactly the image you
  reviewed. To build your own images instead, see {doc}`../deploy`.
- `access=app.access.forward(...)` declares how you reach the Service from your
  laptop. It renders no Kubernetes object.
- `Checks.http(...)` runs after every apply. A release is `ready` only when its
  checks pass.

See {doc}`../typed_apps` for config, secrets, volumes, network policies and
dependencies between components.

## 2. Render it without a cluster

```bash
piceli render app.py:app --namespace hello
```

`piceli render` prints one YAML document per object, grouped by component. It
never contacts a cluster and never reads secret values. Add `--format json` for
one JSON object.

## 3. Plan

```bash
piceli deploy app.py:pipeline --plan
```

The plan reads the live namespace and lists every stage: what would be
created, changed or deleted, and which checks run. It changes nothing and ends
with a combined hash:

```text
deploy plan for app.py:pipeline (until checks):
  plan     release hello-df30ac0a3d8a (create): create Deployment/web, create Service/web
  apply    apply
  checks   1 check(s)
combined hash: 8e1fecd25cec1d16dc1061fe1bbfa48e9ec7c470d55a22247fbb0115d86fd388
approve with: piceli deploy app.py:pipeline --approve 8e1fecd25cec…
```

## 4. Approve and apply

```bash
piceli deploy app.py:pipeline --approve <combined-hash>
```

Piceli re-plans and runs only if the plan still has that hash; otherwise it
refuses with `pipeline-plan-changed` and changes nothing. It creates the
objects with server-side apply, waits for readiness, runs the check, and
records every step in a journal:

```text
[inputs] skipped (no build)
[build] skipped (no build)
[deliver] skipped (no built image is used)
[plan] release hello-df30ac0a3d8a (create): 2 create
[plan] done
[apply] hello-df30ac0a3d8a: applying
[apply] done
[checks] hello-df30ac0a3d8a: passed
[checks] done
deploy ready: release hello-df30ac0a3d8a
```

If the run is interrupted, `piceli deploy app.py:pipeline --resume` continues
it. Objects that Piceli did not create are never changed unless you adopt them
explicitly.

## 5. Check it and reach it

```bash
piceli status app.py:pipeline   # is it up, and at which URLs
piceli access app.py:pipeline   # forwards http://127.0.0.1:18080/ until Ctrl-C
```

`piceli status` exits `0` when every workload is ready. `piceli access` keeps
the declared port forwards healthy and restarts them when they stop answering.
See {doc}`../access`.

## 6. Change it and deploy again

Run `piceli deploy app.py:pipeline --plan` again without changes: the plan
reports `no changes` and the apply stage is skipped
(`deployed, ready and not drifted`). Now change something, for example add
`replicas=2` to the deployment, and plan again: the new release applies only
`Deployment/web`. Approve its hash to roll it out.

When you are done, delete the cluster with `kind delete cluster --name hello`.

## Alternative: a release spec

`piceli deploy` takes a Python `Pipeline`. The same engine also runs from a
`release.toml` file with `piceli release plan` and `piceli release apply`,
which adds adoption of existing objects, generated secrets, rollback to any
earlier release and `[[checks]]` in TOML. See {doc}`../release_cli`.

## Other ways to model resources

The typed `App` is the recommended model. A composition can also use the
{doc}`Piceli templates <../kubernetes_model/piceli_templates/index>`, official
`kubernetes.client` objects, or existing YAML/JSON manifests; see
{doc}`../kubernetes_model/index` and the FAQ entry on
{ref}`existing YAML <faq-existing-yaml>`. To start from what already runs in a
namespace, see {doc}`../migrate_from_kubectl`.

## Next steps

- {doc}`../overview` covers the mental model, the engine and a glossary.
- {doc}`../deploy` adds pinned image builds and delivery to a registry or node.
- {doc}`../typed_apps` describes everything a typed app can declare.
- {doc}`../reference_app` walks through a realistic app (stateful store,
  workers, Gateway route, custom resource, network policy, SOPS secret)
  deployed to dev, staging and prod from one typed module.
- {doc}`../release_cli` covers the release spec, adoption, secrets, rollback
  and resume.
- {doc}`../cli/index` is the command overview; {doc}`../reference/cli` lists
  every option.
