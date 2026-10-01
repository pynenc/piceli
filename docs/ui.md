# Piceli web application

The optional `piceli[ui]` package serves a bundled React application. It does
not need Node at runtime and does not read an ambient kubeconfig or context.
The UI runs in one of two modes: a local, single-user process with an explicit
target, or an OIDC-authenticated service installed inside a Kubernetes cluster.
Each mode offers only the actions configured by its operator.

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

For service and browser acceptance with that fake API, run `make test-ui-fake`.
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
| UI installed inside the cluster | `piceli ui cluster-serve` behind the configured TLS gateway | OIDC-scoped observation, reviewed manual delivery, cluster build Jobs and local-client access tickets |
| Direct application deployment to a machine without Kubernetes | No target provider exists yet | No deployment action is advertised |

A local UI process may itself run on a remote machine. Keep its listener on
loopback and use an SSH tunnel, for example
`ssh -L 8000:127.0.0.1:8000 my-host`, to open the printed launch URL from
your laptop. Actions and server-side forwards run on the remote machine; they
do not become laptop ports. A local Pipeline can target a remote Kubernetes
API through the explicit kubeconfig in its trusted definition. Never put a
kubeconfig, arbitrary Python module, build command or source path into a
browser request.

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
