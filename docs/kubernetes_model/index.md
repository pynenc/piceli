# Kubernetes Model

```{admonition} Maturity: stable
:class: tip

Defining objects with templates, `kubernetes` client models or YAML/JSON is stable: existing definitions keep loading within a major version. See the {doc}`../roadmap` for every feature's status.
```

Piceli plans and applies Kubernetes objects defined in any of three ways, and
they can be mixed in one composition. For whole applications, start with the
typed `App` ({doc}`../typed_apps`), which builds on the same model.

## Piceli templates

Python classes for the common kinds (Deployment, Service, ConfigMap, Job,
CronJob, RBAC and more), with defaults and validation. See
{doc}`piceli_templates/index`.

## Kubernetes client models

Objects of the official
[Kubernetes Python client](https://github.com/kubernetes-client/python)
(`kubernetes.client.V1Service` and the rest) are accepted as they are:

```python
from kubernetes import client

service = client.V1Service(
    api_version="v1",
    kind="Service",
    metadata=client.V1ObjectMeta(name="api", labels={"app": "api"}),
    spec=client.V1ServiceSpec(
        ports=[client.V1ServicePort(port=80, target_port=8080)],
        selector={"app": "api"},
    ),
)
```

## YAML and JSON files

Load a folder of manifests with `piceli.k8s.ops.load_all` and put the objects
into a composition with `component_from_objects`; {ref}`faq-existing-yaml`
shows the whole module. `load_all` also takes `module_name` (a dotted import
path) and `module_path` (a file or folder of Python modules), and loads every
Piceli template and `kubernetes.client` object they define;
`sub_elements=False` skips sub-folders and sub-modules. To turn manifests
into a typed `App` module instead, use `piceli import yaml`
({doc}`../migrate_from_kubectl`).

```{toctree}
:hidden:
:maxdepth: 2
:caption: Kubernetes Model Sections

piceli_templates/index
```
