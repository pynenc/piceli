# From kubectl scripts to Piceli

This tutorial moves an application that was deployed with `kubectl` commands
into one typed Python module that `piceli release` manages, without
restarting anything: you import the live objects, adopt them, and from then on
deploy with a plan you approve.

```{admonition} Maturity: preview
:class: note

`piceli import live`, `piceli import yaml` and `App.override` are **preview**:
the generated code, the command options and the JSON fields may change before
1.0. The error codes listed under [If it fails](#if-it-fails) are stable.
```

The story uses a small shop namespace made by hand:

```sh
kubectl create namespace shop
kubectl -n shop create configmap web-settings --from-literal=greeting=hello
kubectl -n shop create secret generic web-token --from-literal=token=…
kubectl -n shop create -f web.yaml          # a PVC and the Deployment "web"
kubectl -n shop expose deployment web --port 8080 --target-port 80
```

## Prerequisites

- Piceli installed (`pip install piceli`).
- A kubeconfig **file** and a context with static credentials (a client
  certificate or a token) that can `list` ConfigMaps, Secrets, Services,
  PersistentVolumeClaims, Deployments, NetworkPolicies and Pods in the
  namespace. Piceli never uses `~/.kube/config`, `KUBECONFIG` or the current
  context: you always name both. For `kind`:
  `kind get kubeconfig --name my-cluster > my-cluster.kubeconfig`.
- To follow along without a cluster, import manifest files instead:
  `piceli import yaml DIR` (step 1b).

## Steps

### 1. Import the namespace

```console
$ piceli import live --kubeconfig deploy/my-cluster.kubeconfig --context kind-my-cluster \
    --namespace shop --out deploy/app.py
imported 5 objects from live namespace shop: 4 typed, 0 raw, 1 existing claims; 2 untyped fields kept as overrides; 1 secret inputs to declare (listed in the module docstring)
skipped ConfigMap/kube-root-ca.crt: created by Kubernetes in every namespace
wrote deploy/app.py
{"app": "shop", "images": [{"container": "nginx", "deployment": "web", "image": "nginx:1.27-alpine", "running_digest_ref": "docker.io/library/nginx@sha256:6564…2a10"}], "module_sha256": "8e4c…a973", "namespace": "shop", "objects": [...], "out": "deploy/app.py", "secret_inputs": [{"input": "web-token-token", "key": "token", "secret": "web-token"}], "skipped": [...], "source": "live", "state": "imported"}
```

The command only reads the cluster. It writes `deploy/app.py` (refused if the
file exists, unless `--force`) and prints a JSON summary on stdout (human
lines go to stderr). Without `--out` it prints the module itself.
`--select Deployment/web` or `--select app=web` (repeatable) imports only
matching objects; `--name` sets the app name (default: the namespace).

**1b. From files.** `piceli import yaml manifests/ --out deploy/app.py` reads
every `*.yaml`, `*.yml` and `*.json` file below `manifests/` and generates the
same kind of module, without a cluster. The namespace is the one the files
name (or `--namespace`).

### 2. Read the generated module

The generated module is a composition function of the release context, like
any other typed app ({doc}`typed_apps`). This is the complete module the
import above wrote:

```python
"""Typed app for namespace shop, imported by `piceli import live`.

Generated from namespace shop (context kind-my-cluster). Review it, then release
it with `piceli release plan --adopt-all-desired` (see "From kubectl scripts to
Piceli" in the Piceli documentation). Fields the typed API does not cover yet
are kept with `app.override(...)` under a "not typed" comment.

Secret values were not copied. Declare these generators in release.toml; each
imports the live value on the first release:

    [secrets.web-token-token]
    type = "import"
    secret = { name = "web-token", key = "token" }

Existing claims (mounted, never managed): web-data.

Running images. To pin one by digest, add it to [images] and use
ctx.image(name); that changes the pod template, so it rolls out:

    web-nginx = "docker.io/library/nginx@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10"

Skipped: ConfigMap kube-root-ca.crt (created by Kubernetes in every namespace).
"""

from __future__ import annotations

from piceli import App, ExistingClaim
from piceli.k8s.ops.plan import DeploymentComposition
from piceli.k8s.release_spec import ReleaseContext


def build(ctx: ReleaseContext) -> DeploymentComposition:
    app = App("shop", labels={})

    web_settings = app.config("web-settings", {"greeting": "hello"})

    web_token = app.secret("web-token", {"token": ctx.secret("web-token-token")})

    web = app.deployment(
        "web",
        container="nginx",
        image="nginx:1.27-alpine",
        env={"GREETING": web_settings.key("greeting"), "TOKEN": web_token.key("token")},
        ports=[80],
        ready=app.probe.http("/", 80, period_seconds=2),
        volumes={"/data": ExistingClaim("web-data", name="data")},
        selector={"app": "web"},
    )
    # not typed: spec.template.spec.containers[nginx].securityContext
    app.override(
        web,
        {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": "nginx",
                                "securityContext": {"allowPrivilegeEscalation": False},
                            }
                        ]
                    }
                }
            }
        },
    )

    web_service = app.service(web, 8080, target_port=80)
    # not typed: metadata.labels
    app.override(web_service, {"metadata": {"labels": {"app": "web"}}})

    return app.composition(ctx)
```

