# Piceli web application

The optional `piceli[ui]` package serves a bundled React application. It does
not need Node at runtime and does not read an ambient kubeconfig or context.
The UI runs in one of three modes: a local, single-user process with an
explicit target; the composition UI that `piceli cluster init` installs in the
cluster and you open through a port-forward; or an OIDC-authenticated service
installed inside a Kubernetes cluster.
Each mode offers only the actions configured by its operator.

## What it shows

The screenshots come from `make ui-clips`, which records fixed journeys on a
disposable fake Kubernetes API (no real cluster or credential).

```{image} _static/ui/applications.png
:alt: The Applications page of the Piceli web UI
:width: 720px
```

```{image} _static/ui/environments.webp
:alt: Per-branch environments in the Piceli web UI: branch, namespace, commit, state and health
:width: 720px
```

```{image} _static/ui/gitops.png
:alt: The GitOps page: controller status and per-branch state, with a branch waiting for approval
:width: 720px
```

```{image} _static/ui/cluster.png
:alt: Cluster nodes, k3s mirror restart state, registry storage and service health
:width: 720px
```

```{image} _static/ui/pipeline-plan.png
:alt: A Pipeline plan with its stages and the approval step for the exact combined hash
:width: 720px
```

## Try the local fake UI

In a source checkout:

```sh
make ui-install ui-build
make ui-fake-serve
```

Open the exact loopback URL printed by the foreground server, including its
one-time launch token. The URL exchanges the token for a session and redirects
to a token-free page. The token is also held in a mode-`0600` file at
`$XDG_STATE_HOME/piceli/ui/launch-token-4177` (or
`~/.local/state/piceli/ui/launch-token-4177`) while the server runs. One port
has one locked token file; another process cannot overwrite a running server's
token. Press Ctrl+C to stop. `PICELI_UI_FAKE_PORT=4180 make ui-fake-serve`
selects another port. The demo observes a disposable fake Kubernetes API and
cannot deploy.

For service and browser acceptance with that fake API, run `make test-ui-fake`
(`make test-ui-composition` runs only the composition journeys).
It uses port 4184 by default (`PICELI_UI_FAKE_TEST_PORT` changes it) and checks
desktop, tablet and phone. The test runner removes its servers, browser state
and temporary files on exit.

## Where the UI can run

| Use case | Command and target | Browser actions |
| --- | --- | --- |
| Laptop or remote machine to a Kubernetes cluster | `piceli ui serve --kubeconfig PATH --context NAME --namespace NAME` | Inventory, resources, logs and supervised access |
| Local or remote host with a release definition | `piceli ui serve --definition release.toml` plus the configured isolated renderer | Source review, release plan, deploy and activity |
| Local or remote host with a CI-style Pipeline | `piceli ui serve --pipeline module:attribute` | Pipeline plan, two approvals when images must be delivered, pre-rollout checks, restore points and deploy |
| GitHub Actions or another CI runner to Kubernetes | `piceli deploy --plan` / `piceli deploy --apply` | The CLI remains independent of the web server; the UI does not launch CI jobs |
| Composition UI installed by `piceli cluster init` | `piceli access ui --cluster MODULE:ATTR` (a port-forward and the launch token) | Environments, components, sources, workloads and logs, Sync |
| UI installed inside the cluster | `piceli ui cluster-serve` behind the configured TLS gateway | OIDC-scoped observation, reviewed manual delivery, cluster build Jobs and local-client access tickets |
| Direct application deployment to a machine without Kubernetes | No target provider exists yet | No deployment action is advertised |

For a saved local credential profile, use `piceli ui serve --profile NAME`
(list names with `piceli profiles --json`). The header shows the active
profile and, when several profiles exist, a picker. Switching stops the old
local listener and its session, then starts a fresh process with the selected
profile. Reopen the new launch address printed by that process. Profile names
and availability are shown; kubeconfig paths and contents are not sent to the
browser. An explicit `--kubeconfig` or `--context` cannot accompany
`--profile` on a new serve command.

