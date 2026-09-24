# Frequently Asked Questions

## Why Python instead of YAML, Kustomize or Helm?

YAML has no types, functions or tests. Kustomize and Helm add reuse by patching or
templating text. Piceli builds manifests from typed Python objects, so you get
autocompletion, validation before anything reaches the cluster, loops and
functions for reuse, and ordinary unit tests for your infrastructure. It still
accepts plain YAML/JSON, so you can migrate gradually.

(faq-existing-yaml)=
## Can I keep my existing YAML files?

Yes. A composition function can load a directory of YAML or JSON manifests
(and Python modules of templates or `kubernetes.client` objects) with the
loader, and mix them with a typed app:

```python
from pathlib import Path

from piceli.k8s.ops import loader
from piceli.k8s.ops.plan import DeploymentComposition, component_from_objects

MANIFESTS = Path(__file__).parent / "manifests"


def build(ctx):
    objects = loader.load_all(
        module_name="", module_path="", folder_path=str(MANIFESTS)
    )
    return DeploymentComposition((component_from_objects("app", objects),))
```

`piceli render infra.py:build` shows what was loaded, and `piceli release`
applies it. See {doc}`kubernetes_model/yaml_json_definitions`.

## Does Piceli patch my resources or recreate them?

It patches them. Every apply uses server-side apply with UID and
resourceVersion preconditions. An object is deleted and recreated only when you
name it explicitly with `--replace Kind/name` (for example to change an
immutable field); see {doc}`release_cli`.

## Which cluster does Piceli talk to?

Only the one you name. `piceli release` reads the kubeconfig file and context
from the spec's `[target]`, and `observe`/`operator` take
`--kubeconfig`/`--context`. Piceli never reads `KUBECONFIG`, `~/.kube/config`
or the current context. From Python you pass an `ApiClient` explicitly.

## Does importing Piceli contact my cluster?

No. Importing modules, loading models and building plans are all side-effect
free. Only explicit execution and observation commands make network calls.

## Will Piceli delete resources I did not create?

Piceli only deletes objects that carry its owner annotation, and
only when pruning is explicitly enabled and discovery coverage is complete.
Namespaces, PersistentVolumes, PersistentVolumeClaims and Secrets are never
deleted. Objects present in the namespace but not declared by a release are
reported as *undeclared*; they are never adopted or removed automatically.

## Does Piceli support custom resources (CRDs)?

A composition accepts any `apiVersion`/`kind` as a raw manifest in a
`ResourceIntent`. Typed templates for custom resources are on the {doc}`roadmap`.

## Does Piceli push images to a registry?

Only when you ask it to, and never implicitly. `piceli artifacts deliver --to
oci://host:port/repo` pushes an image you have approved by config digest, and
uploads only the layers the registry is missing. Plain HTTP is only allowed to
loopback registries, for example an in-cluster registry reached through a
port-forward. Without a registry, `deliver` can also import an image straight
into a node's containerd. See {doc}`node_delivery`.

## Is the web UI safe to expose?

No. `piceli observe serve` and `piceli operator serve` bind to `127.0.0.1` only
and are meant for the operator's own machine. Do not put them behind a public
proxy.

## How does Piceli relate to Pynenc?

Both are developed within the Pynenc project, but Piceli does not depend on
Pynenc and can deploy any workload.
