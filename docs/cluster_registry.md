# In-cluster registry

```{admonition} Maturity: preview
:class: note

New in 0.14.0. Its parameters may still change in a minor release, with a changelog entry. See the {doc}`roadmap` for every feature's status.
```

`Registry.in_cluster(on="NODE")` is one registry inside the cluster that
**every node** pulls from by one stable name. Use it when workloads run on
several nodes and you do not want a hosted registry: images are pushed once,
by digest, and any node pulls them. For a single node, the
{doc}`node_local_registry` needs nothing on the node; for a hosted registry,
use `Registry("oci://host/prefix")`.

```python
from piceli import App, Build, Pipeline, Registry, Target

registry = Registry.in_cluster(on="worker-1", storage="20Gi")

pipeline = Pipeline(
    app,
    target,
    build=images,
    deliver=registry,  # images go to piceli-registry.piceli-system.svc:5000/<app>/…
)
```

Install it once per cluster, then deploy as usual:

```console
$ piceli registry install deploy/app.py:pipeline        # plan, exit 3, prints the hash
$ piceli registry install deploy/app.py:pipeline --approve sha256:…
$ piceli registry status deploy/app.py:pipeline
registry piceli-registry.piceli-system.svc:5000: ready
  pod piceli-registry-6f9c… on worker-1: Running (ready)
  mirror on control-plane: ready
  mirror on worker-1: ready
  mirror on worker-2: ready
  storage piceli-registry-storage: Bound, 20Gi, 1048576 bytes used
$ piceli deploy deploy/app.py:pipeline --plan
```

The argument is a pipeline whose `deliver=` is `Registry.in_cluster(…)` (its
target names the cluster), or the `Registry.in_cluster(…)` value itself. Without it,
`--on NODE --storage 20Gi --port 5000 --namespace piceli-system` describe the
registry and `--kubeconfig FILE --context NAME` (or `--profile NAME`) name the
cluster; Piceli never uses the current context.

## What is installed

In the namespace `piceli-system` (`namespace=`):

| Object | Purpose |
| --- | --- |
| `ConfigMap/piceli-registry-config` | The complete registry configuration (no debug listener; deletes allowed for {doc}`registry_retention`). |
| `PersistentVolumeClaim/piceli-registry-storage` | The images (`storage=`, `storage_class=`), annotated `piceli.io/retained`. |
| `Service/piceli-registry` | A stable ClusterIP on `port=`; with `node_port=` also a fixed NodePort. |
| `Deployment/piceli-registry` | One replica of the digest-pinned `registry` image, pinned to `on=` (the control-plane taint is tolerated, since the node was chosen by name), on the pod network, non-root, read-only root filesystem. |
| `DaemonSet/piceli-registry-mirror` | The node agent: one pod on every node (every taint tolerated) that writes the containerd mirror. |

`piceli registry uninstall` plans the removal (exit 3, then `--approve
HASH`); it keeps the namespace and, without `--delete-storage`, the claim
with the images.

## The stable name

Every image is referenced as

```text
<name>.<namespace>.svc:<port>/<repository>/<image>@sha256:<manifest digest>
piceli-registry.piceli-system.svc:5000/shop/web@sha256:…
```

The repository prefix is the app name (`repository=` to change it), so
several apps share one registry.

- **Pods** (cluster build Jobs, the GitOps controller) resolve the name with
  cluster DNS and reach the Service directly.
- **Nodes** do not use cluster DNS. The node agent writes
  `/etc/containerd/certs.d/piceli-registry.piceli-system.svc:5000/hosts.toml`
  on every node:

  ```toml
  server = "http://10.96.12.34:5000"

  [host."http://10.96.12.34:5000"]
    capabilities = ["pull", "resolve"]
  ```

  containerd then pulls that host from the Service's ClusterIP (which
  kube-proxy routes from every node) over plain HTTP. Only this host is
  affected; every other registry keeps its configuration. The agent reads the
  ClusterIP from the variable the kubelet sets in every pod
  (`PICELI_REGISTRY_SERVICE_HOST`); it removes the file when it stops
  (uninstall, drain), so an uninstalled registry leaves nothing behind.

Plain HTTP is allowed only for a loopback registry or a name of the form
`name.namespace.svc` (or `…svc.cluster.local`), which never resolves outside
the cluster: traffic stays on the cluster network. Any other host still needs
TLS (`plain-http-not-loopback`).

```{important}
containerd reads `certs.d` only when its configuration sets `config_path`.
k3s sets it to `/var/lib/rancher/k3s/agent/etc/containerd/certs.d`: pass that
as `mirror_dir=` (not verified on k3s in this release). On kind, add to the
cluster configuration:

    containerdConfigPatches:
    - |-
      [plugins."io.containerd.grpc.v1.cri".registry]
        config_path = "/etc/containerd/certs.d"

`piceli registry status` shows each node's agent as ready once the file is
written; it cannot see whether containerd reads that directory.
```

## How images get in

| Who pushes | How it reaches the registry |
| --- | --- |
| `piceli deploy` on a laptop | A supervised, loopback-only `kubectl port-forward` to `service/piceli-registry`, for the length of the push (as for `NodeLoopbackRegistry`). Only missing blobs are sent; the receipt records the stable pull reference. |
| A cluster build (`piceli build job`, the GitOps controller) | The Job pushes to `piceli-registry.piceli-system.svc:5000` by cluster DNS. |
| Mirrors (`mirror=[…]`) | Copied by digest like a laptop push. |
| Anything else | `piceli registry forward` keeps a port-forward open and prints its `push_url` (`oci://127.0.0.1:PORT/<repository>`); push with `piceli artifacts deliver … --to <push_url>/IMAGE --node-registry piceli-registry.piceli-system.svc:5000`. Or expose `node_port=` for builders outside the cluster that are configured for a plain-HTTP registry. |

`piceli deploy --plan` shows the registry in the deliver stage (`registry
piceli-registry.piceli-system.svc:5000: ready`) and refuses with
`cluster-registry-not-installed` or `cluster-registry-not-ready` until it
serves. The deploy never installs or changes the registry, and its readiness
is not part of the plan hash. Pipelines that do not use it keep their 0.13
plan hashes.

## Security

- Anything that can reach the Service on the pod network can push and pull:
  the registry has no authentication in this release. Restrict it with a
  NetworkPolicy in `piceli-system` if pods you do not trust share the cluster.
- The node agent runs as root without any capability, and writes only its own
  directory under `mirror_dir`.
- Images are always pulled by digest; a moved tag never changes what runs.

## Verification on kind

A kind cluster (v0.32, Kubernetes 1.35, containerd with `config_path` set as
above) with one control-plane and two workers
(`tests/integration/test_cluster_registry_kind.py`): the registry was
installed on one worker; status became `ready` with the mirror ready on all
three nodes; a busybox image was pushed by digest from the laptop through the
Service port-forward; a pod pinned to the other worker with
`imagePullPolicy: Always` ran `piceli-registry.piceli-system.svc:5000/e2e/busybox@sha256:…`
(the node itself cannot resolve that name, so the pull went through the
mirror); after `uninstall --delete-storage` nothing was left in the
namespace and the agent had removed `hosts.toml` from the nodes.
