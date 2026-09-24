# Operations Lens

```{admonition} Maturity: preview
:class: note

The operations lens is under active development. Its REST API and UI may change
between minor releases. See the {doc}`roadmap` for every feature's status.
```

The operations lens answers three questions about a deployment:

- *What does this release declare?*
- *What is actually running in the cluster?*
- *How do I reach a service safely from my machine?*

It works from a {doc}`deployment session archive <deployment_planning>` and an
explicitly selected kubeconfig. It is available as a Python API, as JSON CLI
commands, and as a small local web UI with a REST API.

## Where it runs

The lens runs **on the operator's own machine**, not inside the cluster.

- It serves on loopback only (`127.0.0.1`); binding to another address is refused.
- It uses the kubeconfig and context you pass explicitly and never falls back to
  ambient credentials. `--context` is **required** on every command that takes
  `--kubeconfig`: the file's `current-context` is never used, neither by the
  lens nor by the `kubectl` processes it starts (they always get `--context`).
- The kubeconfig goes through the same checks as `piceli release`: proxies,
  `insecure-skip-tls-verify`, non-https servers and legacy `auth-provider`
  users are refused (`target-refused`, `auth-provider-refused`). A context
  whose user runs an exec credential plugin (GKE, EKS, AKS, OIDC) needs
  `--allow-exec`, optionally with `--exec-sha256 sha256:…`; see
  {doc}`managed_clusters`. For commands that run `kubectl` (`forward-run`,
  `logs-run`, `forwards apply`, the UI's pods and logs) the plugin is resolved
  and pinned first, but `kubectl` runs it with your environment.
- Refusals print `{"state": "refused", "reason": …, "code": …}` and exit `2`.
- Port forwards are `kubectl port-forward` processes it starts and supervises
  itself. They bind to loopback, and it never adopts or kills processes it did
  not start.
- Cluster credentials never leave your machine, and workloads in the cluster
  cannot reach the lens.

## Reconcile a session with the cluster

```bash
piceli observe status \
  --archive ./session.archive.json \
  --kubeconfig ~/.kube/config --context my-cluster
```

The JSON report classifies every object:

| Classification | Meaning |
| --- | --- |
| Declared | Part of the session archive and present in the cluster. |
| Missing | Declared by the archive but not found in the cluster. |
| Undeclared | Present in the namespace but not part of the archive. This is information only; Piceli never adopts or deletes such objects automatically. |
| Unknown | Could not be inspected (for example a transient API error). Never treated as absent. |

## Logs

```bash
# Print the exact kubectl argv without running it
piceli observe logs-command --namespace my-app --target deployment/api --tail 200 \
  --kubeconfig ~/.kube/config --context my-cluster

# Run it in the foreground
piceli observe logs-run --namespace my-app --target deployment/api --tail 200 \
  --kubeconfig ~/.kube/config --context my-cluster
```

## Saved port forwards

Forward preferences are stored per local user as non-secret JSON with owner-only
permissions.

```bash
piceli observe forward-save --user "$USER" --name api \
  --namespace my-app --target service/api \
  --local-port 18080 --remote-port 8080

piceli observe forward-list --user "$USER"
piceli observe forward-run  --user "$USER" --name api \
  --kubeconfig ~/.kube/config --context my-cluster
```

## Web UI and REST API

```bash
piceli observe serve --archive ./session.archive.json \
  --kubeconfig ~/.kube/config --context my-cluster \
  --user "$USER" --port 9876 --ui-config ./piceli-ui.toml
```

Open `http://127.0.0.1:9876/`. With `--user`, the server restores that user's
saved forwards and supervises the processes it starts. `--namespace` defaults to
the archive's namespace when the archive has exactly one. The page refreshes every
few seconds.

The same data is available as JSON:

| Endpoint | Purpose |
| --- | --- |
| `GET /healthz` | Liveness |
| `GET /v1/status` | Declared vs. live inventory |
| `GET /v1/forwards`, `GET /v1/preferences` | Saved and running forwards, with connection health |
| `GET /v1/shortcuts` | Configured shortcuts, with connection health |
| `GET /v1/pods`, `GET /v1/logs` | Pods and bounded log tails |
| `POST /v1/forwards/start`, `/stop`, `/add`, `/delete` | Manage forwards |

Every `/v1/*` request, `GET` included, needs either the per-process token
embedded in the page (`X-Piceli-Local-Token` header) or a bearer token for a user
in the operator user store. Bearer users with the `viewer` role are read-only. The
server only answers requests whose `Host` is `127.0.0.1`, `localhost` or `[::1]`
on its own port, and it rejects cross-origin requests. This protects against DNS
rebinding. Responses carry a strict Content-Security-Policy. Error bodies contain
fixed error codes only; details go to the server log.

Interactive elements carry stable `data-testid` attributes, so the UI can be
driven by browser automation and AI agents.

## Dashboard configuration

Without configuration the dashboard shows no shortcuts and groups live workloads
by their `app.kubernetes.io/component` label (falling back to `app`). To add
one-click shortcuts, topology tiers and header badges, pass a TOML file with
`--ui-config` (or set `PICELI__UI_CONFIG`) on `observe serve` or `operator serve`:

```toml
topology_subtitle = "My application tiers"

[[shortcuts]]
id = "web"
label = "Web UI"
target = "service/web"      # service/<name>, deployment/<name> or pod/<name>
local_port = 3000
remote_port = 3000
path = "/login"             # optional: path opened by the "Open" link

[[tiers]]
name = "Application"
components = ["web", { name = "worker", role = "Task worker" }]

[inventory]
# extra labels that mark live objects as managed by this application
managed_labels = { "app.kubernetes.io/part-of" = "my-app" }
revision_label = "my-app/revision"
```

A shortcut uses its own `namespace` if set, otherwise the namespace being served.
Unknown keys are rejected, so typos fail fast. A complete example lives in
[`examples/ui-config.toml`](https://github.com/pynenc/piceli/blob/main/examples/ui-config.toml).

## Access profiles and connection health

A running `kubectl port-forward` process is not proof that its connection
works: when the upstream pod goes away, the process can stay alive while it
resets every connection. The supervisor therefore probes each forward it owns
and restarts one whose probe keeps failing.

An access profile is the same TOML file as `--ui-config`. Each shortcut may add
a health probe and a restart policy:

```toml
[[shortcuts]]
id = "api"
label = "HTTP API"
target = "service/api"
namespace = "my-app"        # optional; otherwise --namespace
local_port = 18080
remote_port = 8000
required = true             # false: skip it when the local port is taken

  [shortcuts.health]
  type = "http"             # "tcp" (default) or "http"
  path = "/healthz"
  expect_status = [200, 399]
  interval = 5.0            # seconds between probes
  timeout = 2.0
  failure_threshold = 3     # consecutive failures before a restart
  startup_grace = 3.0       # failures right after a (re)start don't count

  [shortcuts.restart]
  backoff_initial = 1.0     # doubles per consecutive restart ...
  backoff_max = 30.0        # ... up to this cap
  max_restarts = 10         # then the forward is marked failed
```

- A `tcp` probe connects to the loopback port and watches the connection for a
  moment (`settle`, 0.3 s by default). A forward whose upstream is gone accepts
  the connection and then closes or resets it, which counts as a failure.
- An `http` probe sends `GET path` and expects a status within `expect_status`.
  The older `health_path = "/x"` key still works: it is an HTTP probe that
  accepts any status below 500. Use either `health_path` or `[shortcuts.health]`,
  not both.
- `max_restarts` counts consecutive restarts without a healthy probe in between.
  After that the forward stays stopped with state `failed` until you start it
  again.
- Probes only contact `127.0.0.1`. They never send credentials.

Start and supervise every declared forward in the foreground:

```bash
piceli observe forwards apply --profile ./access.toml \
  --kubeconfig ~/.kube/config --context my-cluster --namespace my-app
```

Before it starts anything, `apply` runs a port-conflict preflight. If another
process already serves a required shortcut's local port, it prints the
conflicts and exits with code 2 without starting any forward. An optional
shortcut (`required = false`) on an occupied port is listed as `external` and
skipped. While running, it prints one JSON line per status change and stops
every forward it owns on Ctrl-C, `SIGTERM` or `SIGHUP`. Use `--only ID`
(repeatable) to supervise a subset.

To check the declared endpoints once and print JSON, run
`piceli observe forwards status --profile ./access.toml`. It probes loopback
only and exits with code 1 if a required forward is unhealthy.

To get the dashboard with the same forwards, pass the profile as `--ui-config`
and add `--start-shortcuts` to `observe serve`.

Each forward's status (`/v1/forwards`, `/v1/shortcuts`, and the dashboard) has
these fields:

| Field | Meaning |
| --- | --- |
| `state` | Process state: `starting`, `running`, `degraded`, `backoff`, `failed` or `stopped` |
| `health` | Connection health: `healthy`, `unhealthy`, `starting`, `restarting`, `conflict`, `failed` or `stopped` |
| `restarts` | Restarts since the forward was added |
| `consecutive_failures` | Probe failures since the last healthy probe |
| `last_error`, `last_probe_at` | Last failure reason and last probe time (UTC) |
| `probe` | The effective probe settings |

A local port that another process already serves is reported as `conflict`.
The supervisor never kills that process and never spawns a forward on the port.

The broader operator features (releases, promotion, backups) are described in
{doc}`operator_workflow`.
