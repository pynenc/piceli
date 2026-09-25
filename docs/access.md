# Reach your services from your laptop

This page shows how to declare, in the typed model, how each service is reached
from your laptop, and then use one command to check whether the app is up
(`piceli status`) and one to forward its ports (`piceli access`). You do not
need `kubectl` or `curl` for either.

```{admonition} Maturity: preview
:class: note

`app.access.forward`, `piceli access` and `piceli status` are new in this
release. They work and are tested (including against `kind`), but options and
JSON fields may still change before 1.0. Fields of `piceli.status.v1` are only
added, never removed, within a major version. See the {doc}`roadmap`.
```

## How it works

An access declaration is part of the model, but not part of the cluster:

- it renders to **no Kubernetes object** and changes no manifest, so
  `piceli render` and `piceli release plan` show exactly what they showed
  before;
- `piceli access` starts one loopback `kubectl port-forward` per declaration,
  probes it (TCP or HTTP) and restarts it when it stops answering;
- `piceli status` reads the release state, the live workloads and the
  declared forwards, and prints whether the app is up and each URL;
- the local dashboard shows the same forwards as its shortcuts, so a separate
  `--ui-config` TOML file becomes optional.

Everything uses the target's explicit kubeconfig file and context. Piceli never
reads `KUBECONFIG`, `~/.kube/config` or the current context.
An exec credential plugin (GKE, EKS, AKS, OIDC) runs only when the target
allows it: `[target] allow_exec = true` or `Target.kubeconfig(…,
allow_exec=True)` (see {doc}`managed_clusters`); otherwise `status` reports
`exec-auth-not-allowed` in `errors` and `access` refuses with that code.

## Prerequisites

New to Piceli? Start with {doc}`getting_started/index`.

- An app declared with the typed model (see {doc}`typed_apps`) and released
  with `piceli release` (see {doc}`release_cli`).
- `kubectl` on your `PATH` (or pass `--kubectl PATH`) for `piceli access`.
- The kubeconfig context in `release.toml` can read Deployments and Pods and
  create port forwards in the namespace.

## Steps

1. **Declare access on a Service.** Pass `access=app.access.forward(...)` to
   `app.service(...)`:

   ```python
   from piceli import App


   def build(ctx):
       app = App("shop")
       web = app.deployment("web", image=ctx.image("web"), ports=[3000])
       app.service(
           web,
           port=3000,
           access=app.access.forward(local=18080, path="/login", health="/healthz"),
       )
       return app  # return the App, not app.composition(ctx)
   ```

   - `local` is the port on `127.0.0.1` (unique within the app).
   - `port` picks the Service port by number or name (default: the first).
   - `path` is appended to the URL that `status` prints.
   - `health` is `None` or `"tcp"` for a TCP probe, a path such as
     `"/healthz"` for an HTTP probe that expects 200-399, or a full
     `HealthProbe` (status range, interval, timeout, failure threshold).
   - `name` (the forward id, default: the Service name), `label`,
     `description`, `required=False` (skip instead of refusing when the port is
     taken) and `restart=RestartPolicy(...)` are optional.

   A Deployment may declare `access=` too; it then forwards to the pods, on
   the main container's port. Prefer a Service.

   ```{important}
   The composition function must **return the App** (a release renders it), so
   `access` and `status` can see its declarations. A function that returns
   `app.composition(ctx)` still deploys, but has no declared access.
   ```

2. **Check whether the app is up.**

   ```sh
   piceli status release.toml
   ```

   Expected output (exit code `0`):

   ```text
   shop is UP  (namespace shop, context kind-shop)
   release    shop-4d2f7a0c9b1e  ready, apply at 2026-09-24T10:00:00+00:00
   workloads
     ready        Deployment/web  1/1  web=0f5c2a9e81d4
   access     down
     down      web          http://127.0.0.1:18080/login  -> service/web:3000
   Start the forwards with: piceli access release.toml
   ```

3. **Forward the ports.**

   ```sh
   piceli access release.toml
   ```

   Expected output; it keeps running until Ctrl-C:

   ```text
   Forwarding 1 port(s) of shop (namespace shop); Ctrl-C stops them.
     web          http://127.0.0.1:18080/login  -> service/web:3000
   web: starting
   web: healthy
   ```

   Open the printed URL. `--only web` forwards one declaration;
   `--dashboard 9876` also serves the local dashboard on
   `http://127.0.0.1:9876` with these forwards as its shortcuts.

4. **Check again.** In another terminal, `piceli status release.toml` now shows
   `up` next to each forward.

   `up` means the port is held by the `kubectl port-forward` that Piceli
   started for this declaration (same context, namespace, target and ports,
   started by `piceli access` or a dashboard) and its health probe passes.
   If another process holds the port (another project's forward, a dev
   server), the forward is `occupied`, never `up`, and only that process's
   pid is shown:

   ```text
   access     down
     occupied  web          http://127.0.0.1:18080/login  -> service/web:3000
         port 18080 is held by pid 4242, not by piceli (status-port-occupied)
   ```

