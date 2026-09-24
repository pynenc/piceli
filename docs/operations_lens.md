# Operations Lens

The operations lens answers three questions about a deployment:

- *What does this release declare?*
- *What is actually running in the cluster?*
- *How do I reach a service safely from my machine?*

It works from a {doc}`deployment session archive <deployment_planning>` and an
explicitly selected kubeconfig. It is available as a Python API, as JSON CLI
commands, and as a small local web UI with a REST API.

```{admonition} Early preview
:class: note

The operations lens is under active development. Its REST API and UI may change
between releases.
```

## Where it runs

The lens runs **on the operator's own machine**, not inside the cluster.

- It serves on loopback only (`127.0.0.1`); binding to another address is refused.
- It uses the kubeconfig and context you pass explicitly and never falls back to
  ambient credentials.
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
| `GET /v1/forwards`, `GET /v1/preferences` | Saved and running forwards |
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
target = "service/web"      # service/<name> or pod/<name>
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
Unknown keys are rejected, so typos fail fast.

The broader operator features (releases, promotion, backups) are described in
{doc}`operator_workflow`.
