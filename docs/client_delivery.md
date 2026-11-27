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
  `piceli.io/bundle: <name>@<version>` (the domain is yours to choose, see below). `<name>` is the render's part-of
  label (the App name by default) or `--name`.
- **No Secret is shipped.** Each Secret the app declares is bound to outputs
  of the pipeline's generators (`Secrets(Random, Template, TlsCa, …)`, see
  {doc}`secrets`); `prepare.sh NAMESPACE` generates them in the client's
  cluster with `openssl` and creates them with `kubectl`: random tokens
  (`Random(n)`: the same URL-safe form as a release), templates, static
  values, a private CA with its leaf certificates (the CA key never leaves
  the script's private temporary directory). Values from sources Piceli
  cannot generate (`Sops`, `Vault`, `AwsSecret`, an import) are read from
  files the client names (`PICELI_INPUT_<NAME>=path`, or your own prefix). The script is POSIX
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

## The names the operator sees

An operator who installs the bundle reads its files, so they can carry the
owner's names instead of the tool's. Three options of `piceli bundle` (both
the kustomize bundle and `--helm`) choose them:

| Option | Default | What it names |
| --- | --- | --- |
| `--input-prefix PREFIX` | `PICELI_INPUT_` | the input variables of `prepare.sh` (`<PREFIX><NAME>=path`), in its error texts and in INSTALL.md |
| `--tool-name NAME` | `piceli bundle` | the tool in generated comments: `prepare.sh`, the file headers, `values.yaml`, `Chart.yaml`, the chart README (`--helm` adds no suffix to a custom name) |
| `--annotation-domain DOMAIN` | `piceli.io` | the domain of every annotation and label key the bundle writes: `DOMAIN/bundle`, `DOMAIN/namespace`, `DOMAIN/helm-release`, `DOMAIN/configmaps-digest`, `DOMAIN/secrets-digest`, `safety.DOMAIN/allow-<rule>` |

```sh
piceli bundle app.py:pipeline --env client --out shop-bundle \
  --input-prefix SHOP_INPUT_ --tool-name "shop bundle" --annotation-domain shop.example
```

- A bad name is refused with `bundle-invalid`: the prefix is capital letters,
  digits and `_`; the tool name is plain text of 60 characters or fewer; the
  domain is a lower-case DNS name that is not a Kubernetes one.
- The keys are renamed in every `labels`, `annotations` and selector of the
  objects, and the `fieldPath` that reads one, so selectors still match their
  pods. Node labels and other keys that are not Piceli's own stay.
- The domain also names what is not an annotation: the host of build images
  in the base becomes `images.<first label>.invalid`, the `schema` of
  `bundle.json` becomes `<first label>.client-bundle.v1` and the chart's
  Secrets template `templates/<first label>-secrets.yaml` (for `shop.example`:
  `shop`). With the defaults these are the names of every earlier bundle.
- The defaults give files identical to the bundles of earlier versions.
- `piceli support-bundle` reads the `DOMAIN/bundle` marker under any domain.
- **Upgrade of an installed chart.** `helm upgrade` with a chart rendered
  under a new domain works over an install made with the old keys. The keys
  on the old objects change; on the pod templates the digest annotations
  (`configmaps-digest`, `secrets-digest`) change their key, so the pods roll
  once. The old keys are not migrated or removed by the chart.

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

## As a Helm chart

`piceli bundle ... --helm` writes the same bundle (the same render, after the
same safety gate) as a Helm 3 chart instead: `Chart.yaml`, `values.yaml`,
`values.schema.json`, `README.md` and `templates/`. The client installs and
removes it with one command each, with no archive and no `prepare.sh`:

```sh
piceli bundle app.py:chart --helm --out chart --version 1.4.0
helm install agent oci://registry.example/acme/charts/agent --version 1.4.0 \
  -n agent --create-namespace --set team=T1 --set-file apiToken=token.txt
helm uninstall agent -n agent
```

What becomes a value:

| Value | From | Notes |
| --- | --- | --- |
| each ConfigMap key, in camelCase (`cluster-id` → `clusterId`) | the app's ConfigMaps | or only the keys `HelmChart(settings=...)` names; one in `required` fails the install with a message naming it |
| each secret input, in camelCase (`api_token` → `apiToken`) | a generator Piceli cannot run (an import, SOPS, Vault) | `--set-file NAME=FILE`; required on the first install, kept across upgrades when not passed again; trailing whitespace (a newline, `\r\n`) is trimmed |
| each toggle (`relayOnly`) | `HelmChart(toggles=...)` | `true` applies the toggle's patches |
| `image.registry`, `image.pullSecrets` | every image | a mirror's host (and path) replaces each image's registry; digests stay |
| `storageClass` | every claim | empty: the cluster's default (or `--storage-class`) |
| `storage.size` (0.31.0) | every claim | empty keeps each claim's declared size; set it at install (Kubernetes does not resize a StatefulSet's claims on upgrade) |
| `deleteDataOnUninstall` | StatefulSets with claims | `true`: uninstall also deletes their claims (Kubernetes 1.27 or later) |
| `existingSecret` (0.31.0) | every Secret of the app | the name of a Secret you created holding every secret key (the README lists them); the chart then makes no Secret and generates nothing |
| each existing-Secret value (`api.existingSecret`, 0.80.0) | `HelmChart(existing_secrets=...)` | the name of a Secret you created for the app Secrets it replaces (the README lists its keys); the chart then does not make those Secrets, and a generated value or an input only they need is skipped |
| each optional value (`intake.expose`, 0.31.0) | `HelmChart(optional=...)` | `true` installs the named objects; with `default_on_reuse=True` (0.81.0) a value the stored values lack reads as its default; otherwise it reads as off |
| each object value (`intake.allowNamespaces`, 0.31.0) | `HelmChart(values=...)` | written at a JSON pointer of named objects (a list, a string, a number); where the object holds a string (a ConfigMap key, an environment variable), a number or boolean is written as one, `"true"` (0.65.0) |
| each retired value (0.65.0) | `HelmChart(retired=...)` | accepted and ignored, so `helm upgrade --reuse-values` from an older chart works; the install notes warn when it is set |