What the importer did:

| Live object | In the module |
| --- | --- |
| ConfigMap, Deployment, Service, NetworkPolicy | Declared with the typed API (`app.config`, `app.deployment`, `app.service`, `app.network_policy`) |
| Secret | `app.secret(...)` with one `ctx.secret(...)` input per key. **Values are never copied**: the docstring lists an `import` generator for each key |
| PersistentVolumeClaim | `ExistingClaim(...)` where a Deployment mounts it: the release never creates, changes or deletes it |
| A field the typed API does not cover (a security context, tolerations, an extra label, an env var from `resourceFieldRef`, a list order the model cannot produce) | `app.override(obj, {...})`, with one `# not typed: <field>` comment per field |
| Another kind (StatefulSet, Ingress, …) or an object the typed API cannot express | A raw manifest in `app.add(DeploymentComponent(...))`, with a comment giving the reason |
| Server-populated fields (UIDs, status, cluster IPs, `managedFields`, revision annotations) and API defaults (`dnsPolicy: ClusterFirst`, `protocol: TCP` …) | Left out |
| The `kube-root-ca.crt` ConfigMap, service-account token Secrets, objects owned by another object | Skipped (listed in the docstring and the summary) |

The module is deterministic (the same objects give the same file) and passes
`ruff check` and `ruff format --check`. Before writing it, the importer
executes it and checks that it renders back **every** imported field; if not,
it refuses with `import-roundtrip-mismatch` and writes nothing.

Review it like any code change: rename variables, move values into
`ctx.values`, replace an override with a typed argument when one exists.

### 3. Write `release.toml`

Next to the module, in `deploy/release.toml`:

```toml
[target]
kubeconfig = "my-cluster.kubeconfig"   # relative to this file
context = "kind-my-cluster"
namespace = "shop"

[release]
name = "shop"
owner = "shop"
field_manager = "shop"
composition = "app.py:build"
state_dir = ".piceli-release"

[images]
# the running digest from the docstring; the module keeps the tag for now
web = "docker.io/library/nginx@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10"

# from the docstring: the Secret keeps its value
[secrets.web-token-token]
type = "import"
secret = { name = "web-token", key = "token" }
```

The module keeps the image string the Deployment runs today, so the first
release does not restart it. Switching to `image=ctx.image("web")` later pins
the digest and rolls the Deployment out once.

### 4. Plan with adoption

The objects exist but this release does not manage them yet, so a plain
`plan` refuses (exit `2`) and lists them:

```console
$ piceli release plan --spec deploy/release.toml
refused: existing objects are not managed by this release's owner: …
  blocking ConfigMap/web-settings: exists and is not managed by this release's owner -> --adopt ConfigMap/web-settings or --replace ConfigMap/web-settings
  blocking Deployment/web: exists and is not managed by this release's owner -> --adopt Deployment/web or --replace Deployment/web
  blocking Secret/web-token: exists and is not managed by this release's owner; retained: replace is never allowed -> --adopt Secret/web-token
  blocking Service/web: exists and is not managed by this release's owner -> --adopt Service/web or --replace Service/web
```

The module declares exactly these objects, so authorize all of them at once:

```console
$ piceli release plan --spec deploy/release.toml --adopt-all-desired
release shop-9ee3975e08b5 (create, apply): 4 adopt
    adopt ConfigMap/web-settings  [takeover: transfers field managers: kubectl-create; fields they own that the release does not declare will be REMOVED; previous owner: none]
    adopt Secret/web-token  [metadata-only: owner annotation only; previous owner: none]
    adopt Deployment/web  [takeover: transfers field managers: kubectl-create; fields they own that the release does not declare will be REMOVED; previous owner: none]
    adopt Service/web  [takeover: transfers field managers: kubectl-expose; fields they own that the release does not declare will be REMOVED; previous owner: none]
  secret web-token-token: imported
plan hash: fd67fee8…f667 (valid until 2026-09-24T22:36:19+00:00)
```

A takeover removes fields that `kubectl` wrote and the module does not
declare. The import declared every one of them, so only fields the API server
defaults are dropped, and it puts them back with the same values. The claim is
not in the plan at all: it is an `ExistingClaim`. To delete and recreate an
object instead (for example a Deployment whose selector you want to change),
use `--replace Kind/name`; see {doc}`release_cli`.

### 5. Apply

```console
$ piceli release apply --spec deploy/release.toml --approve fd67fee8…f667
  adopted ConfigMap/web-settings (takeover; transferred field managers: kubectl-create)
  adopted Secret/web-token (metadata-only)
  adopted Deployment/web (takeover; transferred field managers: kubectl-create)
  adopted Service/web (takeover; transferred field managers: kubectl-expose)
apply shop-9ee3975e08b5: ready
```

