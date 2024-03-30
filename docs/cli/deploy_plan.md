# Deploy Plan Command

The `deploy plan` command in Piceli CLI generates a deployment plan for the Kubernetes object model. This command provides a structured overview of the deployment steps required for your Kubernetes objects.

## Command Usage

```bash
piceli deploy plan --help

 Usage: piceli deploy plan [OPTIONS]

 Deployment plan for the kubernetes object model.
 Note: The command options are shared among commands and should be specified at the
 root level.

╭─ Options ───────────────────────────────────────────────────────────────────────────╮
│ --validate  -v        Validate the deployment graph for cycles and errors before    │
│                       showing the plan.                                             │
│ --help                Show this message and exit.                                   │
╰─────────────────────────────────────────────────────────────────────────────────────╯
```

## Options

- `--validate` (`-v`): Validate the deployment graph for cycles and errors before showing the plan.
- `--help`: Show this message and exit.

## Execution Example

Below is an example of how to generate a deployment plan using the Piceli CLI:

```bash
PICELI__FOLDER_PATH=/folder/to/resources/tmp_cli PICELI__NAMESPACE=test-run piceli deploy plan
╭───────────────────────────────── Command Execution ─────────────────────────────────╮
│ Running command: Deployment Plan                                                    │
╰─────────────────────────────────────────────────────────────────────────────────────╯
╭────────────────────────────── Context Options ───────────────────────────────╮
│ Namespace: test-run                                                          │
│ Module Name: Not specified                                                   │
│ Module Path: Not specified                                                   │
│ Folder Path: /folder/to/resources/tmp_cli │
│ Include Sub-elements: True                                                   │
╰──────────────────────────────────────────────────────────────────────────────╯
Kubernetes Deployment Plan
┣━━ Step 1:
┃   ┣━━ Role example-role in namespace default
┃   ┣━━ ServiceAccount example-serviceaccount in namespace default
┃   ┗━━ StorageClass resizable in namespace default
┣━━ Step 2:
┃   ┗━━ RoleBinding example-rolebinding in namespace default
┣━━ Step 3:
┃   ┣━━ Secret example-secret in namespace default
┃   ┗━━ ConfigMap example-configmap in namespace default
┣━━ Step 4:
┃   ┗━━ PersistentVolumeClaim example-persistentvolumeclaim in namespace default
┣━━ Step 5:
┃   ┗━━ Deployment example-deployment in namespace default
┣━━ Step 6:
┃   ┗━━ Service example-service in namespace default
┣━━ Step 7:
┃   ┗━━ CronJob example-cronjob in namespace default
┗━━ Step 8:
    ┗━━ HorizontalPodAutoscaler example-horizontalpodautoscaler in namespace default
```
