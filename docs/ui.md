# Piceli web application

Status: experimental, under implementation. The local application serves a
bundled React interface from Python. Node is a contributor build dependency;
the installed wheel needs only `piceli[ui]`. No browser assets load from a CDN.

## Try the local UI without a cluster

From a source checkout, build the bundled browser assets once, then start the
foreground demo server:

```sh
make ui-install ui-build
make ui-fake-serve
```

Open `http://127.0.0.1:4177/applications` as printed. If you enter
`localhost:4177`, page navigation redirects to the configured address while
API requests stay restricted to that exact origin. The terminal prints the
address and Uvicorn's startup status; it stays occupied
while the server runs. Press
Ctrl+C to stop it. `PICELI_UI_FAKE_PORT=4180 make ui-fake-serve` selects another
loopback port. This demo uses a disposable fake Kubernetes API and an
inventory-only registration. You can inspect observed resources, but it cannot
approve or execute a deployment. It never reads your ambient kubeconfig.
After updating the server code or browser assets, stop and restart this
foreground process; it does not reload changes automatically. Opening the URL
from an external link is supported, while API requests still require the local
session established by that page.

To run the fake-API service and real-browser journeys together, install
Playwright's Chromium through the locked frontend toolchain and run:

```sh
cd ui && npm exec -- playwright install chromium && cd ..
make test-ui-fake
```

The browser journeys cover desktop, tablet and phone. They use the real Python
UI service against the disposable fake API; reports, profiles, server state and
subprocesses are removed when the runner exits. The combined target uses port
4184 for its browser server, so it can run while the demo occupies 4177;
`PICELI_UI_FAKE_TEST_PORT` overrides that port. `make test-ui` runs just the
service acceptance tests and does not require Chromium. The fake journeys do
not establish real-cluster deployment behavior; `make test-ui-kind-delivery`
uses a separate disposable kind cluster for that.

## Where Piceli runs

The UI server, the deployment target, and the source of a run are separate
choices. The Applications screen distinguishes a local process from an
authenticated scoped service, shows how many Kubernetes scopes are configured,
and reports whether this session can review, observe, or access no scope. Scope
authorization does not reveal the server's physical location. An action
appears only when the server grants its capability. A local process may run on
a laptop or remote machine; the target Kubernetes API may be elsewhere. A
browser action runs on the UI server host, not on the browser's laptop.

| Use case | Current path | UI behavior |
| --- | --- | --- |
| Laptop UI → explicit Kubernetes cluster | `piceli ui serve` with an explicit kubeconfig/context or release definition | Inspect; review and deploy when isolated rendering is configured |
| GitHub Actions or another CI runner → Kubernetes cluster | `piceli deploy --plan` and `piceli deploy --apply` with exact approval (see {doc}`ci`) | CLI works independently; the UI does not launch CI jobs |
| Local or remote UI host running a CI-style pipeline definition | The existing `piceli deploy` CLI remains available | Browser plan/apply for pipeline definitions is pending; a release-definition UI run does not stand in for it |
| Remote machine hosting the local UI → Kubernetes cluster | Run `piceli ui serve` on that machine and reach its loopback listener through an SSH tunnel | Operations and port forwards run on that machine |
| Authenticated scoped UI → Kubernetes namespace | `piceli ui cluster-observe` behind an HTTPS/OIDC gateway | Read-only observation today; an in-cluster install and manual deployment are pending |
| Direct application deployment to a machine without Kubernetes | No Piceli target/provider for this yet | No deployment action is offered |

For the SSH-tunnel case, keep the UI bound to loopback on the remote host and
forward the same port from your laptop, for example
`ssh -L 8000:127.0.0.1:8000 my-host`. Open `http://127.0.0.1:8000` locally.
The release definition and explicit Kubernetes credentials live on the remote
UI host. Port-forward sessions also bind there. This is a local UI process on a
remote host, not a publicly hosted multi-user service.

The existing CI deployment flow remains available while the browser workflow
develops. The browser cannot yet run a pipeline definition using the same
`piceli deploy` plan/apply path; that requires the pipeline's materialized
second-plan approval before an action can be exposed. Native Git reconciliation
and a browser action that starts a CI job are also not implemented. A
non-Kubernetes host target would need its own real planner, authorization and
execution backend before the UI can expose it.

Register an existing release definition without changing its layout:

```sh
piceli ui serve --definition release.toml --name my-app
```

The definition supplies the explicit kubeconfig, context and namespace. For an
inventory-only scope, supply those directly:

```sh
piceli ui serve --kubeconfig ./my-cluster.kubeconfig \
  --context kind-my-cluster --namespace shop --name shop
```

Open the printed loopback address. `--url-prefix /piceli` hosts the application
under that prefix, including deep links and API routes. Local sessions are
same-origin and scoped to the configured host. This mode is a loopback server,
including when started on a remote host behind an SSH tunnel. It is not a
public multi-user server.

To enable deployment for a release definition, also configure the durable
control directory, an explicit allowlist of source files, and a trusted local
renderer image by immutable Docker image ID. For example:

```sh
piceli ui serve --definition release.toml --name my-app \
  --control-dir ./ui-control --source-root . \
  --source-file composition.py \
  --renderer-image sha256:YOUR_PINNED_IMAGE_ID \
  --renderer-platform linux/arm64
```

Repeat `--source-file` for every module or data file the definition needs.
The source root must contain the configured composition entrypoint. Keep the
control directory, state, kubeconfig, secrets and build credentials outside
that allowlist. The renderer image must contain the same Piceli version as the
service; the image ID and platform are checked before evaluation. An
unavailable renderer disables deployment rather than evaluating consumer code
inside the API process. The source evaluation container runs without network,
deployment credentials or a writable root filesystem.

