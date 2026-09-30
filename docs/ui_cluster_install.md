# In-cluster Piceli UI installation template

The installation renderer produces Kubernetes YAML for one Piceli UI in one
existing namespace. It makes no Kubernetes request and never reads an ambient
kubeconfig. Review the output, then apply it with an explicit kubeconfig and
context. The default template starts `piceli ui cluster-observe`: signed OIDC
login, scoped observation and logs, with no write verbs in its Role. An
optional manual-delivery profile mounts an explicit release definition and
source allowlist, starts `piceli ui cluster-serve`, and adds only the namespace
resource rules the operator supplies. It grants no universal deployment
rights. The whole template is **experimental**. The manual-delivery profile
and local-client access are also **unsupported and disabled by default**:
the renderer refuses them with `ui-experimental-disabled` unless the config
sets `experimental=True`, which adds `--experimental` to the rendered UI
command. A clean authenticated deployment and restart recovery have not
passed their release gate; rendering a manifest is not that gate.

Contributors can run `make test-ui-kind-renderer` to build and load a throwaway
renderer image into a disposable kind cluster. That test checks actual Job
execution, token-free Pod mounts, result parsing and cleanup. Kind's default
CNI does not enforce NetworkPolicy, so it does not establish the egress-denial
gate; that requires a separate cluster with an enforcing CNI and a negative
network probe.
`make test-ui-kind-runtime` builds a throwaway UI image, boots the rendered
single-replica installation, verifies its TLS sidecar with a trusted scratch
certificate and an unauthenticated API refusal, then replaces the Pod and
checks that a marker on its private PVC survives. It does not establish OIDC
login, deployment, dispatcher recovery or backup/restore acceptance.

The UI container binds to `127.0.0.1:8000` inside its Pod. A second container
terminates TLS on port 8443 and proxies to that loopback listener. The Service
exposes only TLS on port 443. The Ingress must re-encrypt to the Service; the
renderer requires a controller-specific backend HTTPS annotation so a
controller does not silently use cleartext HTTP. The UI's exact public HTTPS
origin is passed to its OIDC and same-origin checks.
The gateway copies the pinned image's Caddy binary to its private scratch
volume before execution; the official binary carries a low-port file
capability that conflicts with the Pod's no-new-privileges setting, although
this gateway listens only on unprivileged port 8443.

## Render and review

Install `piceli[ui]`, choose immutable image digests, and create the target
namespace and a TLS Secret containing a certificate for the UI hostname.
Register the OIDC callback URL `https://piceli.example.test/auth/callback`
(or include the configured URL prefix). Supply at least one exact OIDC subject
that may inspect the namespace. Subject IDs appear in the Deployment command
arguments and should be treated as installation metadata, not credentials.

This example uses documentation addresses; replace the API and identity
provider CIDRs with the actual destinations the Pod reaches. Standard
Kubernetes NetworkPolicy selects IP ranges, not DNS names. The installed CNI
must enforce NetworkPolicy, and the selected Ingress controller must really
support HTTPS upstreams. If the controller runs with `hostNetwork`, its source
may not match the Pod selector; check that before applying this policy.

```python
from pathlib import Path

from piceli.server.cluster_install import ClusterInstallConfig, cluster_install_yaml

config = ClusterInstallConfig(
    namespace="shop",
    origin="https://piceli.example.test",
    api_server="https://kubernetes.default.svc:443",
    oidc_issuer="https://id.example.test",
    oidc_metadata_url="https://id.example.test/.well-known/openid-configuration",
    oidc_client_id="piceli-ui",
    authorized_subjects=("OPERATOR_SUBJECT",),
    authorized_access_subjects=(),
    ui_image="registry.example.test/piceli@sha256:" + "a" * 64,
    gateway_image="registry.example.test/caddy@sha256:" + "b" * 64,
    tls_secret="piceli-ui-tls",
    ingress_class="nginx",
    ingress_namespace="ingress-nginx",
    ingress_pod_labels={"app.kubernetes.io/name": "ingress-nginx"},
    backend_tls_annotation_key="nginx.ingress.kubernetes.io/backend-protocol",
    backend_tls_annotation_value="HTTPS",
    api_egress_cidrs=("192.0.2.10/32",),
    oidc_egress_cidrs=("192.0.2.11/32",),
    state_size="5Gi",
    storage_class="standard",
)
Path("piceli-ui.yaml").write_text(cluster_install_yaml(config))
```

