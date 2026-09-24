# Getting Started

This guide installs Piceli, describes a small app in typed Python, renders it
without a cluster, and releases it to a namespace.

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

## 1. Describe the app

Create `infra.py` with a function of the release context. `App(name)` collects
declarations and validates each one as you make it:

```python
from piceli import App
from piceli.k8s.ops.plan import DeploymentComposition
from piceli.k8s.release_spec import ReleaseContext


def build(ctx: ReleaseContext) -> DeploymentComposition:
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

`ctx.image("web")` is the image pinned by digest in the spec below. See
{doc}`../typed_apps` for config, secrets, volumes, network policies and
dependencies between components.

## 2. Write the release spec

Create `release.toml` next to it. It names the cluster, the namespace, the
composition and the images:

```toml
[target]
kubeconfig = "hello.kubeconfig"   # an explicit file; never ~/.kube/config
context = "kind-hello"            # an explicit context; never current-context
namespace = "hello"               # must already exist

[release]
name = "hello"
owner = "hello"
field_manager = "hello"
composition = "infra.py:build"
state_dir = ".piceli-release"

[images]
web = "docker.io/library/nginx@sha256:<digest>"
```

Relative paths resolve from the spec's directory. Every key is described in
{doc}`../release_cli`.

## 3. Render it without a cluster

```bash
piceli render --spec release.toml
```

`piceli render` prints one YAML document per object, grouped by component. It
never contacts a cluster and never reads secret values. Add `--format json` for
one JSON object.

## 4. Plan and apply a release

Point `[target]` at a disposable cluster (for example a
[kind](https://kind.sigs.k8s.io/) cluster whose kubeconfig you export with
`kind get kubeconfig --name hello > hello.kubeconfig`), create the namespace,
then:

```console
$ piceli release plan --spec release.toml
release hello-3f2a9c1b7d20 (create, apply): 2 create
   create Deployment/web
   create Service/web
plan hash: 4123ff6e…
$ piceli release apply --spec release.toml --approve 4123ff6e…
apply hello-3f2a9c1b7d20: ready
```

`plan` reads the live namespace and prints what would change. `apply` runs
exactly that plan: it patches existing objects with server-side apply, waits
for readiness, and records every step in a journal, so an interrupted apply can
be resumed with `piceli release resume`. Objects that Piceli did not create are
never changed unless you adopt them explicitly. `piceli release rollback
previous` returns to the previous release.

```{tip}
`piceli deploy` runs the whole pipeline (verify inputs, build, deliver, plan,
apply) as one resumable command. See
[Deploy in one command](https://docs.pynenc.org/projects/piceli/en/latest/deploy.html).
```

## Other ways to model resources

The typed `App` is the recommended model. A composition function can also use
the {doc}`Piceli templates <../kubernetes_model/piceli_templates/index>`,
official `kubernetes.client` objects, or existing YAML/JSON manifests; see
{doc}`../kubernetes_model/index` and the FAQ entry on
{ref}`existing YAML <faq-existing-yaml>`.

## Next steps

- {doc}`../overview` covers the mental model, the engine and a glossary.
- {doc}`../typed_apps` describes everything a typed app can declare.
- {doc}`../release_cli` covers adoption, secrets, rollback and resume.
- {doc}`../cli/index` is the command overview; {doc}`../reference/cli` lists
  every option.
