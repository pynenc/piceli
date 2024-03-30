# Piceli Command Line Interface (CLI) Guide

The Piceli CLI offers a suite of commands to manage and deploy Kubernetes resources efficiently. This guide provides an overview of the available commands and examples of how to use them for various operations.

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

### Subcommands

- **Detail**: Analyzes the required changes to deploy the specified Kubernetes object model. `{subdoc}`./deploy_detail`
- **Plan**: Generates a deployment plan for the Kubernetes object model. `{subdoc}`./deploy_plan`
- **Run**: Deploys the Kubernetes Object Model to the current cluster. `{subdoc}`./deploy_run`

For detailed information about each subcommand, refer to the respective documentation pages.

## Model Command

The `model` command is related to actions involving the Kubernetes object model that the CLI considers, such as listing the Kubernetes objects.

```bash
python -m piceli model --help

Usage: python -m piceli model [OPTIONS] COMMAND [ARGS]...
```

### Subcommands

- **List**: Lists Kubernetes objects based on the command options. `{subdoc}`./model_list`

For more information on the `list` command, visit the documentation page linked above.

By utilizing the `deploy` and `model` commands, users can effectively manage and deploy their Kubernetes object models, ensuring efficient and streamlined operations within their Kubernetes environments.