The browser first approves the exact frozen source preview, then reviews and
approves a separate deployment plan. The plan binds source, inputs, target,
checks and live preconditions. A changed plan needs a new review. Runs persist
in the control directory; closing the browser does not cancel work. The
Activity view shows their stages, receipts and linked recovery attempts. An
archived rollback creates a new reviewed plan and run. Direct CLI release runs
from the same local state appear as imported history with the provenance the
engine recorded; their original browser plan is not available. Inventory-only
registrations remain read-only.

Health, desired/live relation, operation state and observation freshness are
separate facts. A present resource does not establish health or an unchanged
deployment. Denied resource kinds produce partial inventory; unavailable
observation must not appear as an empty successful result. Unknown custom
resource health remains unknown.

The Resources inspector offers current and previous container logs for an
observed Pod, Deployment, StatefulSet, DaemonSet, Job or ReplicaSet. Workload
selection follows current pods; the selected pod UID and container are checked
again before every bounded read. A missing pod, denied read, or partial owner
observation is shown explicitly. Log reads refresh about once a second; the
browser retains at most 20,000 lines or 8 MiB, virtualizes visible rows and
reports gaps when consecutive tail reads do not overlap. Resource changes use
a shared scoped list/watch cache with a replayable observation stream; an
expired stream cursor causes a reset and a fresh read.
The Table and Relationships views use the same observed resource identities
and inspector. Relationships follow observed owner UIDs; an unobserved owner
is labelled as such rather than inferred.

Where `kubectl` is installed, a forwardable Pod, Deployment or Service can be
opened from its inspector. The UI asks for a target port and a local port;
Piceli verifies the selected live UID and supervises the forward. “Ready”
means its own loopback listener passed a TCP probe. An occupied port is
refused, and closing the server stops forwards it owns. The binding is on the
host running this local UI, which may differ from the browser's machine.
Ended access sessions remain visible briefly for diagnosis: the local server
retains at most 128 terminal records for ten minutes, then releases their
supervisor references. Active sessions are not evicted by this history limit.

`make test-ui-access-retention-smoke` exercises a few real supervised local
forward lifecycles through the disposable fake API.
`make test-ui-access-retention-bound` runs 129 sessions to cross the terminal
history limit. The 30-minute `make test-ui-access-retention` gate repeatedly
starts and stops an owned forward and checks process, thread, socket and memory
return. All three print compact JSON and remove their temporary fake
credentials and executable on exit. None contacts a real cluster.

The legacy `piceli operator serve` entry point remains during migration and
prints a deprecation notice. Its inventory is labelled as observation, its
catalog selection is not deployment rollback, and an unconfigured artifact
inventory has unknown totals. The old `/v1/releases/rollback` compatibility
route returns `action: "catalog-selection"`, `selected`, and `deployed: false`.
Actual deployment rollback is available through reviewed release plans in the
CLI and through the new web workflow when isolated rendering is configured.

The implementation tracker records the release gates. Native Git delivery and
in-cluster manual deployment are not available in this local interface.

An experimental **read-only** cluster observation command is available for an
installation that supplies a projected service-account token, its CA file, an
explicit Kubernetes API origin and a public HTTPS OIDC client. It binds only
to loopback for a separately configured TLS gateway sidecar:

```sh
piceli ui cluster-observe \
  --api-server https://kubernetes.default.svc:443 \
  --ca-file /var/run/secrets/kubernetes.io/serviceaccount/ca.crt \
  --token-file /var/run/secrets/kubernetes.io/serviceaccount/token \
  --namespace shop --control-dir /var/lib/piceli \
  --origin https://piceli.example.test \
  --oidc-issuer https://id.example.test \
  --oidc-metadata-url https://id.example.test/.well-known/openid-configuration \
  --oidc-client-id piceli-ui --authorized-sub OPERATOR_SUBJECT
```

The command generates a private kubeconfig containing file paths, not token
bytes. It refreshes the projected token before each Kubernetes request. Only
the named OIDC subjects can inspect the configured namespace and read its
container logs. This read-only mode does not list Secrets or ConfigMaps.
Sessions remain in the server process, expire with the signed
ID token, and require same-origin CSRF protection for writes. This command has
no deployment or server-side port-forward action. An actual in-cluster manual
deployment requires the isolated cluster renderer, durable installation state
and remote local-client access still tracked under Wave 4.

Contributors can run `make test-ui-performance` to measure the server's shared
observation fixture: 50 applications, 5,000 resources in three scopes and ten
concurrent API clients. It prints a small JSON result and leaves no artifacts.
This fixture does not measure browser rendering or network latency.

`make test-ui-browser-performance` measures ten isolated Chromium processes
against the same 50-application/5,000-object synthetic scope at desktop and
phone sizes. It adds 100 ms to API requests, applies fourfold CPU throttling,
and reports first useful list timing from each browser's first navigation in
turn while all ten processes remain alive, concurrent resource timing, selection
response and scroll long tasks. The command fails if the published 2 s
first-list, 100 ms selection or 50 ms scroll-task targets are missed. The
fixture also reads a scoped Pod log through the real service at 1,000 lines/s
for ten seconds and checks the visible line position and log-scroll tasks. A
missed polling interval is shown as a gap. Ten simultaneous cold starts are a
distinct stress case and may take longer. These synthetic observations do not
replace a real Kubernetes watch or an installed-cluster log-load run.