## Targets

`TARGET` is either:

- a **`release.toml` path**: the cluster comes from `[target]` and the
  declarations from the App that `[release] composition` returns; or
- **`module:attr`** (or `file.py:attr`) of an object with `.app` (a piceli
  `App`) and `.target` (with `.kubeconfig`, `.context` and `.namespace`), such
  as a pipeline object. For a `piceli.pipeline.Pipeline` the release state
  `piceli deploy` keeps (`state_dir/release`) is read automatically. For
  other objects, `.release_spec` (a `ReleaseSpec` or a path to
  `release.toml`) adds the release state, and `.last_checks()` adds the
  latest checks result.

## If it fails

| You see | Meaning | Next step |
| --- | --- | --- |
| `access-port-conflict` | A declared local port is held by another process. The rejection lists `conflicts[].owner` with its `pid`, `command` and parent (often an older `piceli access`, a dashboard or a `kubectl port-forward`). Piceli never takes a port over. | Stop that process (or the dashboard that supervises it), or change `local=`, then run again. |
| `access-none-declared` | No forward is declared, or the composition function returns `app.composition(ctx)`. | Add `access=` and return the App. |
| `access-unknown-forward` | `--only` names an id that does not exist. | Use an id from `piceli status TARGET --json`. |
| `access-kubectl-missing` | No `kubectl` found. | Install it or pass `--kubectl PATH`. |
| `occupied` / `status-port-occupied` in `piceli status` | A declared local port is held by a process Piceli did not start for this forward. Only its pid is reported. | Stop that process (`lsof -nP -iTCP:PORT -sTCP:LISTEN`) or change `local=`, then run `piceli access`. |
| `access-kubeconfig-invalid` | The kubeconfig file or context is missing or unusable. | Fix `[target] kubeconfig` / `context`. |
| `exec-auth-not-allowed` | The context's user runs an exec plugin and the target does not allow it. | Review the plugin, then set `allow_exec` on the target. |
| `access-target-invalid` | TARGET is neither a readable `release.toml` nor a `module:attr` object with `.app` and `.target`. | Read the message on stderr. |
| `status` exits `1` with `state: degraded` or `down` | Some workloads are not ready. | Read `workloads[].problems` (for example `CrashLoopBackOff`, restarts). |
| `status` shows `state: unknown` and `errors: ["status-cluster-unreadable"]` | The cluster could not be read. | Check that the context reaches the cluster, then retry. |
| A forward shows `unhealthy` | Piceli's forward holds the port but the health probe fails. | The upstream is down or not ready: check the workload (`piceli status`). |

Every code is explained by `piceli explain <code>` and in {doc}`reference/errors`.

## Contract: `piceli status`

| | |
| --- | --- |
| Arguments | `TARGET` (text, required); `--json` (flag); `--timeout SECONDS` (float, 1-60, default 10) |
| Reads | `release.toml` or the target module, the release `state_dir` (catalog, history), the cluster through the explicit kubeconfig (Deployments, StatefulSets, DaemonSets, Pods), and `127.0.0.1` for each declared forward |
| Writes | Nothing |
| Approval | None; read-only. Safe to retry. |
| Exit codes | `0` every workload is ready; `1` not ready or the cluster could not be read; `2` TARGET rejected |
| Output | Human text, or with `--json` one `piceli.status.v1` object ([schema](schemas/piceli-status-v1.schema.json)) |

The JSON object:

```json
{
  "schema": "piceli.status.v1",
  "state": "up",
  "app": "shop",
  "namespace": "shop",
  "context": "kind-shop",
  "source": "release-spec",
  "release": {
    "name": "shop",
    "state": "ready",
    "current": "shop-4d2f7a0c9b1e",
    "previous": null,
    "latest": {"release": "shop-4d2f7a0c9b1e", "intent": "apply", "state": "ready",
               "at": "2026-09-24T10:00:00+00:00", "execution_id": "…"},
    "images": {"web": "registry.example/shop/web@sha256:…"},
    "pending_plans": 0
  },
  "workloads": [
    {
      "kind": "Deployment", "name": "web", "health": "ready", "ready": true,
      "replicas": {"desired": 1, "ready": 1, "updated": 1, "available": 1, "current": 1},
      "images": [{"container": "web", "image": "registry.example/shop/web@sha256:…",
                  "digest": "sha256:…", "pinned": true, "running": ["sha256:…"]}],
      "problems": [],
      "error": null
    }
  ],
  "access": {
    "state": "up",
    "forwards": [
      {"id": "web", "label": "web", "target": "service/web",
       "url": "http://127.0.0.1:18080/login", "local_port": 18080, "remote_port": 3000,
       "required": true, "forward": "up", "error": null,
       "owner": {"port": 18080, "pid": 4242, "command": "kubectl … port-forward service/web 18080:3000",
                 "parent": {"pid": 4200, "command": "… piceli access release.toml"}},
       "probe": {"type": "http", "path": "/healthz", "expect_status": [200, 399],
                 "interval": 5.0, "timeout": 2.0, "failure_threshold": 3}}
    ]
  },
  "checks": null,
  "errors": []
}
```

