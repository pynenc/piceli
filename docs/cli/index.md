# Piceli Command Line Interface (CLI) Guide

The `piceli` command (also available as `python -m piceli`) has seven command groups:

| Group | Purpose | Cluster access | Output |
| --- | --- | --- | --- |
| `model` | Show what Piceli loaded from your modules and folders | None | Rich tables |
| `deploy` | Plan, diff and apply the model (CLI engine) | Current kubeconfig context | Rich tables |
| `observe` | Reconcile a session archive, logs, port forwards, local UI | Explicit `--kubeconfig/--context` | JSON |
| `operator` | Inventory, releases, approvals, backups, local UI | Explicit `--kubeconfig` | JSON |
| `artifacts` | Deterministic OCI builds and explicit local import | None (local tools only) | JSON |
| `inputs` | Record and verify the git identity of build sources | None (local git only) | JSON |
| `release` | Plan, apply, roll back, resume and stop releases from a `release.toml` spec (recoverable engine); `release secret show` inspects secret values (owner, `--reveal`). See {doc}`../release_cli` and {doc}`../secrets` | Explicit kubeconfig file + context from the spec | JSON + summary on stderr |

The global options below (`--namespace`, `--module-*`, `--folder-path`) apply to
`model` and `deploy`. The `observe`, `operator` and `artifacts` groups take their
own explicit options.

```{toctree}
:hidden:
:maxdepth: 2
:caption: CLI Commands

./model_list
./deploy_detail
./deploy_plan
./deploy_run
```

## Global Options

Piceli CLI supports several global options that can be used across all commands. These options allow you to specify common settings like namespace, path to Kubernetes object specifications, and more.

```bash
python -m piceli --help

Usage: python -m piceli [OPTIONS] COMMAND [ARGS]...

Piceli kubernetes commands

╭─ Options ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╮
│ --namespace           -n       TEXT  Namespace on the kubernetes cluster [env var: PICELI__NAMESPACE] [default: default]                                               │
│ --module-name         -mn      TEXT  Folder containing Kubernetes objects specifications. [env var: PICELI__MODULE_NAME]                                               │
│ --module-path         -mp      TEXT  Folder containing Kubernetes objects specifications. [env var: PICELI__MODULE_PATH]                                               │
│ --folder-path         -fp      TEXT  Folder containing Kubernetes objects specifications. [env var: PICELI__FOLDER_PATH]                                               │
│ --sub-elements        -se            Should load kubernetes objects from sub folders/modules [env var: PICELI__SUB_ELEMENTS] [default: True]                           │
│ --install-completion                 Install completion for the current shell.                                                                                         │
│ --show-completion                    Show completion for the current shell, to copy it or customize the installation.                                                  │
│ --help                               Show this message and exit.                                                                                                       │
╰────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╮
│ deploy                                                                                                                                                                 │
│ model                                                                                                                                                                  │
╰────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯
```

## Deploy Command

The `deploy` command is utilized for actions related to the deployment process, such as planning, running, and detailing deployments.

```bash
python -m piceli deploy --help

Usage: python -m piceli deploy [OPTIONS] COMMAND [ARGS]...
```

### Deploy Subcommands

- **Detail**: Analyzes the required changes to deploy the specified Kubernetes object model. {doc}`deploy_detail`
- **Plan**: Generates a deployment plan for the Kubernetes object model. {doc}`deploy_plan`
- **Run**: Deploys the Kubernetes Object Model to the current cluster. {doc}`deploy_run`

For detailed information about each subcommand, refer to the respective documentation pages.

## Model Command

The `model` command is related to actions involving the Kubernetes object model that the CLI considers, such as listing the Kubernetes objects.

```bash
python -m piceli model --help

Usage: python -m piceli model [OPTIONS] COMMAND [ARGS]...
```

### Model Subcommands

- **List**: Lists Kubernetes objects based on the command options. {doc}`model_list`

For more information on the `list` command, visit the documentation page linked above.

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