A local UI process may itself run on a remote machine. Keep its listener on
loopback and use an SSH tunnel, for example
`ssh -L 8000:127.0.0.1:8000 my-host`, to open the printed launch URL from
your laptop. Actions and server-side forwards run on the remote machine; they
do not become laptop ports. A local Pipeline can target a remote Kubernetes
API through the explicit kubeconfig in its trusted definition. Never put a
kubeconfig, arbitrary Python module, build command or source path into a
browser request.

## The composition UI in the cluster

A composition's `Cluster` declares the UI with `ui=Ui(access="forward")`:

```python
from piceli.infra import Cluster, Controller, Node, Ui

my_cluster = Cluster(
    "my-cluster",
    api="https://192.0.2.10:6443",
    credentials="my-cluster",  # a `piceli login` profile
    nodes=[Node("node-a", arch="amd64", roles=["controller"])],
    controller=Controller(on="node-a", image="registry.example/piceli@sha256:…"),
    ui=Ui(access="forward"),  # optional: on="node-a", image="…@sha256:…"
)
```

`piceli cluster init infra.py:my_cluster` installs it in `piceli-system`, next
to the GitOps controller:

- one Deployment running `piceli ui forward-serve` from the Piceli image pinned
  by digest (`Ui.image`, else the controller's image), on `Ui.on` (else the
  controller's node). It listens on its pod's loopback only;
- a ClusterIP Service, the port-forward target. Nothing is exposed outside
  the cluster: no NodePort, no Ingress, and no OIDC in this mode;
- a service account that reads the controller's status ConfigMap and the
  environments' workloads, pods and logs (never their Secrets or ConfigMaps),
  and writes exactly two objects: the controller's request inbox
  `piceli-gitops-requests` (the **Sync** button) and the Secret
  `piceli-ui-launch`, where the UI puts a fresh launch token when it starts.

Open it from your laptop with the cluster's credentials profile:

```sh
piceli access ui --cluster infra.py:my_cluster   # or: --profile my-cluster
```

The command reads the launch token with the profile's explicit kubeconfig and
context (never the current context), forwards `127.0.0.1:8790` to the UI
Service, and prints one line, `Piceli UI: http://127.0.0.1:8790/?token=…`.
Open it once: the token becomes a session cookie and leaves the address bar.
The token appears nowhere else, not in the UI's logs. Ctrl-C stops the
forward. When port 8790 is taken, `access-port-conflict` names its owner.
When the UI pod restarts it writes a new token: run `piceli access ui` again.
`--json` prints `started` (with the URL), `status` and `stopped` lines.

```{image} _static/ui/composition-environments.png
:alt: The composition Environments page: each environment's revision per source, health, state, last sync and a Sync button
:width: 720px
```

The views, like Argo CD's applications, read the controller's published status
(`piceli.gitops-status.v1`) every ten seconds:

- **Environments**: each environment with its revision per source
  (`source commit`, shortened), health, state, last sync and component
  states, and a **Sync** button.
- **An environment**: its components with source, commit, image digest,
  state (`synced`, `building`, `rolling`, `failed`, `unchanged`) and health,
  a **Sync** button per component, and a link to the namespace's workloads,
  pods and current or previous logs.
- **Sources**: each repository's URL (without any credential), the commit of
  every ref the controller follows, and its last poll.

**Cluster** shows declared node architecture and roles, live readiness, the
in-cluster registry's pod and claim status, mirror state on each node, and the
controller and UI Deployment health. A k3s mirror that needs a service restart
is shown separately from a ready mirror. Storage use is shown when kubelet
volume statistics are readable; otherwise it says “Not reported.” The page
returns only a small status projection: it does not expose the declaration's
credential reference, Git Secret, kubeconfig or service-account token.

On an environment detail header, **Approve** reviews the current pending plan
hash, namespace, revisions and components before requiring a second explicit
confirmation. **Promote** chooses a branch and exact commit already published
by the controller, then requires confirmation. The server rereads status
immediately before either request, so a changed hash or ref is refused. A
promotion is a request to the controller; its declared follow policy still
decides whether to accept it. An environment stopped by the idle policy shows
“Idle-stopped since” and **Wake**; Wake confirms a sync request, which the
controller processes at its next poll. The next push can wake it as well.

The composition controller currently publishes environment names but not its
per-environment `Promote()` policy, and it publishes only followed Git refs.
The UI therefore keeps Promote disabled when it cannot establish the policy
and a published branch head from status. The controller must publish those
facts before a promotion from a Promote-only source can be offered. Also, the
forward UI install currently grants reads of controller status and workloads,
but not `piceli-cluster` or Nodes. Cluster shows the facts it can read and
marks unavailable node readiness and storage use as unknown. The install
needs scoped reads of `piceli-cluster`, Nodes and kubelet volume statistics to
show all Cluster fields. These permissions are owned by the cluster UI install
work package.

```{image} _static/ui/named-environment-approve.png
:alt: Approval review of an exact pending plan hash in a named environment
:width: 720px
```

```{image} _static/ui/named-environment-promote.png
:alt: Promotion review of a published branch and exact commit
:width: 720px
```

```{image} _static/ui/idle-stopped-environment.png
:alt: An idle-stopped environment with its stopped-since time and Wake review
:width: 720px
```

Short recordings from the same fake-API journey show the
[Cluster status](_static/ui/cluster-overview.webp),
[Promote and Approve](_static/ui/named-environment-actions.webp), and
[idle-stop Wake](_static/ui/idle-stopped-environment.webp) interactions.

**Sync** writes the same request as `piceli gitops sync ENV [--component
NAME]`; the controller handles it on its next poll, under the environment's
own rules (an approval it requires is still required: run `piceli gitops
approve`). Every refusal has a message and a code (`piceli explain CODE`):
`ui-controller-absent` before the controller publishes its first status,
`ui-sync-target-unknown` for an environment or component the status no longer
lists, `ui-sync-unavailable` when the request inbox cannot be written; and for
`piceli access ui`, `access-ui-not-installed`, `access-ui-not-ready` (the pod is
still starting), `access-ui-forbidden` and `access-ui-unreachable`.

```{image} _static/ui/composition-environment.png
:alt: One environment: components with source, commit, digest, state and health, each with a Sync button
:width: 720px
```

## Pipeline deployment from the browser

The UI owner starts `piceli ui serve --pipeline module:attribute` with a
trusted `Pipeline` definition. The server uses the existing `PipelineRunner`,
state lock, journal and portable `piceli.pipeline.planfile` document. The
browser shows the target, exact combined hash and planned stages: inputs,
build, delivery, pre-rollout checks, restore point, release plan, apply and
post-deploy checks, when declared. The operator must confirm the reviewed
hash before execution. A changed or expired plan is refused.

When the release uses built images that are not yet delivered, the first
approval covers only the build and delivery stages. The runner stops there.
The server then creates a real, materialized release plan and waits in
`awaiting-review`. The browser presents its new hash, pre-rollout checks and
restore-point effects. A second explicit approval is required before rollout.
Closing the browser does not grant that approval. Reload the run URL to resume
review. Restarting the service marks a queued or running browser operation
interrupted; inspect the Piceli deploy journal and prepare a new plan. A plan
waiting for its second approval remains waiting after restart.

Pre-rollout checks declared by a release definition are included in its
reviewed plan and run before the apply stage. Pipeline restore points and
two-stage image delivery remain available to local Pipeline sessions.

The Pipeline adapter runs on the local UI host and uses its configured builder
and delivery route. An installed UI offers a separate cluster-build action:
after approval of the exact build plan hash, its dispatcher calls Piceli's
existing Kubernetes build Job path. The builder Job uses its own scoped
ServiceAccount, Git Secret, cache PVC and node registry. The UI Pod receives no
registry or cluster-admin credential.

## Branch environments and GitOps

If the trusted Pipeline declares `EnvConfig`, **Environments** lists branch,
namespace, commit, state, health, age and workloads using `list_envs`. Each
running branch registers an explicit local observation scope: open its
Resources page to inspect workloads, current or previous container logs, and
access forwards. Environment up, down and seed requests show the core plan
first, then require the exact `env_hash`. Seed asks for an explicit source
environment. Piceli replans before the change; a changed hash is refused.

Configure `--gitops-namespace NAME` to expose the existing GitOps controller.
**GitOps** displays controller and branch status, pending plan hashes, and
requests approval of a pending hash or promotion of `branch@sha` to main. It
submits a request to the installed controller; the browser does not reconcile
Git itself. In OIDC-scoped mode, entries outside the granted namespace are
hidden and cannot be approved or promoted. The cluster service account also
needs the corresponding ConfigMap permission in the explicitly named
controller namespace. An absent controller is displayed as unconfigured.

## Local release definition and observation

`piceli ui serve --definition PATH` registers the definition's explicit
target. To allow source evaluation, also configure `--source-root`, one or
more `--source-file` entries, the pinned `--renderer-image`, and
`--renderer-platform`; see `piceli ui serve --help` for the remaining isolated
renderer options. The browser reviews a source-execution preview before
running the isolated renderer, then separately approves the produced exact
release plan. Operations are journaled and continue after a browser disconnect.
The Resources view shows observed objects, scoped logs and owned port forwards.
The UI never treats a catalog selection as a deployment rollback.

With only `--kubeconfig`, `--context` and `--namespace`, the UI is inventory
only. An access endpoint labelled **server** is on the UI host, including when
that host is remote. Do not infer a laptop endpoint from it.

## In-cluster service

The [installation guide](ui_cluster_install.md) describes the single-replica
Deployment, TLS gateway, OIDC configuration, namespace grants and private PVC.
The default installed profile is scoped observation. An operator may configure
a trusted release definition, immutable renderer image, named deployment
resources and deploy subjects. A granted OIDC user first approves the source
evaluation digest, reviews the resulting release plan, then approves that
plan's exact digest. The dispatcher applies that plan and writes operation and
release state on the private PVC; an interrupted operation is visible after a
Pod restart. Back up the PVC's control directory with the stopped-server
commands below. The installed service does not run arbitrary browser-provided
Pipelines.

Cluster login requires a signed ID token with the configured issuer, exact
client audience, nonce and expiry. Only a principal with a configured grant
gets a bounded session. Each API request and live stream rechecks its scope;
logout is a CSRF-protected POST. The browser never receives the projected
Kubernetes service-account token. A renderer Job has no deployment token or
host mount and is selected by a deny-egress policy. Its trusted entrypoint
waits for policy enforcement before importing approved source; verify the
installed CNI's enforcement before enabling source evaluation.

For remote local-client access in cluster mode, the server
issues a pending one-time ticket. `piceli ui connect` uses an explicit local
kubeconfig and context, starts a supervised loopback forward, probes it, and
only then reports **ready**. Stopping, expiry or lost ownership removes that
readiness. A pending server ticket never claims a laptop port exists.

## Back up or restore UI control state

Stop the single UI server before backing up its private control directory:

```sh
piceli ui backup --control-dir /var/lib/piceli --output /safe/ui-state.tar.gz
piceli ui restore --archive /safe/ui-state.tar.gz --destination /new/empty/control
```

The backup command refuses a held dispatcher lock. It includes the operation
store, evaluation evidence and release journal, verifies file digests and
writes a mode-`0600` archive. Restore validates every member before moving it
into an empty directory with private modes. Copy or restore the archive before
starting a new single replica; keep the archive as private as deployment
credentials and source evidence. Pipeline runs keep their own state directory,
which must also be backed up if it lives outside this control directory.