`values.schema.json` refuses unknown values, so a mistyped name fails the
install. Generated values (`Random`, `Static`, `Template`) are made at install
in the same form as `prepare.sh` makes them (`Random(32)`: 32 random bytes,
URL-safe base64), and read back with Helm's `lookup` on every upgrade, so
they never change; they are never printed.

A private CA (`TlsCa`) or a self-signed certificate is made at install too
(0.63.0, Helm's `genCA` and `genSignedCert`) and kept on upgrade while the
Secrets hold every key of it. A DNS name naming the bundle's namespace
(`intake.agent.svc`) names the release namespace instead. Put `ca.crt` in a
Secret the other workloads mount to let them trust the leaves. The CA key is
not stored, so a new leaf in a later chart makes a new CA. Keys are 2048-bit
RSA; other sizes and IP addresses are refused (`bundle-secret-unsupported`):
use the kustomize bundle for those. Build
images from `--receipt` are refused: push them and declare them by digest.

**Argo CD and `helm template`.** They render without a cluster, so `lookup`
finds nothing and a generated value would change on every render (and roll
the pods). There, the client creates the Secret (random values for the
generated keys, for example `openssl rand -base64 32`) and sets
`existingSecret`: nothing is generated, every render is the same, and the
client holds every value, so it can copy one (an intake token) to another
namespace. `existingSecret` is offered when the app's Secrets have distinct
key names; a Secret volume then mounts that one Secret.

**One existing Secret per app Secret** (0.80.0). `existingSecret` replaces
every Secret at once. To bring only some, map a value name to the app
Secrets it replaces:

```python
chart = HelmChart(
    pipeline,
    existing_secrets={
        "db.existingSecret": ["shop-db"],
        "api.existingSecret": ["shop-api"],
    },
)
```

With `--set db.existingSecret=client-db`, the chart does not render
`shop-db`, and every reference to it (an environment variable, a volume, a
projected source, `envFrom`) reads `client-db`. A generator whose outputs
land only in replaced Secrets does not run, and an import that feeds only
them is not required, so an install that brings the database Secret needs
no `--set-file` for its password. The `secrets-digest` annotation takes the
name, so a different Secret rolls the pods. The README and `values.yaml`
list each value with the keys its Secret must hold; the Secrets of one value
must not share a key. Each Secret is in one value, and a value name is a
dotted camelCase value like the optional ones. The declaration is checked
when the chart is built (`bundle-invalid`): an unknown Secret, a Secret in
two values, a name that clashes with another value, a bad name.
`existingSecret` itself is still the value that replaces every Secret, and
when both it and a value are set, `existingSecret` wins for all. To make
`existingSecret` replace only some Secrets, list it in the mapping
(`{"existingSecret": ["shop-db"]}`): it then is that value, and no value
replaces every Secret. Without the declaration the chart is as before.

**Optional values and `--reuse-values`** (0.81.0). `helm upgrade
--reuse-values` keeps the values of the release, which lack a value a newer
chart added. By default a missing `HelmOptional` or `HelmToggle` value is
off, so a plain `--reuse-values` adds nothing the client never turned on
(`--reset-then-reuse-values` turns the defaults on). Set
`default_on_reuse=True` on a value whose default is `true` to make a missing
key read as `true` instead (a default-deny NetworkPolicy, say); an explicit
`false` still turns it off. 0.80.0 did this for every value; 0.81.0 leaves
the choice to the chart owner.

Cluster-scoped objects (ClusterRoles and their bindings) belong to the
release: their names follow the release namespace, and `helm uninstall`
deletes them. A StatefulSet's claims (its data) stay after uninstall unless
`deleteDataOnUninstall=true`; delete the namespace to remove them.

Name the values with a `HelmChart` beside the pipeline and point `piceli
bundle` at it:

```python
from piceli import HelmChart, HelmToggle

chart = HelmChart(
    pipeline,
    required=("team", "clusterId"),
    toggles={
        "relayOnly": HelmToggle(
            "UDP only to the relay's ports",
            patches=[
                (
                    policy,
                    [
                        {
                            "op": "replace",
                            "path": "/spec/egress/1/ports",
                            "value": [{"port": 3478, "protocol": "UDP"}],
                        }
                    ],
                )
            ],
        )
    },
    descriptions={"team": "the team id"},
)
```

A toggle's patch is a JSON Patch (RFC 6902: `add`, `remove`, `replace`,
`test`) or a mapping merged like `app.override`; the patched render passes
the safety gate too, and two toggles may not change the same object.

