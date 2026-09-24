# NodeLocalRegistry

**An OCI registry that one node pulls from, with no registry configuration on the node.**

`NodeLocalRegistry` runs the [distribution](https://distribution.github.io/distribution/)
registry with the host network and binds it to the node loopback
(`127.0.0.1:<port>`). containerd allows plain HTTP to a registry on
`127.0.0.1`/`localhost`, so the kubelet on that node pulls
`127.0.0.1:<port>/<repository>@sha256:…` without `registries.yaml`, `certs.d` or
TLS, and the registry is not reachable from any other host. Images are pushed
through `kubectl port-forward`.

The design, the verification on kind and the maintenance procedures are in
{doc}`../../../node_local_registry`.

## Generated objects

| Object | Name | Notes |
| --- | --- | --- |
| ConfigMap | `<name>-config` | The complete registry `config.yml`. No image defaults are used. |
| PersistentVolumeClaim | `<name>-storage` | `ReadWriteOnce`, annotated `piceli.io/retained: "true"`. Not generated with `host_path`. |
| Deployment | `<name>` | 1 replica, `Recreate`, `hostNetwork: true`, `nodeSelector` on `kubernetes.io/hostname`. |

`garbage_collection()` returns a separate `NodeLocalRegistryGarbageCollection`
deployable (a Job). It is not part of `get()`, because garbage collection is a
maintenance step, not a steady state.

## Properties

- `node_name` (required): the `kubernetes.io/hostname` of the node that runs the
  registry and pulls from it.
- `name`: base name of the objects (default `registry`, at most 55 characters).
- `port`: loopback port (default `5000`, 1024–65535).
- `image`: registry image pinned by digest. The default is
  `docker.io/library/registry:3.1.1@sha256:325b4b29…a6e0` (multi-arch index).
  An image without `@sha256:<64 hex>` is rejected.
- `storage`, `storage_class`: size and class of the retained PVC.
- `host_path`: use this node directory instead of a PVC. With a non-root user, an
  init container (root, only `CAP_CHOWN`) hands the directory to that user,
  because `fsGroup` does not apply to hostPath volumes.
- `read_only`: maintenance mode. Pulls work; pushes and deletes return `405`.
- `stopped`: zero replicas (used for garbage collection).
- `run_as_user`: UID, GID and `fsGroup` (default `65532`). `None` keeps the image
  user (root).
- `cpu_request`, `memory_request`, `memory_limit`: defaults `10m`, `32Mi`, `256Mi`.
- `upload_purge_age`: incomplete uploads older than this are purged (default `168h`).
- `labels`: extra labels for every object.

Security: the container runs as non-root with a read-only root filesystem,
`allowPrivilegeEscalation: false`, all capabilities dropped and the
`RuntimeDefault` seccomp profile. Probes are `httpGet` to `127.0.0.1:<port>/v2/`
(the kubelet shares the node network namespace with a host-network pod).

## Methods

- `pull_reference(repository, digest)`: `127.0.0.1:<port>/<repository>@<digest>`.
  The module-level `templates.pull_reference(repository, digest, port=5000)` does
  the same without a template.
- `garbage_collection(name=None, delete_untagged=True, dry_run=False, cleanup_after_seconds=3600)`:
  the garbage-collection Job for this registry.
- `resource_intents(namespace)`: the manifests as `ResourceIntent` objects.
- `component(namespace, component_name="registry", dependencies=())`: a
  `DeploymentComponent` for a `piceli release` composition.

## Example

```python
from piceli.k8s import templates

registry = templates.NodeLocalRegistry(node_name="worker-1", storage="20Gi")

# workloads on worker-1 pull by digest from the node loopback
image = registry.pull_reference("team/api", "sha256:4f1c…")  # 127.0.0.1:5000/team/api@sha256:4f1c…

# garbage collection (see the guide for the procedure)
gc = registry.garbage_collection()
```

In a `piceli release` composition function:

```python
def build(ctx):
    registry = templates.NodeLocalRegistry(
        node_name=ctx.nodes["primary"].name, image=ctx.image("registry")
    )
    return DeploymentComposition(
        (
            registry.component(ctx.namespace),
            DeploymentComponent("app", (app_intent,), dependencies=("registry",)),
        )
    )
```

A complete composition and `release.toml` are in `examples/registry/`.
