# Node-local registry

```{admonition} Maturity: preview
:class: note

The template's parameters may still change in a minor release, with a changelog entry. See the {doc}`roadmap` for every feature's status.
```

`templates.NodeLocalRegistry` deploys an OCI registry that one node pulls from
without any registry configuration on that node. Workloads reference images by
digest, pushes move only the layers the registry does not have, and nothing is
exposed on the network.

Use it for single-node clusters and for workloads pinned to one node (k3s on a
lab machine, kind, a single-board computer). For images that must reach several
nodes, see [Multi-node pulls](#multi-node-pulls-next-step).

## How it works

- The registry pod uses `hostNetwork: true` and listens only on
  `127.0.0.1:<port>` (default 5000). The kubelet and containerd on that node share
  the node network namespace, so they reach it; other hosts and ordinary pods
  (which have their own network namespace) do not.
- containerd allows plain HTTP to a registry on the loopback address, so pulling
  `127.0.0.1:5000/<repository>@sha256:…` needs no `registries.yaml`,
  `certs.d/<host>/hosts.toml` or TLS.
- Pushes go through `kubectl port-forward deployment/<name> <local>:5000`. For a
  host-network pod, the port-forward connects to the node loopback. The right to
  push is therefore the Kubernetes permission `pods/portforward` in the
  registry's namespace (plus `get` on the Deployment and its pods).
- The registry configuration is a complete `config.yml` in a ConfigMap. The image's
  default configuration would also open a debug and metrics listener on `:5001`
  on all interfaces, which with the host network is reachable from the LAN. The
  generated configuration has no debug listener.

Use `127.0.0.1`, not `localhost`, in image references: `localhost` can resolve
to `::1`, where the registry does not listen. `pull_reference()` always
returns the `127.0.0.1` form.

### Why a Deployment and not a DaemonSet

The registry holds state: the pushed images. A DaemonSet would run one registry
with separate storage on each node, and a push through a port-forward would land
in only one of them. Pull-local mode is inherently one node. That node is
pinned with a `nodeSelector` on `kubernetes.io/hostname`, and the storage (a
`ReadWriteOnce` PVC or a hostPath) lives on that node.

The Deployment uses `Recreate`: the old pod releases the loopback port and the
storage before the new pod starts. The declared container port also makes the
scheduler reserve the port on the node (for host-network pods the API sets
`hostPort` to the container port). Two registries on one port never land on the
same node.

## Deploy

```python
from piceli.k8s import templates

registry = templates.NodeLocalRegistry(node_name="worker-1", storage="20Gi")
```

Add it to a `piceli release` composition (or `kubectl apply` the manifests
from `registry.api_data()`):

```python
registry.component(ctx.namespace)  # a DeploymentComponent
```

```{note}
With a `WaitForFirstConsumer` StorageClass (kind's `standard`, k3s
`local-path`), the claim binds after it has been applied. The first `piceli
release apply` that creates the claim can then end with
`applied-resource-drift`. Plan and apply again: the claim is retained and
unchanged, and the second apply completes.
```

`examples/registry/composition.py` and `examples/registry/release.toml` show a
release that installs the registry and a workload that pulls from it. Release
with `app_replicas = 0` first, push the image, then release with
`app_replicas = 1`.

`examples/two-images/` keeps the registry in its own release and releases two
workloads from `piceli artifacts deliver --via-forward deployment/registry`
receipts; see {doc}`release_cli` (Images, "Two images, one change").

## Push and pull

```console
$ kubectl -n apps port-forward deployment/registry 15000:5000 &
$ # push by digest to 127.0.0.1:15000 (piceli.artifacts.registry.StreamedOciRegistryClient,
$ # skopeo, crane or docker push), then reference the manifest digest:
$ kubectl -n apps run app --image=127.0.0.1:5000/team/app@sha256:<manifest digest> \
    --overrides='{"spec":{"nodeSelector":{"kubernetes.io/hostname":"worker-1"}}}'
```

In code, `registry.pull_reference("team/app", digest)` returns
`127.0.0.1:5000/team/app@<digest>`. The workload must run on `node_name`; add
the same `nodeSelector`.

Push with a tag as well as by digest when the image must survive garbage
collection (see below).

## Storage

- Default: a `ReadWriteOnce` PVC `<name>-storage` annotated
  `piceli.io/retained: "true"`. `piceli release` never deletes it when the
  registry leaves a composition; the images survive a reinstall.
- `host_path="/srv/registry"`: a node directory (`DirectoryOrCreate`). `fsGroup`
  does not apply to hostPath volumes, so an init container (root, only
  `CAP_CHOWN`) gives the directory to `run_as_user`. With `run_as_user=None` the
  registry runs as the image user (root) and there is no init container.

- `existing_claim="data"`: a claim that already exists. The template never
  creates, changes or deletes it (like `ExistingClaim` in a typed app).

The registry runs as UID/GID 65532 with a read-only root filesystem, no
capabilities, no privilege escalation and the `RuntimeDefault` seccomp profile.
The container writes only to the storage volume.

## Multi-arch images

By default the registry accepts an image index (a multi-arch image) only when
every platform's manifest is present, as distribution does.
`index_platforms=["linux/arm64"]` relaxes that to the listed platforms
(`validation.manifests.indexes.platforms: list` in `config.yml`): an index
keeps its digest while the registry holds only the manifests the node can run.
`piceli deploy` sets it to the node's platform when the pipeline mirrors
images (see {ref}`deploy-mirror`).

## Take over a registry that already runs

`selector={"app": "old-registry"}` keeps the selector of a Deployment that
another owner created: Kubernetes never changes a Deployment's selector, so a
release that adopts a live registry must declare the same one. With
`name="old-registry"` and matching storage (`host_path=` or
`existing_claim=`) the release adopts the live Deployment in place
(`adopt = ["Deployment/old-registry"]` in `release.toml`) and the images on
the node survive. A Deployment created without a strategy has
`RollingUpdate`, which Kubernetes refuses to switch to `Recreate` while
another client owns its settings; `rolling_update=True` keeps a rolling update
with `maxSurge: 0` and `maxUnavailable: 1`, so the old pod still frees the
port before the new one starts. `piceli deploy` does this for you with
`NodeLoopbackRegistry(adopt="old-registry")`, after checking that the live
registry is compatible, or recreates it with `replace="old-registry"`; see
{ref}`deploy-registry-takeover`.

## Garbage collection

Deleting a manifest (`DELETE /v2/<repo>/manifests/<digest>`) removes the
reference, not the layers. `registry garbage-collect` removes blobs that no
manifest references. With `--delete-untagged` (the default here) it also removes
manifests that no tag references.

```{warning}
An image pushed **only by digest** has no tag, so `--delete-untagged` removes it.
Tag every image that workloads still use (for example with the release name),
or use `garbage_collection(delete_untagged=False)`.
```

Garbage collection must not run while the registry accepts pushes. A push that
races the mark phase can lose blobs. `registry.garbage_collection()` returns a
Job on the same node, with the same storage and configuration, that makes this
safe in one of two ways.

**Stopped mode (default).** The Job claims the registry's host port on the
node. The scheduler starts it only while the registry is not running, and the
registry cannot start again until the Job's pod has finished.

```python
gc = registry.garbage_collection()
# 1. apply registry.model_copy(update={"stopped": True})  -> replicas: 0
# 2. apply gc; wait for the Job to complete (it stays Pending while the registry runs)
# 3. apply registry (stopped=False) again
```

**Read-only mode.** Pulls keep working during collection. Distribution supports
garbage collection against a read-only registry.

```python
maintenance = registry.model_copy(update={"read_only": True})
# 1. apply maintenance  (the config digest annotation restarts the pod; pushes -> 405)
# 2. apply maintenance.garbage_collection(); wait for the Job
# 3. apply registry (read_only=False) again
```

`garbage_collection(dry_run=True)` reports what would be deleted. The Job has
`backoffLimit: 0` and is removed an hour after it finishes
(`cleanup_after_seconds`). No CronJob is generated. Unattended collection would
need to stop the registry or switch it to read-only on a schedule, which needs
RBAC to modify the Deployment. That is a decision for the operator.

## Verification on kind

These results come from a kind v0.29.0 cluster (Kubernetes v1.33.1, containerd
2.1.1, arm64). The node had no `/etc/containerd/certs.d` directory and no
registry section in `/etc/containerd/config.toml`:

- The Deployment became Ready with the `httpGet` probes to `127.0.0.1:5000/v2/`,
  running as UID 65532 with a read-only root filesystem. `ss -ltn` on the node
  showed only `127.0.0.1:5000`, and nothing on `:5001`.
- An image was pushed by digest through `kubectl port-forward deployment/registry`
  with `StreamedOciRegistryClient`. A pod with
  `image: 127.0.0.1:5000/demo/busybox@sha256:8f6f…` and `imagePullPolicy: Always`
  ran. The registry log shows `HEAD`/`GET` of the manifest and blobs with user
  agent `containerd/v2.1.1`.
- From another container on the kind Docker network, connecting to
  `<node IP>:5000` and `<node IP>:5001` failed. From the node itself,
  `http://<node IP>:5000/v2/` was refused and `http://127.0.0.1:5000/v2/`
  answered 200.
- Garbage collection: one tagged image was kept, a second image's manifest was
  deleted, and a third was pushed only by digest. The stopped-mode Job stayed
  `Pending` ("didn't have free ports") until the registry was scaled to zero.
  It then deleted the deleted image's manifest, config and layer blobs and the
  untagged manifest, and kept the tagged image's blobs. The tagged image still
  pulled after the registry restarted, and the deleted digest failed with
  `ErrImagePull`.
- Read-only mode: a blob upload returned `405`, a manifest `GET` returned `200`,
  and the Job without the port claim completed while the registry was running.
- Installed through `piceli release` with the example composition, the registry
  became Ready. After the image was pushed, a second release with
  `app_replicas = 1` ran the workload, which pulled from the registry.

## k3s

k3s embeds containerd and uses the same CRI image service. `registries.yaml`
only adds per-registry `hosts.toml` entries for the registries it lists, and
this design lists none. containerd's plain-HTTP handling for loopback registries
therefore applies unchanged. **Not verified in this change:** this was tested on
kind only, not on a k3s node. If a k3s node's `registries.yaml` defines a
wildcard (`"*"`) mirror, that entry also applies to `127.0.0.1:<port>`. Add an
explicit `127.0.0.1:5000` entry with an `http://` endpoint, or remove the
wildcard.

## Multi-node pulls (next step)

The loopback address exists only on its own node. To let other nodes pull,
the registry needs:

1. a reachable address: a Service with a stable ClusterIP, a node IP, or a
   hostname that every node resolves (node containerd does not use cluster DNS);
2. TLS for that address, with the CA distributed to each node
   (`/etc/containerd/certs.d/<host>/ca.crt`, or `configs."<host>".tls.ca_file`
   in k3s `registries.yaml`);
3. authentication for pushes (and, where needed, pulls), for example
   htpasswd with a Secret-backed password;
4. a generated node configuration: `registries.yaml` for k3s, or
   `certs.d/<host>/hosts.toml` for containerd, delivered to every node and
   followed by a runtime restart on k3s.

That mode is not implemented yet. Until then, use one node-local registry per
node that needs images, or direct node delivery ({doc}`node_delivery`).
