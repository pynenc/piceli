# Model List Command

The `model list` command in Piceli CLI is designed to list Kubernetes objects based on the command options. This command plays a crucial role in visualizing the Kubernetes object model that will be utilized in deployment and other operations.

## Command Usage

```bash
piceli model list --help

Usage: piceli model list [OPTIONS]

 Lists Kubernetes objects based on the command options.
 Note: The command options are shared among commands and should be specified at the
 root level. The model listed in this command will be the same as those used in other
 commands, such as deploy.

╭─ Options ───────────────────────────────────────────────────────────────────────────╮
│ --help          Show this message and exit.                                         │
╰─────────────────────────────────────────────────────────────────────────────────────╯

```

## Options

- `--help`: Show this message and exit.

## Execution Example

The following is an example execution of the `model list` command, showcasing how it outputs a list of Kubernetes objects based on the provided path:

```bash
piceli --folder-path=/folder/to/resources model list

╭───────────────────────────────── Command Execution ─────────────────────────────────╮
│ Running command: List Kubernetes Objects Model                                      │
╰─────────────────────────────────────────────────────────────────────────────────────╯
╭───────────────────────── Context Options ─────────────────────────╮
│ Namespace: default                                                │
│ Module Name: Not specified                                        │
│ Module Path: Not specified                                        │
│ Folder Path: /folder/to/resources                                 │
│ Include Sub-elements: True                                        │
╰───────────────────────────────────────────────────────────────────╯
┏━━━━━━━━━━━━━━━━━━┳━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Name             ┃ Kind ┃ Namespace ┃ Origin                                        ┃
┡━━━━━━━━━━━━━━━━━━╇━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ tasker-scheduler │ Job  │ Default   │ OriginYAML(path='/folder/to/resources         │
│ task-scheduler   │ Job  │ Default   │ OriginYAML(path='/folder/to/resources         │
│ task-worker      │ Job  │ Default   │ OriginYAML(path='/folder/to/resources         │
│ other-job        │ Job  │ Default   │ OriginYAML(path='/folder/to/resources         │
└──────────────────┴──────┴───────────┴───────────────────────────────────────────────┘
```

This output provides a clear and concise list of the Kubernetes objects that are recognized by the Piceli CLI based on the specified folder path. It includes details such as the object's name, kind, namespace, and the origin file, making it easier to understand and manage the object model within your Kubernetes environment.
