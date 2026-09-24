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

> **Status: pre-alpha.** APIs change between releases. `piceli deploy run` currently
> replaces (deletes and recreates) existing objects; the server-side-apply engine
> (`DeploymentSession`) is available from Python. See the
> [overview](https://docs.pynenc.org/projects/piceli/en/latest/overview.html).

## Key Features

- **Recoverable Execution API**: Pure target-bound plans, validated discovery,
  private secret versions and journaled apply/readiness/cancel/resume/compensation.
  See the [execution guide](docs/deployment_planning.md). Run
  `make local-test-env` and `make test-local-executor` for local API acceptance;
  this does not qualify or contact a live cluster.

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

- **Effortless Kubernetes Object Management**: Easily define Kubernetes resources in Python, with support for custom objects and configurations.

- **Simplified Deployment Workflow**: Piceli provides tools for planning, detailing, and executing deployments directly from your terminal, offering clear insights into the deployment process.

- **Intuitive CLI**: The Piceli CLI enhances your Kubernetes management experience, offering commands to list models, plan deployments, and more.

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

First define your kubernetes objects using piceli's templates:

```python
from piceli.k8s import templates

my_cron_job = templates.CronJob(
    name="example-cronjob",
    schedule=templates.crontab.daily_at_x(hour=6, minute=0),
    labels={"app": "myapp"},
    backoff_limit=2,
)
```

Objects from the official kubernetes library:

```python
from kubernetes import client

def create_service_example(name, labels, ports, selector):
    # Define the Kubernetes Service
    service = client.V1Service(
        api_version="v1",
        kind="Service",
        metadata=client.V1ObjectMeta(
            name=name,
            labels=labels
        ),
        spec=client.V1ServiceSpec(
            ports=ports,
            type="ClusterIP",
            selector=selector
        )
    )
    return service
```

Or a yaml or json file:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: test-service
  labels:
    service: test-service
    component: test-service
spec:
  ports:
    - name: test-service
      port: 5432
      targetPort: 5432
    - name: test-service-2
      port: 5433
      targetPort: 5434
  type: ClusterIP
  selector:
    pod_name: pod-to-select
```

Then explore the defined kubernetes object with the CLI:

```bash
piceli --folder-path=/folder/to/resources model list

╭───────────────────────────────── Command Execution ─────────────────────────────────╮
│ Running command: List Kubernetes Objects Model                                      │
╰─────────────────────────────────────────────────────────────────────────────────────╯
╭───────────────────────── Context Options ─────────────────────────╮
│ Namespace: default                                                │
│ Module Name: Not specified                                        │
│ Module Path: Not specified                                        │
│ Folder Path: /folder/to/resources                                 │
│ Include Sub-elements: True                                        │
╰───────────────────────────────────────────────────────────────────╯
┏━━━━━━━━━━━━━━━━━━┳━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Name             ┃ Kind ┃ Namespace ┃ Origin                                        ┃
┡━━━━━━━━━━━━━━━━━━╇━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ some-scheduler   │ Job  │ Default   │ OriginYAML(path='/folder/to/resources         │
│ task-scheduler   │ Job  │ Default   │ OriginYAML(path='/folder/to/resources         │
│ task-worker      │ Job  │ Default   │ OriginYAML(path='/folder/to/resources         │
│ other-job        │ Job  │ Default   │ OriginYAML(path='/folder/to/resources         │
└──────────────────┴──────┴───────────┴───────────────────────────────────────────────┘
```

The automatic deployment plan with the different steps

```bash
PICELI__FOLDER_PATH=/folder/to/resources/tmp_cli PICELI__NAMESPACE=test-run piceli deploy plan
╭───────────────────────────────── Command Execution ─────────────────────────────────╮
│ Running command: Deployment Plan                                                    │
╰─────────────────────────────────────────────────────────────────────────────────────╯
╭────────────────────────────── Context Options ───────────────────────────────╮
│ Namespace: test-run                                                          │
│ Module Name: Not specified                                                   │
│ Module Path: Not specified                                                   │
│ Folder Path: /folder/to/resources/tmp_cli                                    │
│ Include Sub-elements: True                                                   │
╰──────────────────────────────────────────────────────────────────────────────╯
Kubernetes Deployment Plan
┣━━ Step 1:
┃   ┣━━ Role example-role in namespace default
┃   ┣━━ ServiceAccount example-serviceaccount in namespace default
┃   ┗━━ StorageClass resizable in namespace default
┣━━ Step 2:
┃   ┗━━ RoleBinding example-rolebinding in namespace default
┣━━ Step 3:
┃   ┣━━ Secret example-secret in namespace default
┃   ┗━━ ConfigMap example-configmap in namespace default
┣━━ Step 4:
┃   ┗━━ PersistentVolumeClaim example-persistentvolumeclaim in namespace default
┣━━ Step 5:
┃   ┗━━ Deployment example-deployment in namespace default
┣━━ Step 6:
┃   ┗━━ Service example-service in namespace default
┣━━ Step 7:
┃   ┗━━ CronJob example-cronjob in namespace default
┗━━ Step 8:
    ┗━━ HorizontalPodAutoscaler example-horizontalpodautoscaler in namespace default
```

A detail view of all the changes that the deployment will do in the cluster:

```bash
PICELI__FOLDER_PATH=/folder/to/resources/tmp_cli PICELI__NAMESPACE=test-run piceli deploy detail
╭───────────────────────────────── Command Execution ─────────────────────────────────╮
│ Running command: Deployment Detailed Analysis                                       │
╰─────────────────────────────────────────────────────────────────────────────────────╯
╭────────────────────────────── Context Options ───────────────────────────────╮
│ Namespace: test-run                                                          │
│ Module Name: Not specified                                                   │
│ Module Path: Not specified                                                   │
│ Folder Path: /folder/to/resources/tmp_cli                                    │
│ Include Sub-elements: True                                                   │
╰──────────────────────────────────────────────────────────────────────────────╯
                                New Kubernetes Objects
┏━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━┓
┃ Kind                    ┃ Name                        ┃ Version ┃ Group             ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━┩
│ Role                    │ example-role                │ v1      │ RbacAuthorization │
│ ServiceAccount          │ example-serviceaccount      │ v1      │ Core              │
│ RoleBinding             │ example-rolebinding         │ v1      │ RbacAuthorization │
│ Secret                  │ example-secret              │ v1      │ Core              │
│ ConfigMap               │ example-configmap           │ v1      │ Core              │
│ PersistentVolumeClaim   │ example-persistentvolumecl… │ v1      │ Core              │
│ Deployment              │ example-deployment          │ v1      │ Apps              │
│ Service                 │ example-service             │ v1      │ Core              │
│ CronJob                 │ example-cronjob             │ v1      │ Batch             │
│ HorizontalPodAutoscaler │ example-horizontalpodautos… │ v2      │ Autoscaling       │
└─────────────────────────┴─────────────────────────────┴─────────┴───────────────────┘
     Kubernetes Objects Deployment Summary
┏━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━┓
┃ Kind         ┃ Name      ┃ Update Action    ┃
┡━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━┩
│ StorageClass │ resizable │ No action needed │
└──────────────┴───────────┴──────────────────┘
─────────────────────  StorageClass resizable - No action needed ──────────────────────
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Existing Object                          ┃ Desired Object                           ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ {                                        │ {                                        │
│   "allowVolumeExpansion": true,          │   "allowVolumeExpansion": true,          │
│   "apiVersion": "storage.k8s.io/v1",     │   "apiVersion": "storage.k8s.io/v1",     │
│   "kind": "StorageClass",                │   "kind": "StorageClass",                │
│   "metadata": {                          │   "metadata": {                          │
│     "creationTimestamp":                 │     "name": "resizable"                  │
│ "2024-03-06T18:01:30+00:00",             │   },                                     │
│     "managedFields": [                   │   "provisioner":                         │
│       {                                  │ "k8s.io/minikube-hostpath"               │
│         "apiVersion":                    │ }                                        │
│ "storage.k8s.io/v1",                     │                                          │
│         "fieldsType": "FieldsV1",        │                                          │
│         "fieldsV1": {                    │                                          │
│           "f:allowVolumeExpansion": {},  │                                          │
│           "f:provisioner": {},           │                                          │
│           "f:reclaimPolicy": {},         │                                          │
│           "f:volumeBindingMode": {}      │                                          │
│         },                               │                                          │
│         "manager": "OpenAPI-Generator",  │                                          │
│         "operation": "Update",           │                                          │
│         "time":                          │                                          │
│ "2024-03-06T18:01:30+00:00"              │                                          │
│       }                                  │                                          │
│     ],                                   │                                          │
│     "name": "resizable",                 │                                          │
│     "resourceVersion": "176636",         │                                          │
│     "uid":                               │                                          │
│ "9ef7f7b7-8733-40b5-9e56-ee2f555823a5"   │                                          │
│   },                                     │                                          │
│   "provisioner":                         │                                          │
│ "k8s.io/minikube-hostpath",              │                                          │
│   "reclaimPolicy": "Delete",             │                                          │
│   "volumeBindingMode": "Immediate"       │                                          │
│ }                                        │                                          │
└──────────────────────────────────────────┴──────────────────────────────────────────┘
                                  Differences Summary
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┓
┃ Path                       ┃ Type     ┃ Existing                          ┃ Desired ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━┩
│ metadata,creationTimestamp │ Ignored  │ 2024-03-06T18:01:30+00:00         │ None    │
├────────────────────────────┼──────────┼───────────────────────────────────┼─────────┤
│ metadata,managedFields     │ Ignored  │ [                                 │ None    │
│                            │          │   {                               │         │
│                            │          │     "apiVersion":                 │         │
│                            │          │ "storage.k8s.io/v1",              │         │
│                            │          │     "fieldsType": "FieldsV1",     │         │
│                            │          │     "fieldsV1": {                 │         │
│                            │          │       "f:allowVolumeExpansion":   │         │
│                            │          │ {},                               │         │
│                            │          │       "f:provisioner": {},        │         │
│                            │          │       "f:reclaimPolicy": {},      │         │
│                            │          │       "f:volumeBindingMode": {}   │         │
│                            │          │     },                            │         │
│                            │          │     "manager":                    │         │
│                            │          │ "OpenAPI-Generator",              │         │
│                            │          │     "operation": "Update",        │         │
│                            │          │     "time":                       │         │
│                            │          │ "2024-03-06T18:01:30+00:00"       │         │
│                            │          │   }                               │         │
│                            │          │ ]                                 │         │
├────────────────────────────┼──────────┼───────────────────────────────────┼─────────┤
│ metadata,uid               │ Ignored  │ 9ef7f7b7-8733-40b5-9e56-ee2f5558… │ None    │
├────────────────────────────┼──────────┼───────────────────────────────────┼─────────┤
│ metadata,resourceVersion   │ Ignored  │ 176636                            │ None    │
├────────────────────────────┼──────────┼───────────────────────────────────┼─────────┤
│ volumeBindingMode          │ Defaults │ Immediate                         │ None    │
├────────────────────────────┼──────────┼───────────────────────────────────┼─────────┤
│ reclaimPolicy              │ Defaults │ Delete                            │ None    │
└────────────────────────────┴──────────┴───────────────────────────────────┴─────────┘
```

And finally run the deployment and follow the step by step process, that will rollback to the previous state if anything unexpected happens.

```bash
PICELI__FOLDER_PATH=/folder/to/resources/tmp_cli PICELI__NAMESPACE=test-run piceli deploy run
╭───────────────────────────────── Command Execution ─────────────────────────────────╮
│ Running command: Running Deployment                                                 │
╰─────────────────────────────────────────────────────────────────────────────────────╯
Namespace 'test-run' created successfully.
╭───────────────────────────── Execution Status: PENDING ─────────────────────────────╮
│ Starting the deployment process.                                                    │
╰─────────────────────────────────────────────────────────────────────────────────────╯
──────────────────────────────────  Starting Level 0 ──────────────────────────────────
                            Applying Level 0
 ───────────────────────────────────────────────────────────────────────
  Name                     Kind             Group               Version
 ───────────────────────────────────────────────────────────────────────
  example-role             Role             RbacAuthorization   v1
  example-serviceaccount   ServiceAccount   Core                v1
  resizable                StorageClass     Storage             v1
 ───────────────────────────────────────────────────────────────────────
╭───────────────────────────── Execution Status: PENDING ─────────────────────────────╮
│ Starting the deployment process.                                                    │
╰─────────────────────────────────────────────────────────────────────────────────────╯
──────────────────────────────────  Starting Level 0 ──────────────────────────────────
                            Applying Level 0
 ───────────────────────────────────────────────────────────────────────
  Name                     Kind             Group               Version
 ───────────────────────────────────────────────────────────────────────
  example-role             Role             RbacAuthorization   v1
  example-serviceaccount   ServiceAccount   Core                v1
  resizable                StorageClass     Storage             v1
 ───────────────────────────────────────────────────────────────────────
Role example-role - Applying object
Role example-role - New object, will be created.
Role example-role - Application completed.
ServiceAccount example-serviceaccount - Applying object
ServiceAccount example-serviceaccount - New object, will be created.
ServiceAccount example-serviceaccount - Application completed.
StorageClass resizable - Applying object
StorageClass resizable - Comparing existing object...
Existing object matches the desired spec; no action needed.
StorageClass resizable - Application completed.
                   Completed Level 0
 ─────────────────────────────────────────────────────
  Name                     Kind             Status
 ─────────────────────────────────────────────────────
  example-role             Role             Completed
  example-serviceaccount   ServiceAccount   Completed
  resizable                StorageClass     Completed
 ─────────────────────────────────────────────────────
────────────────────────────  Level Completed Successfully ────────────────────────────
──────────────────────────────────  Starting Level 1 ──────────────────────────────────
                         Applying Level 1
 ─────────────────────────────────────────────────────────────────
  Name                  Kind          Group               Version
 ─────────────────────────────────────────────────────────────────
  example-rolebinding   RoleBinding   RbacAuthorization   v1
 ─────────────────────────────────────────────────────────────────
RoleBinding example-rolebinding - Applying object
RoleBinding example-rolebinding - New object, will be created.
RoleBinding example-rolebinding - Application completed.
                Completed Level 1
 ───────────────────────────────────────────────
  Name                  Kind          Status
 ───────────────────────────────────────────────
  example-rolebinding   RoleBinding   Completed
 ───────────────────────────────────────────────
────────────────────────────  Level Completed Successfully ────────────────────────────
──────────────────────────────────  Starting Level 2 ──────────────────────────────────
                 Applying Level 2
 ─────────────────────────────────────────────────
  Name                Kind        Group   Version
 ─────────────────────────────────────────────────
  example-secret      Secret      Core    v1
  example-configmap   ConfigMap   Core    v1
 ─────────────────────────────────────────────────
Secret example-secret - Applying object
ConfigMap example-configmap - Applying object
Secret example-secret - New object, will be created.
Secret example-secret - Application completed.
ConfigMap example-configmap - New object, will be created.
ConfigMap example-configmap - Application completed.
              Completed Level 2
 ───────────────────────────────────────────
  Name                Kind        Status
 ───────────────────────────────────────────
  example-secret      Secret      Completed
  example-configmap   ConfigMap   Completed
 ───────────────────────────────────────────
────────────────────────────  Level Completed Successfully ────────────────────────────
──────────────────────────────────  Starting Level 3 ──────────────────────────────────
                             Applying Level 3
 ─────────────────────────────────────────────────────────────────────────
  Name                            Kind                    Group   Version
 ─────────────────────────────────────────────────────────────────────────
  example-persistentvolumeclaim   PersistentVolumeClaim   Core    v1
 ─────────────────────────────────────────────────────────────────────────
PersistentVolumeClaim example-persistentvolumeclaim - Applying object
PersistentVolumeClaim example-persistentvolumeclaim - New object, will be created.
PersistentVolumeClaim example-persistentvolumeclaim - Application completed.
                          Completed Level 3
 ───────────────────────────────────────────────────────────────────
  Name                            Kind                    Status
 ───────────────────────────────────────────────────────────────────
  example-persistentvolumeclaim   PersistentVolumeClaim   Completed
 ───────────────────────────────────────────────────────────────────
────────────────────────────  Level Completed Successfully ────────────────────────────
──────────────────────────────────  Starting Level 4 ──────────────────────────────────
                  Applying Level 4
 ───────────────────────────────────────────────────
  Name                 Kind         Group   Version
 ───────────────────────────────────────────────────
  example-deployment   Deployment   Apps    v1
 ───────────────────────────────────────────────────
Deployment example-deployment - Applying object
Deployment example-deployment - New object, will be created.
Deployment example-deployment - Application completed.
               Completed Level 4
 ─────────────────────────────────────────────
  Name                 Kind         Status
 ─────────────────────────────────────────────
  example-deployment   Deployment   Completed
 ─────────────────────────────────────────────
────────────────────────────  Level Completed Successfully ────────────────────────────
──────────────────────────────────  Starting Level 5 ──────────────────────────────────
               Applying Level 5
 ─────────────────────────────────────────────
  Name              Kind      Group   Version
 ─────────────────────────────────────────────
  example-service   Service   Core    v1
 ─────────────────────────────────────────────
Service example-service - Applying object
Service example-service - New object, will be created.
Service example-service - Application completed.
            Completed Level 5
 ───────────────────────────────────────
  Name              Kind      Status
 ───────────────────────────────────────
  example-service   Service   Completed
 ───────────────────────────────────────
────────────────────────────  Level Completed Successfully ────────────────────────────
──────────────────────────────────  Starting Level 6 ──────────────────────────────────
               Applying Level 6
 ─────────────────────────────────────────────
  Name              Kind      Group   Version
 ─────────────────────────────────────────────
  example-cronjob   CronJob   Batch   v1
 ─────────────────────────────────────────────
CronJob example-cronjob - Applying object
CronJob example-cronjob - New object, will be created.
CronJob example-cronjob - Application completed.
╭───────────────────────────────────╮
│ Deployment completed successfully │
╰───────────────────────────────────╯
Deployment ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100% 0:00:00

```

For more information and detailed guides, check out the [Piceli Documentation](https://docs.piceli.org/).

## Requirements

Piceli aims to work seamlessly with your existing Kubernetes setup. Ensure you have `kubectl` configured and access to a Kubernetes cluster.

## License

Piceli is made available under the [MIT License](https://github.com/pynenc/piceli/blob/main/LICENSE).
