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
| UI installed inside the cluster | `piceli ui cluster-serve` behind the configured TLS gateway | OIDC-scoped observation; manual delivery and remote local-client access remain experimental |
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

Pre-rollout checks and restore points are Pipeline features. The separate
release-definition UI evaluator still refuses a definition with pre-rollout
checks (`ui-prerollout-unsupported`), so it cannot silently skip them. Use a
configured Pipeline for that application.

The Pipeline adapter runs on the local UI host. It uses that host's configured
builder and delivery route. It is not an in-cluster credential-free builder;
that server path is still experimental.

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

## In-cluster service and current gate

The [installation guide](ui_cluster_install.md) describes the single-replica
Deployment, TLS gateway, OIDC configuration, namespace grants and private PVC.
Read-only scoped observation is the supported installed profile. Manual
source evaluation/deployment and local-client access still require
`--experimental` or `PICELI_UI_EXPERIMENTAL=1`. The installed manual path has
not passed the full clean-cluster OIDC deploy, CNI egress-enforcement and
credential-free image-build gate. Use this opt-in only in disposable clusters.
The UI does not advertise Pipeline execution from the installed service.

Cluster login requires a signed ID token with the configured issuer, exact
client audience, nonce and expiry. Only a principal with a configured grant
gets a bounded session. Each API request and live stream rechecks its scope;
logout is a CSRF-protected POST. The browser never receives the projected
Kubernetes service-account token. A renderer Job has no deployment token or
host mount and is selected by a deny-egress policy, whose enforcement must be
verified on the installed CNI.

For remote local-client access in experimental cluster mode, the server
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
