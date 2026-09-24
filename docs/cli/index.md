# Piceli Command Line Interface (CLI) Guide

```{admonition} Maturity: preview
:class: note

Every command here uses one engine: live discovery, a pure plan, server-side
apply with preconditions, and a durable journal. Commands only reach a cluster
through an explicit kubeconfig file and context. See the {doc}`../roadmap` for
each feature's maturity.
```

The `piceli` command (also available as `python -m piceli`) has six command groups, the `render`, `deploy`, `status` and `access` commands and two contract commands:

| Group | Purpose | Cluster access | Output |
| --- | --- | --- | --- |
| `render` | Print the manifests of a typed app or composition (preview). See {doc}`../typed_apps` | None | YAML or JSON |
| `deploy` | Run the whole pipeline for a typed app: verify inputs, build, deliver, plan, apply and run checks, skipping unchanged stages. See {doc}`../deploy` | Explicit kubeconfig file + context from the target | JSON + summary on stderr |
| `release` | Plan, apply, roll back, resume and stop releases from a `release.toml` spec; `release check` runs the spec's `[[checks]]` ({doc}`../checks`); `release secret show` inspects secret values (owner, `--reveal`). See {doc}`../release_cli` and {doc}`../secrets` | Explicit kubeconfig file + context from the spec | JSON + summary on stderr |
| `observe` | Reconcile a session archive, logs, port forwards, local UI | Explicit `--kubeconfig/--context` | JSON |
| `operator` | Inventory, releases, approvals, backups, local UI | Explicit `--kubeconfig` | JSON |
| `import` | Generate a typed app module from live objects (`import live`) or manifest files (`import yaml`) (preview). See {doc}`../migrate_from_kubectl` | Explicit `--kubeconfig/--context` for `live` (reads only); none for `yaml` | Python module + JSON |
| `artifacts` | Deterministic OCI builds, image delivery and explicit local import | None, or an explicit target for `deliver` | JSON |
| `inputs` | Record and verify the git identity of build sources | None (local git only) | JSON |
| `status` | Whether the app is up and how to reach it: release, image digests, health, URLs (preview). See {doc}`../access` | Explicit kubeconfig file + context from the target (reads only) | Text, or JSON with `--json` |
| `access` | Forward the ports the model declares to `127.0.0.1` and keep them healthy (preview). See {doc}`../access` | Explicit kubeconfig file + context from the target (port forwards) | Text, or JSON lines with `--json` |
| `explain` | Explain an error code: cause, fix, whether a retry can succeed | None | Text, or JSON with `--json` |
| `help-json` | The whole command tree with options, side effects and approval rules | None | JSON |

`piceli deploy` runs the whole pipeline (verify inputs, build, deliver, plan,
apply, checks) as one resumable command; see {doc}`../deploy`.

Every command, option and contract is listed in {doc}`../reference/cli`,
generated from `piceli help-json`. Refusal codes are explained in
{doc}`../reference/errors`. Agents should start with {doc}`../agents`.

There are no global options: each command takes its own explicit options.

```{note}
Upgrading from 0.3.x: the `model list` and `deploy run/plan/detail` commands
and the global `--namespace`, `--module-name`, `--module-path`,
`--folder-path` and `--sub-elements` options (`PICELI__*` environment
variables, `[tool.piceli]` in `pyproject.toml`) were removed in 0.4.0. Use
`piceli render` to see what a model contains, `piceli import` to turn existing
objects into a typed app, and `piceli release` or `piceli deploy` to apply it.
```

## Observe Command

Read-only reconciliation of a {doc}`deployment session <../deployment_planning>`
archive with a live namespace, plus bounded logs and saved loopback port forwards.
See {doc}`../operations_lens` for examples.

| Subcommand | Description |
| --- | --- |
| `status` | Print declared resources, live state, and objects absent from the archive. |
| `logs-command` / `logs-run` | Print, or run, a bounded workload-log request. |
| `forward-save` / `forward-list` | Save or list a user's loopback port-forward preferences. |
| `forward-command` / `forward-run` | Print, or run, one saved loopback-only port forward. |
| `forwards apply` / `forwards status` | Start and health-supervise every forward in an access profile, or probe them once. |
| `serve` | Open the local operations dashboard; `--start-shortcuts` starts and supervises the configured forwards. |

## Operator Command

Local release and operations management. See {doc}`../operator_workflow`.

| Subcommand | Description |
| --- | --- |
| `status` | Print the classified inventory (managed, unmanaged, unknown) and releases. |
| `promote` | Promote an existing built digest to a new release name without rebuilding. |
| `approve` | Record an explicit operator approval for a pull request rollout. |
| `backup` / `restore` | Create or restore a verified, mode-restricted backup of operator state. |
| `serve` | Launch the operator dashboard and REST API on loopback. |

## Artifacts Command

Deterministic OCI image layouts from pinned sources. Nothing is pushed. See
{doc}`../artifact_delivery`.

| Subcommand | Description |
| --- | --- |
| `pin` | Capture a source pin for an explicitly public file. |
| `preview` / `build` | Preview a build plan, or build a deterministic OCI layout. |
| `inspect` | Validate and describe an OCI layout. |
| `build-spec preview` / `build-spec run` | Containerized build from a declarative `build.toml`, with a pinned builder and a `piceli.build-receipt.v1` receipt. See {doc}`../containerized_builds`. |
| `import-local` | Import an approved layout into a local Docker engine (no tag, no push). |
| `deliver` | Deliver an image approved by config digest: push to a registry with `oci://` (default; only missing blobs are uploaded, optionally through a port-forward), or import it straight into a node's containerd over `ssh://` / `docker://`. See {doc}`../node_delivery`. |
| `preview-command` / `execute-command` | Preview or run a pinned external build tool under an explicit grant. |

```{note}
`piceli artifacts` does not appear in `piceli --help` yet. Run
`piceli artifacts --help` directly.
```
