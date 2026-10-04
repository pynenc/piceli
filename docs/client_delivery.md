# Deliver to a cluster you never access

Sometimes the app runs in someone else's cluster: a client's dev cluster, an
air-gapped site, a partner's platform. You never get credentials for it, and
they should not need to read your manifests to trust them. `piceli bundle`
packages the same typed `App` you deploy yourself into one directory the
client installs with `kubectl` alone, and `piceli support-bundle` gives them a
read-only, redacted snapshot to send back when something goes wrong.

```sh
piceli artifacts build-spec run --spec host-build.toml --out receipt.json
piceli bundle app.py:pipeline --env client --receipt receipt.json --out shop-bundle
```

`piceli bundle` never contacts a cluster or a registry and writes no secret
value. A full example: `examples/client_bundle/` (a web page that shows the
cluster's identity), run end to end by `make acceptance-bundle`.

## What the bundle holds

```
shop-bundle/
  bundle.json              what it holds: objects, images (digests, platforms,
                           SBOMs), the Secrets prepare.sh makes (names and keys),
                           declared egress, safety exceptions
  base/                    kustomization.yaml and one file per object
  overlays/dev/            what the client changes:
    kustomization.yaml
    namespace.yaml         the namespace (RoleBinding subjects follow it)
    images.yaml            registry and digest of each image
    resources.yaml         requests and limits of every container
    storage.yaml           storage class and size of every claim
  prepare.sh               makes the generated Secrets in the client's cluster
  images/<name>.oci.tar    OCI image layout per image
  images/<name>.<os>-<arch>.sbom.spdx.json, .provenance.json
  INSTALL.md, UNINSTALL.md the exact commands
  SHA256SUMS               sha256 of every other file
```

- **Every object** carries `app.kubernetes.io/part-of=<name>` (and its pods
  too; selectors are never changed) and the annotation
  `piceli.io/bundle: <name>@<version>`. `<name>` is the render's part-of
  label (the App name by default) or `--name`.
- **No Secret is shipped.** Each Secret the app declares is bound to outputs
  of the pipeline's generators (`Secrets(Random, Template, TlsCa, …)`, see
  {doc}`secrets`); `prepare.sh NAMESPACE` generates them in the client's
  cluster with `openssl` and creates them with `kubectl`: random tokens
  (`Random(n)`: the same URL-safe form as a release), templates, static
  values, a private CA with its leaf certificates (the CA key never leaves
  the script's private temporary directory). Values from sources Piceli
  cannot generate (`Sops`, `Vault`, `AwsSecret`, an import) are read from
  files the client names (`PICELI_INPUT_<NAME>=path`). The script is POSIX
  `sh`, idempotent and all-or-nothing (every Secret present: nothing to do;
  none: create all; some: refuse and name them), and never prints a value.
  An object other than a Secret that holds a secret value is refused
  (`bundle-secret-value`).
- **Images.** With `--receipt` (a build receipt of `piceli artifacts
  build-spec run`, as for {doc}`publishing_images`), every build image the
  app uses becomes `images/<name>.oci.tar`, an OCI image layout copied byte
  for byte from the build's archives (each layer checked against the
  config): `linux/amd64` by default, `--platform` (repeatable) or
  `--all-platforms` for a multi-architecture index. The digest is the
  build's, and `skopeo copy --all --preserve-digests` keeps it in the
  client's registry. Each platform's SBOM (SPDX) and provenance (in-toto)
  from the receipt are copied next to it. In the base the image is named
  `images.piceli.invalid/<name>` (a name that never resolves);
  `overlays/dev/images.yaml` sets the client's registry and the digest.
  Images pinned by digest outside the build are kept as they are and
  listed in INSTALL.md; an image without a digest is refused
  (`bundle-image-unpinned`).
- **Network.** The bundle adds `<name>-default-deny`: ingress only from the
  namespace, egress only to the namespace and the cluster DNS. Egress the
  app declares with `app.network_policy(..., egress=...)` adds to it and is
  listed in INSTALL.md.

## The safety gate

A bundle is applied by someone who did not write it, so `piceli bundle`
refuses an object that breaks one of these rules (exit `2`, reason
`bundle-unsafe-<rule>`, and `violations` lists every object and rule):

| Rule | What it requires |
| --- | --- |
| `non-root` | every container (init containers and sidecars too) runs as a non-root user: `runAsNonRoot: true` or a non-zero `runAsUser` |
| `read-only-root` | every container sets `readOnlyRootFilesystem: true` |
| `no-privilege` | no privileged container, `allowPrivilegeEscalation: false`, no added capability but `NET_BIND_SERVICE`, no host network, PID or IPC, no `hostPath` |
| `resources` | every container requests and limits `cpu` and `memory` |
| `network` | no NetworkPolicy of the app admits ingress from outside the namespace |
| `rbac` | no wildcard, no `escalate`/`bind`/`impersonate`; a ClusterRole only reads and never Secrets; bindings only to roles of the bundle and only ServiceAccounts |
| `node-port` | no `NodePort` or `LoadBalancer` Service and no `hostPort` |