Replace both sample image digests with the content-addressed images you
built and trust. The UI image must contain the installed `piceli[ui]` package;
the gateway image must provide `sh`, `cp` and `/usr/bin/caddy` (as the official
Caddy image does) to run the rendered Caddyfile. Rendering does
not validate image contents or contact a registry. For an Ingress controller
other than NGINX, set its class, controller namespace and Pod labels, and the
annotation that makes it connect to the Service over HTTPS. An empty or HTTP
backend setting is rejected by the renderer. `url_prefix="/piceli"` is
supported when the identity provider has the matching callback URL.
`authorized_access_subjects` is optional and must be a subset of
`authorized_subjects`. It adds `--authorized-access-sub` for those subjects
and enables a local-client access ticket; it does not grant deployment writes
or claim that a port opens on the browser's machine. A local client must
establish the forward separately. Leaving it empty grants no access tickets.
Setting it requires `experimental=True`.

Review the rendered Role, ClusterRole, NetworkPolicies, projected credentials,
image digests and Ingress route. With an explicit disposable target, the
following commands perform a server-side check and an installation:

```sh
kubectl --kubeconfig ./my-cluster.kubeconfig --context kind-my-cluster \
  apply --dry-run=server -f piceli-ui.yaml
kubectl --kubeconfig ./my-cluster.kubeconfig --context kind-my-cluster \
  apply -f piceli-ui.yaml
kubectl --kubeconfig ./my-cluster.kubeconfig --context kind-my-cluster \
  -n shop rollout status deployment/piceli-ui
```

The installer needs cluster permission to create the name-scoped ClusterRole
and ClusterRoleBinding for identity reads. The UI ServiceAccount receives only
`get` on the `kube-system` and target Namespace objects at cluster scope. Its
default Role grants namespace observation and Pod logs, with no write verbs
or Secret read. The `piceli-renderer` ServiceAccount has no
RoleBinding and does not automatically receive a token. The
`piceli-renderer-deny-egress` policy denies ingress and egress for Pods labelled
`app.kubernetes.io/component: renderer`; any renderer Job must carry that
label. The UI can list NetworkPolicies in its namespace to reject an
additional policy that would grant renderer egress.
Each renderer Job mounts an immutable, per-evaluation ConfigMap. The Job
receives only approved source and request hashes, checks them inside the
container, and copies verified source bytes to private scratch space before
importing them. A deleted and recreated ConfigMap cannot silently change the
approved source. Other actors with permission to change Jobs, Roles or the
installation itself remain inside the namespace's administrative trust boundary.

## Optional manual delivery profile

The profile is experimental, unsupported and requires `experimental=True`
on `ClusterInstallConfig`. It puts `release.toml` and precisely the listed source
files in a read-only ConfigMap volume. A credential-free init container checks
their reviewed SHA-256 digests and copies them as regular, read-only files to
`/opt/piceli/source`, backed by a private `emptyDir`. This is necessary because
Kubernetes presents ConfigMap entries as symlinks, which the source evaluator
correctly refuses to execute. The init container has no projected service
account token or other credential mount. Use only non-secret source: ConfigMap
data and Deployment arguments are visible to
accounts with Kubernetes read permission. Keep kube credentials, TLS keys,
secret values and operation state on their separate mounted paths. The release
definition must identify this installation and use local state on the PVC:

```toml
[target]
kubeconfig = "/var/lib/piceli/control/target.kubeconfig"
context = "piceli-incluster"
namespace = "shop"

[release]
name = "shop"
owner = "shop"
field_manager = "piceli-ui"
composition = "composition.py:build"
state_dir = "/var/lib/piceli/control/release-state"
state = "local"
```

The composition and every imported local module or data file must be in
`source_file_allowlist` and `source_files`. This profile refuses exec
credential plugins, node pins and cluster-state mode because the manifest
does not grant their extra cluster permissions. Optional catalog, journal or
secret-store file paths must also stay under `/var/lib/piceli/control/`.

```python
from pathlib import Path

from piceli.server.cluster_install import DeployResourceRule, ManualDeliveryConfig

manual = ManualDeliveryConfig(
    release_definition_toml=Path("release.toml").read_text(),
    source_files={"composition.py": Path("composition.py").read_text()},
    source_file_allowlist=("composition.py",),
    renderer_image="registry.example.test/piceli-renderer@sha256:" + "c" * 64,
    renderer_platform="linux/arm64",
    authorized_deploy_subjects=("OPERATOR_SUBJECT",),
    deploy_resources=(
        DeployResourceRule(
            api_group="apps",
            resource="deployments",
            verbs=("get", "list", "watch", "create", "patch", "update", "delete"),
        ),
    ),
)
# Pass manual=manual and experimental=True to ClusterInstallConfig.
```

The deploy subjects must also appear in `authorized_subjects`. The renderer
image must be pinned by digest, contain the same Piceli version and run on the
declared platform. `cluster-serve` receives the mounted definition, source
root and file allowlist as explicit command arguments. The manual Role adds
create/get/delete on ConfigMaps and Jobs for source evaluation, plus exactly
the supplied resource-plural/API-group rules. It does not add Secret or
cluster-scoped writes automatically. RBAC limits kinds and namespaces, not
object names; Kubernetes RBAC cannot restrict `create` by name. For custom
resources, verify through API discovery that each supplied resource is
namespaced before granting it. Add each workload kind the approved release
actually needs, then review the generated Role with the deployment plan.
Some release features may need more permissions or integrations than this
profile supplies; the service must show those failures rather than broadening
its Role silently.

The UI Pod uses a projected, rotating service-account token and the
namespace's `kube-root-ca.crt`; token bytes do not enter the rendered manifest.
The Pod has no automatic service-account mount. Its single-replica Deployment
uses `Recreate` and a ReadWriteOnce PVC at `/var/lib/piceli`. The TLS private
key stays in the pre-existing Secret, outside the PVC. A configuration change
rolls the Pod; after rotating the TLS Secret, restart the gateway Pod so Caddy
loads the new files. OIDC sessions live in memory and require login again
after restart.

## Back up and restore state

Keep the rendered manifest, image digests, Piceli version and an offline
snapshot of the state PVC together. The PVC holds the generated kubeconfig
file references and, when manual operations are enabled, the operation
control state. Preserve storage encryption and access controls for that
snapshot. Back up the TLS Secret and OIDC client registration through their
own secret-management systems; the manifest intentionally contains neither
the private key nor provider tokens.

Before a snapshot, stop new admissions and let any active operation reach a
recorded terminal or safely resumable state. Scale the UI Deployment to zero
using an explicit kubeconfig/context, wait for its Pod to exit, and take a
storage-consistent CSI VolumeSnapshot or an offline copy supported by the
storage provider. Do not copy SQLite or journal files from a running Pod as a
recovery point. Bring the Deployment back to one replica after the snapshot.

For recovery, keep the old claim intact. Restore the snapshot to a new PVC,
set `state_claim` to that new claim in `ClusterInstallConfig`, render and review
the replacement manifest, then apply it while the UI is scaled down. Start
one replica, verify target identity, OIDC login, observed resources and
existing operation records before admitting new work. A record with ambiguous
execution is not proof of success; follow the operation's recovery path
instead of repeating a write blindly. The template does not automate snapshot
creation or recovery, because CSI classes and retention policies are
cluster-specific.