Objects the client opts into, values written into objects, and a disk
limit that follows the claim size (0.31.0):

```python
from piceli import (
    HelmChart,
    HelmOptional,
    HelmValue,
    NetworkPeer,
    NetworkPort,
    NetworkRule,
)

intake = app.service(agent, port=4318, name="agent-intake")
intake_policy = app.network_policy(
    agent,
    name="agent-intake",
    policy_types=("Ingress",),
    ingress=[
        NetworkRule(
            peers=(NetworkPeer.namespaces(["workers"]),),
            ports=(NetworkPort(port=4318),),
        )
    ],
)
app.safety_exception(intake_policy, "network", reason="the client names the senders")

chart = HelmChart(
    pipeline,
    optional={
        "intake.expose": HelmOptional(
            "Expose the intake to other namespaces", objects=[intake, intake_policy]
        )
    },
    values={
        "intake.allowNamespaces": HelmValue(
            "Namespaces whose pods may send to the intake",
            targets=[
                (
                    intake_policy,
                    "/spec/ingress/0/from/0/namespaceSelector/matchExpressions/0/values",
                )
            ],
        )
    },
    storage_env={"DISK_LIMIT": 90},
)
```

`helm install ... --set intake.expose=true --set 'intake.allowNamespaces={jobs,web}'`
installs the Service and the policy, which admits those namespaces
(`NetworkPeer.namespaces([...])` renders `kubernetes.io/metadata.name In
[...]`). A `HelmValue`'s default is the rendered value at its first target.
With `--set storage.size=20Gi`, each container that declares `DISK_LIMIT`
gets 90 % of 20Gi in bytes (`19327352832`); without it, its declared value.
Optional objects pass the safety gate like every other object.

The same value can also patch other objects (0.34.0), so one switch both
adds the objects and adjusts the ones that use them. Here the agent
listens only on loopback until the intake is exposed:

```python
LISTEN_EVERYWHERE = {  # a merge patch: containers and env merge by name
    "spec": {
        "template": {
            "spec": {
                "containers": [
                    {
                        "name": "agent",
                        "env": [{"name": "INTAKE_LISTEN", "value": "0.0.0.0:4318"}],
                    }
                ]
            }
        }
    }
}

chart = HelmChart(
    pipeline,
    optional={
        "intake.expose": HelmOptional(
            "Expose the intake to other namespaces",
            objects=[intake, intake_policy],
            patches=[(agent, LISTEN_EVERYWHERE)],
        )
    },
)
```

`patches` takes the same patches as a `HelmToggle` (a JSON Patch or a
merge patch per object) and applies them only when the value is true; the
patched render passes the safety gate. A patched object may not be one of
the optional objects, nor be patched by another value or toggle.

A secret input the client may leave out (0.34.0), for example a private
certificate authority, is named in `optional_inputs` by its value name:

```python
chart = HelmChart(pipeline, optional_inputs=("privateCa",))
```