`Security.restricted(user=…, read_only_root_filesystem=True)` in the App's
`pod_defaults` covers the first three for every pod. When an object really
needs an exception, declare it with a reason the client will read:

```python
app.safety_exception(worker, "read-only-root", reason="unpacks plugins at start")
```

It renders as the annotation `safety.piceli.io/allow-read-only-root:
<reason>` on every object the declaration renders (for a ServiceAccount, its
Roles and bindings too), so it is visible in the client's cluster, and the
bundle lists it in `bundle.json`, in INSTALL.md and on stderr.

## The cluster's identity

An app that must know which cluster it runs in reads it from the cluster: the
UID of the `kube-system` namespace never changes while the cluster lives.

```python
identity = app.cluster_identity(image=build["web"])
web = app.deployment(
    "web",
    image=build["web"],
    init=[identity.init],
    volumes=identity.volumes,
    service_account=identity.service_account,
    env={"CLUSTER_UID_FILE": identity.file},
)
app.network_policy(web, egress=[identity.egress], name="web-api-server")
```

`app.cluster_identity` declares a ServiceAccount whose only permission is
`get` on the namespace `kube-system` (a ClusterRole restricted by
`resourceNames`) and returns a `ClusterIdentity`: an init container that
writes the UID to `/run/cluster-identity/uid` on a memory volume, the volume
to mount in the main container, the service account and the egress rule to
the API server (TCP 443 and 6443). The init container runs `sh` with `curl`
(verifying the API server with the service account's CA) or BusyBox `wget`
(which cannot verify it: prefer an image with curl); `command=` runs your
own program instead, which writes `$IDENTITY_FILE`. A workload with its own
service account adds `ClusterIdentity.rule()` to its `cluster_rules`.

## What the client runs

INSTALL.md lists the commands in fenced `sh` blocks, to run as written, in
order, in one shell, from the bundle's directory: check `SHA256SUMS`, choose
`NAMESPACE` and `REGISTRY`, copy the images (`skopeo copy`), point the
overlay at them, then:

```sh
kubectl create namespace "$NAMESPACE"
./prepare.sh "$NAMESPACE"
kubectl apply -k overlays/dev
kubectl -n "$NAMESPACE" port-forward svc/web 18080:8080
```

UNINSTALL.md deletes the namespace and the cluster-scoped objects by label
(`kubectl delete clusterrole,clusterrolebinding -l
app.kubernetes.io/part-of=<name>`) and checks that nothing is left. Both need
`kubectl` 1.27 or later (kustomize 5).

## Support bundles

```sh
piceli support-bundle --kubeconfig client.kubeconfig --context their-dev \
  --namespace shop --out shop-support.tar.gz
```

Run by whoever has access (the client), with an explicit kubeconfig and
context (`--allow-exec` for an exec credential plugin). It reads the
namespace with `GET` requests only (any other method is refused before it is
sent) and writes a private (`0600`) `.tar.gz`, never overwriting a file:

| File | Holds |
| --- | --- |
| `versions.json` | API server, each node's kubelet, runtime, OS and architecture (when nodes are readable), Piceli |
| `pods.json` | phase, conditions, container states and restarts, images and image IDs, resources, probes, the **names** of environment variables |
| `workloads.json` | Deployments, StatefulSets, DaemonSets and Jobs: replicas, conditions, the bundle they came from |
| `health.json` | each workload's declared readiness probe and whether its replicas are ready |
| `events.json`, `services.json` | the namespace's events; Service ports |
| `configmaps.json` | ConfigMap names and **keys** |
| `logs/<pod>.<container>[.previous].log` | the last `--log-lines` lines (default 200) |
| `manifest.json` | every file's sha256, the request methods, what could not be read, what was redacted |

Secrets are never requested; environment and ConfigMap values are never
collected; log lines, event and condition messages pass through the log
redaction of {doc}`operations_lens` (passwords, tokens, bearer credentials,
URL credentials, JWTs and private keys become `[REDACTED]`).

## Errors

| Code | Meaning |
| --- | --- |
| `bundle-unsafe-<rule>` | an object breaks a safety rule; `violations` lists them |
| `bundle-secret-value`, `bundle-secret-unsupported` | a secret value outside a Secret; a Secret key bound to no generator |
| `bundle-image-unresolved`, `bundle-image-unpinned` | a build image missing from `--receipt`; an image without digest |
| `bundle-image-platform-missing`, `bundle-image-archive-missing`, `bundle-image-mismatch`, `bundle-receipt-invalid` | the receipt lacks a platform or an archive, or does not match its files |
| `bundle-invalid`, `bundle-out-refused`, `bundle-empty` | invalid name, version or labels; `--out` not empty |
| `support-bundle-*` | unreachable API, missing namespace, existing `--out` |

All codes: {doc}`reference/errors`.
