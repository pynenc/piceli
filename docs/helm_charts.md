# Publish for other people's clusters: a Helm chart and manifests

When someone else installs your app on their own cluster (a client on GKE,
EKS or AKS, an operator who uses Helm), they cannot run your `Pipeline`: they
have their own registry, storage classes, host names and secrets. `piceli
chart` renders the same typed `App` as:

- a **Helm chart**: `Chart.yaml`, `values.yaml` with every value a client
  sets, `values.schema.json` that documents and validates them, and
  templates that, with the default values, render exactly the objects
  `piceli render` prints;
- a **chart archive** (`<name>-<version>.tgz`, byte-identical for the same
  app and version) and an **OCI chart** in the layout `helm push` produces;
- **plain manifests with values applied** (no Helm, no Kustomize), for a
  client who applies YAML.

```{admonition} Maturity: preview
:class: note

`piceli chart` is **preview**: the values layout and the JSON fields may
still change in a minor release. Signing and SBOM attachment are not built
in yet.
```

## Render, package and publish

```sh
piceli chart render app.py:app --out chart/ --version 1.4.0      # a chart directory
piceli chart package app.py:app --out dist/ --version 1.4.0      # dist/shop-1.4.0.tgz
piceli chart publish app.py:app --to oci://registry.example/charts --version 1.4.0
# {"state": "approval-required", "digest": "sha256:…"}   (exit 3; nothing pushed)
piceli chart publish app.py:app --to oci://registry.example/charts --version 1.4.0 \
  --approve sha256:… --credentials registry.json
```

Every subcommand renders like `piceli render` (the same `TARGET`, `--spec`,
`--namespace` and `--env`; a `Pipeline` renders offline with its target's
nodes and build images as placeholders) and never contacts a cluster.

- The chart name defaults to the App name; `--name` sets another one.
  `--version` is the chart version (SemVer 2, default `0.1.0`) and
  `--app-version` the `appVersion` (default: the chart version).
- `chart publish` pushes `path/<name>:<version>` (a `+` in the version
  becomes `_` in the tag, as Helm does). Like `piceli publish`, it prints
  the digest and pushes only with `--approve DIGEST`, reads the manifest
  back by digest and by tag, and takes credentials only from
  `--credentials FILE`. Plain HTTP is allowed only for a loopback registry.
- The same app, name and version always give the same files, the same
  archive bytes and the same digest. `chart package` refuses to overwrite an
  archive of the same version with other content: bump the version.

The client installs with nothing but the chart and a values file:

```sh
helm install shop oci://registry.example/charts/shop --version 1.4.0 \
  --namespace shop --create-namespace --values client-values.yaml
```

## What the client sets

`values.yaml` lists every value with its default (the value your app
renders) and `values.schema.json` gives each one a type, a format and a
description, so `helm install` refuses a typo or a wrong type before
anything reaches the cluster.

| Values | Where it lands |
| --- | --- |
| `images.<key>.repository`, `.tag`, `.digest` | every container using that image (`repository[:tag][@digest]`); the key is the repository's last path segment |
| `imagePullSecrets` | a list of Secret names added to every pod |
| `workloads.<name>.replicas` | Deployment and StatefulSet replicas |
| `workloads.<name>.containers.<container>.resources` | requests and limits (`null` removes them) |
| `workloads.<name>.nodeSelector` | the pod node selector, including `node=` pins |
| `workloads.<name>.storage.<claim>.size`, `.storageClassName` | StatefulSet claim templates (`""` for the cluster default) |
| `claims.<name>.size`, `.storageClassName` | PersistentVolumeClaims the app renders |
| `existingClaims.<name>.claimName` | claims the pods mount but the app does not create (`ExistingClaim`) |
| `ingresses.<name>.hosts`, `.className` | Ingress rules and TLS hosts, ingress class |
| `httpRoutes.<name>.hostnames` | Gateway API HTTPRoute host names |
| `config.<configmap>.<key>` | every ConfigMap key |
| `secrets.<name>.name` | the Secret each reference points to (see below) |

Values merge over the defaults the way Helm merges them: maps merge,
anything else replaces, and `null` removes a key. To drop a
`kubernetes.io/hostname` pin that names one of your nodes:

```yaml
workloads:
  api:
    nodeSelector:
      kubernetes.io/hostname: null
```

An image Piceli has not resolved (a `Pipeline` build that has not run) has
no default: `images.<key>` is required, and both `helm install` and `piceli
chart manifests` refuse to render until the client sets it. The command
lists the required values.

(chart-secrets)=

## Secrets and existing claims

**The chart never renders a Secret.** Every Secret the app declares is left
out, and every reference to it (environment variables, `envFrom`, volumes,
Ingress TLS) reads `secrets.<name>.name`. The client creates the Secret
first, with the keys the chart's `README.md` lists (`kubectl create secret`,
an ExternalSecret, SOPS), and may give it another name. An object other
than a Secret that holds a value Piceli redacts or injects at apply time is
refused (`chart-secret-value`).

Creating Secrets from values the client supplies is deliberately not
offered: secret values would then pass through values files, shell history
and Helm's release records. Claims work the same way: an `ExistingClaim` the
pods mount is referenced by `existingClaims.<name>.claimName` and never
created.

## Plain manifests with values

```sh
piceli chart manifests app.py:app --values client-values.yaml --install-namespace shop
piceli chart manifests app.py:app -f client-values.yaml --out manifests/
```

Piceli merges the values files (later files win) over the defaults,
validates them against the same schema (`chart-values-invalid` names the
path and the rule, never the value) and prints the manifests, or writes one
file per object like `piceli render --out`. The result equals `helm
template` with the same values, apart from the two Helm labels.

## What stays the same, and what does not

With the default values, `helm template` renders the objects `piceli render`
prints, except for:

- the Secrets, which are left out;
- two labels on each object's own metadata, `helm.sh/chart` and
  `app.kubernetes.io/managed-by` (never on selectors or pod templates);
- the namespace, which is Helm's release namespace.

Once a client installs the chart, Helm applies it, not Piceli: there is no
plan against the live cluster, no approval by plan hash, no journal, no
dependency order between components (a Job the app waits on runs alongside
the workloads; make workloads tolerate a dependency that is not ready yet),
no generated or rotated secrets, no restore points and no release checks.
See {doc}`gitops` for the same boundary with an exported OCI artifact.

The chart also keeps literally what it cannot know about another cluster:

- names of cluster-scoped objects (a ClusterRole named after your
  namespace keeps that name; the bindings' subjects follow the release
  namespace);
- the namespace inside strings (an environment variable holding
  `api.shop.svc`);
- node-local image delivery (`NodeLoopbackRegistry`): the client pulls from
  its own registry, set through `images` and `imagePullSecrets`;
- objects of any other kind and custom resources, which are templated
  as they render (only their namespace is the release namespace).