Nothing restarts: the Deployment keeps its pod template, ReplicaSet and pods,
the Service keeps its cluster IP, and the Secret keeps its value (the
`import` generator recorded it as the release's first version). The
Deployment's `metadata.generation` does increase, because the API server
counts annotation changes and the adoption wrote Piceli's owner annotation.

### 6. Deploy from now on

Change the module, then plan and apply:

```console
$ piceli release plan --spec deploy/release.toml
release shop-9ee3975e08b5 (reapply, apply): 4 no-op
```

A plan right after the adoption changes nothing, and it says so: every object
is `no-op`.

- A **Secret** is compared privately: the resolved secret values are compared
  with the live Secret in-process, and only the resulting `no-op`/`apply`
  reaches the plan (see {doc}`plans_and_diffs`).
- **API-server defaults** (a Service's allocated cluster IP, a Deployment's
  defaulted `progressDeadlineSeconds`) never make an object look changed: the
  plan compares the server's dry-run answer to the write with the live object.

If an object still plans `apply`, its field diff shows exactly what would
change; `piceli render --spec deploy/release.toml` shows the fields the
release sets.

Delete the `kubectl` scripts. From now on `plan`, `apply` and `rollback` are
the only commands that change the namespace.

## If it fails

`piceli import` exits `2` and prints `{"state": "rejected", "reason":
"<code>"}` on stdout and the detail on stderr. `piceli explain <code>`
prints the same entry as {doc}`reference/errors`.

| Code | Cause | Fix |
| --- | --- | --- |
| `import-target-invalid` | The kubeconfig, context or namespace cannot be used (missing file, unknown context, exec credentials, non-https server, missing namespace). | Pass an explicit kubeconfig file and context with static credentials, and an existing namespace. |
| `import-discovery-failed` | Listing a kind failed (`rbac-denied`, `api-unavailable` …). | Grant `list` on the kinds above; retry when the API is reachable. |
| `import-select-invalid` | A `--select` is neither `Kind/name` nor `label=value`. | Fix it; kinds are case-sensitive (`Deployment/web`). |
| `import-nothing-selected` | No importable object, or none matches `--select`. | Check the namespace, directory and selectors. |
| `import-name-invalid` | `--name` (or the namespace) is not a DNS label. | Pass `--name` with lowercase letters, digits and `-`. |
| `import-manifests-invalid` | A file is not YAML/JSON, a document lacks `apiVersion`/`kind`/`metadata.name`, an object is declared twice, or the files name several namespaces. | Fix the file named on stderr, or pass `--namespace`. |
| `import-output-refused` | `--out` exists, or its directory does not. | Add `--force`, or choose another path. |
| `import-roundtrip-mismatch` | The generated module does not render back every imported field (a bug). Nothing was written. | Report it; import the other objects with `--select`. |

After the import, `release plan` refusals are covered in
[If `plan` refuses](release_cli.md#if-plan-refuses).

## `piceli import live` contract

```text
piceli import live --kubeconfig FILE --context NAME --namespace NS
    [--select Kind/name|label=value ...] [--name APP] [--out FILE [--force]] [--json]
    [--transport https|loopback-http]
```

| | |
| --- | --- |
| Reads | The kubeconfig file named by `--kubeconfig`, and the namespace: ConfigMaps, Secrets, Services, PersistentVolumeClaims, Deployments, NetworkPolicies, and Pods (only for their running image digests). Secret values are replaced before generation and never written. |
| Writes | Only `--out` (atomically; refused if it exists without `--force`). Never writes to the cluster. |
| stdout | With `--out`: one JSON summary. Without: the module source, or with `--json` one JSON object that also holds `module`. |
| Summary fields | Schema: `docs/schemas/piceli-import-summary-v1.schema.json`. `state` (`imported`), `app`, `namespace`, `source` (`live`/`yaml`), `out`, `module_sha256`, `objects` (`kind`, `name`, `as`: `typed`/`raw`/`existing-claim`, `untyped_fields`, `reason`), `secret_inputs` (`input`, `secret`, `key`), `skipped` (`kind`, `name`, `reason`), `images` (`deployment`, `container`, `image`, `running_digest_ref`). |
| Approval | None. |
| Retry | Always safe; the output is deterministic for the same objects. |
| Exit codes | `0` imported, `2` rejected. |
| `--transport loopback-http` | Only for a local test API server ({doc}`testing`). |

## `piceli import yaml` contract

```text
piceli import yaml DIR [--namespace NS] [--select ...] [--name APP] [--out FILE [--force]] [--json]
```

Reads `*.yaml`, `*.yml` and `*.json` below `DIR` (recursively, in path order;
`kind: List` documents are expanded; cluster-scoped objects are skipped).
Never contacts a cluster. Output, approval, retry and exit codes are the same
as `import live`; the summary has no running digests.

## The override escape hatch

`app.override(obj, patch)` is part of the typed API ({doc}`typed_apps`), not
only of imported modules. It merges `patch` into the rendered manifest:
mappings merge key by key and `None` removes a key; lists of objects with a
unique `name` (containers, env, volumes, ports) merge item by item on `name`
and new names are appended; any other value replaces the rendered one. It
cannot change an object's identity or a Secret's data.
