# Deploy Detail Command

The `deploy detail` command in Piceli CLI is designed to analyze the required changes to deploy the specified Kubernetes object model. This detailed analysis helps in understanding the necessary actions before actual deployment.

## Command Usage

```bash
piceli deploy detail --help

Usage: piceli deploy detail [OPTIONS]

 Analize the required changes to deploy the specified kubernetes object model
 Note: The command options are shared among commands and should be specified at the
 root level.

╭─ Options ───────────────────────────────────────────────────────────────────────────╮
│ --hide-no-action  -hna        Hide the comparison details when no action is needed. │
│ --help                        Show this message and exit.                           │
╰─────────────────────────────────────────────────────────────────────────────────────╯

```

## Options

- `--hide-no-action` (`-hna`): Hide the comparison details when no action is needed.
- `--help`: Show this message and exit.

## Execution Example

The following example showcases the command execution of `deploy detail`, providing a detailed analysis of the required changes for deployment:

```bash
PICELI__FOLDER_PATH=/folder/to/resources/tmp_cli PICELI__NAMESPACE=test-run piceli deploy detail
╭───────────────────────────────── Command Execution ─────────────────────────────────╮
│ Running command: Deployment Detailed Analysis                                       │
╰─────────────────────────────────────────────────────────────────────────────────────╯
╭────────────────────────────── Context Options ───────────────────────────────╮
│ Namespace: test-run                                                          │
│ Module Name: Not specified                                                   │
│ Module Path: Not specified                                                   │
│ Folder Path: /folder/to/resources/tmp_cli │
│ Include Sub-elements: True                                                   │
╰──────────────────────────────────────────────────────────────────────────────╯
                                New Kubernetes Objects
┏━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━┓
┃ Kind                    ┃ Name                        ┃ Version ┃ Group             ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━┩
│ Role                    │ example-role                │ v1      │ RbacAuthorization │
│ ServiceAccount          │ example-serviceaccount      │ v1      │ Core              │
│ RoleBinding             │ example-rolebinding         │ v1      │ RbacAuthorization │
│ Secret                  │ example-secret              │ v1      │ Core              │
│ ConfigMap               │ example-configmap           │ v1      │ Core              │
│ PersistentVolumeClaim   │ example-persistentvolumecl… │ v1      │ Core              │
│ Deployment              │ example-deployment          │ v1      │ Apps              │
│ Service                 │ example-service             │ v1      │ Core              │
│ CronJob                 │ example-cronjob             │ v1      │ Batch             │
│ HorizontalPodAutoscaler │ example-horizontalpodautos… │ v2      │ Autoscaling       │
└─────────────────────────┴─────────────────────────────┴─────────┴───────────────────┘
     Kubernetes Objects Deployment Summary
┏━━━━━━━━━━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━┓
┃ Kind         ┃ Name      ┃ Update Action    ┃
┡━━━━━━━━━━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━┩
│ StorageClass │ resizable │ No action needed │
└──────────────┴───────────┴──────────────────┘
─────────────────────  StorageClass resizable - No action needed ──────────────────────
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ Existing Object                          ┃ Desired Object                           ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ {                                        │ {                                        │
│   "allowVolumeExpansion": true,          │   "allowVolumeExpansion": true,          │
│   "apiVersion": "storage.k8s.io/v1",     │   "apiVersion": "storage.k8s.io/v1",     │
│   "kind": "StorageClass",                │   "kind": "StorageClass",                │
│   "metadata": {                          │   "metadata": {                          │
│     "creationTimestamp":                 │     "name": "resizable"                  │
│ "2024-03-06T18:01:30+00:00",             │   },                                     │
│     "managedFields": [                   │   "provisioner":                         │
│       {                                  │ "k8s.io/minikube-hostpath"               │
│         "apiVersion":                    │ }                                        │
│ "storage.k8s.io/v1",                     │                                          │
│         "fieldsType": "FieldsV1",        │                                          │
│         "fieldsV1": {                    │                                          │
│           "f:allowVolumeExpansion": {},  │                                          │
│           "f:provisioner": {},           │                                          │
│           "f:reclaimPolicy": {},         │                                          │
│           "f:volumeBindingMode": {}      │                                          │
│         },                               │                                          │
│         "manager": "OpenAPI-Generator",  │                                          │
│         "operation": "Update",           │                                          │
│         "time":                          │                                          │
│ "2024-03-06T18:01:30+00:00"              │                                          │
│       }                                  │                                          │
│     ],                                   │                                          │
│     "name": "resizable",                 │                                          │
│     "resourceVersion": "176636",         │                                          │
│     "uid":                               │                                          │
│ "9ef7f7b7-8733-40b5-9e56-ee2f555823a5"   │                                          │
│   },                                     │                                          │
│   "provisioner":                         │                                          │
│ "k8s.io/minikube-hostpath",              │                                          │
│   "reclaimPolicy": "Delete",             │                                          │
│   "volumeBindingMode": "Immediate"       │                                          │
│ }                                        │                                          │
└──────────────────────────────────────────┴──────────────────────────────────────────┘
                                  Differences Summary
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┓
┃ Path                       ┃ Type     ┃ Existing                          ┃ Desired ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━┩
│ metadata,creationTimestamp │ Ignored  │ 2024-03-06T18:01:30+00:00         │ None    │
├────────────────────────────┼──────────┼───────────────────────────────────┼─────────┤
│ metadata,managedFields     │ Ignored  │ [                                 │ None    │
│                            │          │   {                               │         │
│                            │          │     "apiVersion":                 │         │
│                            │          │ "storage.k8s.io/v1",              │         │
│                            │          │     "fieldsType": "FieldsV1",     │         │
│                            │          │     "fieldsV1": {                 │         │
│                            │          │       "f:allowVolumeExpansion":   │         │
│                            │          │ {},                               │         │
│                            │          │       "f:provisioner": {},        │         │
│                            │          │       "f:reclaimPolicy": {},      │         │
│                            │          │       "f:volumeBindingMode": {}   │         │
│                            │          │     },                            │         │
│                            │          │     "manager":                    │         │
│                            │          │ "OpenAPI-Generator",              │         │
│                            │          │     "operation": "Update",        │         │
│                            │          │     "time":                       │         │
│                            │          │ "2024-03-06T18:01:30+00:00"       │         │
│                            │          │   }                               │         │
│                            │          │ ]                                 │         │
├────────────────────────────┼──────────┼───────────────────────────────────┼─────────┤
│ metadata,uid               │ Ignored  │ 9ef7f7b7-8733-40b5-9e56-ee2f5558… │ None    │
├────────────────────────────┼──────────┼───────────────────────────────────┼─────────┤
│ metadata,resourceVersion   │ Ignored  │ 176636                            │ None    │
├────────────────────────────┼──────────┼───────────────────────────────────┼─────────┤
│ volumeBindingMode          │ Defaults │ Immediate                         │ None    │
├────────────────────────────┼──────────┼───────────────────────────────────┼─────────┤
│ reclaimPolicy              │ Defaults │ Delete                            │ None    │
└────────────────────────────┴──────────┴───────────────────────────────────┴─────────┘
```