- `state`: `up` (every workload ready), `degraded`, `down` (none ready or all
  missing) or `unknown` (the cluster could not be read, or no workload).
- `workloads[].health`: `ready` (the controller saw the latest spec, every
  desired replica is updated and ready, and no old replica is left: the rule
  `piceli release apply` waits for), `idle` (scaled to zero), `progressing`,
  `unavailable`, `missing` or `unknown`.
- `workloads[].images[].digest`: the pinned digest of the spec image, else the
  single digest the pods report; `running` lists every digest the pods report.
- `access.forwards[].forward`: `up` (Piceli's forward for this declaration
  holds the port and the probe passes), `unhealthy` (Piceli's forward holds
  the port, the probe fails), `occupied` (another process holds the port;
  `error` is `status-port-occupied` and `owner` carries only its `port` and
  `pid`, with `command` and `parent` `null`) or `down` (nothing listens).
- `checks`: the latest checks result when the target records one, else `null`.
- `errors`: error codes, for example `status-cluster-unreadable`.

## Contract: `piceli access`

| | |
| --- | --- |
| Arguments | `TARGET` (text, required); `--only ID` (repeatable); `--json` (flag); `--kubectl PATH` (default `kubectl`); `--poll SECONDS` (0.1-60, default 1); `--dashboard PORT` (optional); `--ui-config PATH` (optional, also `$PICELI__UI_CONFIG`) |
| Reads | `release.toml` or the target module, the kubeconfig, `kubectl` |
| Writes | Loopback ports only: one `kubectl port-forward --address 127.0.0.1` process per forward, each in its own process group, all stopped on exit. Nothing in the cluster. |
| Approval | None; it only reads the cluster. Ask the owner before starting long-running processes on their machine. |
| Exit codes | `0` stopped by Ctrl-C, SIGTERM or SIGHUP; `1` every forward gave up (`access-forwards-failed`); `2` rejected before starting anything |
| Output | Human lines, or with `--json` JSON lines: one `started` event, one `status` event per change, one `stopped` event |

```json
{"event": "started", "app": "shop", "namespace": "shop", "dashboard": null,
 "forwards": [{"id": "web", "url": "http://127.0.0.1:18080/login", "target": "service/web",
               "local_port": 18080, "remote_port": 3000}],
 "skipped": []}
{"event": "status", "id": "web", "state": "running", "health": "healthy", "restarts": 0, "url": "…", "owner": null, "…": "…"}
{"event": "stopped", "state": "stopped"}
```

Port conflicts: before starting anything, every declared local port is checked.
A required forward on a taken port rejects the command:

```json
{"state": "rejected", "reason": "access-port-conflict",
 "conflicts": [{"id": "web", "local_port": 18080, "required": true,
                "owner": {"port": 18080, "pid": 4242, "command": "kubectl … port-forward service/web 18080:3000",
                          "parent": {"pid": 4200, "command": "… piceli observe serve …"}}}]}
```

A forward with `required=False` on a taken port is listed under `skipped` and
not started. While supervising, a forward whose port another process takes is
marked `conflict` with that owner and is **not** retried: no supervisor (the
dashboard's or `piceli access`) waits for a declared port to free up and then
silently takes it back. The owner is found through `/proc` on Linux and
`lsof` on macOS; when neither works, `owner` is `null`.

## The dashboard

`piceli access TARGET --dashboard 9876` serves the local operations dashboard
(see {doc}`operations_lens`) with the model's forwards as its shortcuts and, if
no `--ui-config` declares tiers, one tier listing the app's workloads. `piceli
operator serve --access TARGET` does the same for the operator dashboard, and
also starts the declared forwards at launch (same port-conflict refusal as
`piceli access`; `--no-start-access` leaves them stopped until you click
Start). The model's declarations are explicit, so they start; a user's
*saved* forwards never start without `--restore-forwards` (see
{doc}`operations_lens`).
`--ui-config` still works: its shortcuts win over a model forward with the same
id, and its badges and tiers are kept.

```{figure} _static/img/dashboard-forwards.webp
:alt: The Port Forwards and Shortcuts tab of the local dashboard for the shop namespace. The api shortcut forwards service/api to 127.0.0.1:13080 and the web shortcut forwards service/web to 127.0.0.1:13000; both are running and healthy. The supervised forwards table lists the same two forwards with their local and remote ports, live links, health and process state.
:width: 100%

The shop example's two declared forwards (`access=app.access.forward(...)`
on `api` and `web`, see {doc}`deploy`) as dashboard shortcuts, each probed
and supervised on loopback.
```