Without `--set-file privateCa=FILE` the install succeeds and the Secret
keys that input fills are omitted, so mount the Secret as a volume (or
read the key with an optional reference) and treat a missing file as "no
private CA". Once passed, the value is kept across upgrades like any input.

To tell a container which release and namespace it runs in (for example
to print its own `helm upgrade` command), use `HelmRelease` as an env value
(0.46.0). The chart's `{{ ... }}` escaping stays in place:

```python
from piceli import HelmRelease

env = {"APP_RELEASE": HelmRelease.name, "APP_NAMESPACE": HelmRelease.namespace}
```

`HelmRelease.namespace` reads the pod's namespace, in every bundle.
`HelmRelease.name` reads the pod annotation `piceli.io/helm-release`, which
the chart sets to `{{ .Release.Name }}`; in a kustomize bundle or a
`piceli deploy` the annotation is absent and the variable is empty.

### Storage probe before install (0.94.0)

A claim in a storage class the cluster's nodes cannot attach (a disk type
the machine type does not support, a class bound to another zone, a full
quota) leaves the app Pending with no message. Helm's `lookup` sees nothing
under `helm template` and Argo CD, so the chart checks it in the cluster,
before it installs anything else:

```python
from piceli import HelmChart, StorageProbe

chart = HelmChart(
    pipeline,
    storage_probe=StorageProbe(image=WEB_IMAGE, timeout_seconds=180, size="1Gi"),
)
```

- **What runs.** Helm `pre-install` hooks (not `pre-upgrade`: on an upgrade
  the claims already exist): a ServiceAccount, a Role (create, get and delete
  claims and pods in the release namespace, list events), a ClusterRole that
  only reads storage classes, their bindings, a NetworkPolicy that lets the
  probe reach the API server, and a Job. The Job runs `sh` and `curl` from
  `image` (pinned by digest, like every chart image; usually the app's own),
  as `app.cluster_identity` does. It creates a claim `<release>-storage-probe`
  of `size` in `storageClass` (the default class when empty) and a pod that
  mounts it, on the nodes where the app's claim workload runs (its node
  selector, tolerations and node affinity). Once the pod runs, it deletes
  both and prints how long it took. Every object passes the
  [safety gate](#the-safety-gate).
- **The message.** When the pod does not run within
  `storageProbe.timeoutSeconds`, the Job reads the newest warning event of
  the claim and the pod and fails the install with one line:

  ```text
  storage class "standard" cannot attach on these nodes (FailedAttachVolume: ...): set storageClass to one of: standard-rwo, premium-rwo
  ```

  For the default class it says `the default storage class "standard"`.
  With no other class it says to ask the cluster's administrator for one.
  A claim the cluster refuses (a full quota) fails at once with the API
  server's message.
- **Where you read it.** Helm 4 prints the Job's log when the hook fails
  (`helm.sh/hook-output-log-policy: hook-failed`). Argo CD shows the
  container's termination message (`FallbackToLogsOnError`). With Helm 3:
  `kubectl -n NAMESPACE logs job/RELEASE-storage-probe`. A failed probe's
  objects stay until the next install replaces them.
- **Turn it off** with `--set storageProbe.enabled=false`. Keep
  `storageProbe.timeoutSeconds` under `helm install --timeout` (5 minutes by
  default). Argo CD maps `pre-install` to a PreSync hook, so it runs the
  probe before every sync; turn it off there once the first sync worked.
- **Cost.** One 1 GiB disk (`size`) for under a minute, deleted after. With a
  class whose reclaim policy is `Retain`, its volume stays; delete it by hand.
- **Only for an app with a claim** (otherwise `bundle-invalid`), and only in
  the Helm chart: the kustomize bundle has no hook to run it before the app.

Publish the chart to an OCI registry as `helm push` does, with an approval
of its digest like every Piceli publish:

```sh
piceli bundle app.py:chart --helm --version 1.4.0 --push oci://registry.example/acme/charts
# exit 3, prints the digest; nothing is sent
piceli bundle app.py:chart --helm --version 1.4.0 --push oci://registry.example/acme/charts \
  --approve sha256:... --credentials registry.json
```

The chart goes to `<path>/<name>:<version>`; the same app, name and version
always give the same archive and digest.

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
| `bundle-invalid`, `bundle-out-refused`, `bundle-empty` | invalid name, version, labels, value names or toggles; `--out` not empty |
| `chart-target-invalid`, `chart-artifact-changed`, `chart-push-failed` | with `--helm --push`: a bad `oci://` path, a digest that is not this chart's, a failed push |
| `support-bundle-*` | unreachable API, missing namespace, existing `--out` |

All codes: {doc}`reference/errors`.
