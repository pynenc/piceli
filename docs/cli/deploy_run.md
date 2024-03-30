# Deploy Run Command

The `deploy run` command in Piceli CLI is designed to deploy your Kubernetes Object Model to the current cluster. This command automates the deployment process, managing the creation and configuration of Kubernetes resources as defined in your model.

## Command Usage

```bash
piceli deploy run --help

 Usage: piceli deploy run [OPTIONS]

 Deploy Kubernetes Object Model to the current cluster.

╭─ Options ───────────────────────────────────────────────────────────────────────────╮
│ --create-namespace  -c        Create the namespace if it does not exist.            │
│                               [default: True]                                       │
│ --help                        Show this message and exit.                           │
╰─────────────────────────────────────────────────────────────────────────────────────╯
```

```bash
PICELI__FOLDER_PATH=/folder/to/resources/tmp_cli PICELI__NAMESPACE=test-run piceli deploy run
╭───────────────────────────────── Command Execution ─────────────────────────────────╮
│ Running command: Running Deployment                                                 │
╰─────────────────────────────────────────────────────────────────────────────────────╯
Namespace 'test-run' created successfully.
╭───────────────────────────── Execution Status: PENDING ─────────────────────────────╮
│ Starting the deployment process.                                                    │
╰─────────────────────────────────────────────────────────────────────────────────────╯
──────────────────────────────────  Starting Level 0 ──────────────────────────────────
                            Applying Level 0
 ───────────────────────────────────────────────────────────────────────
  Name                     Kind             Group               Version
 ───────────────────────────────────────────────────────────────────────
  example-role             Role             RbacAuthorization   v1
  example-serviceaccount   ServiceAccount   Core                v1
  resizable                StorageClass     Storage             v1
 ───────────────────────────────────────────────────────────────────────
╭───────────────────────────── Execution Status: PENDING ─────────────────────────────╮
│ Starting the deployment process.                                                    │
╰─────────────────────────────────────────────────────────────────────────────────────╯
──────────────────────────────────  Starting Level 0 ──────────────────────────────────
                            Applying Level 0
 ───────────────────────────────────────────────────────────────────────
  Name                     Kind             Group               Version
 ───────────────────────────────────────────────────────────────────────
  example-role             Role             RbacAuthorization   v1
  example-serviceaccount   ServiceAccount   Core                v1
  resizable                StorageClass     Storage             v1
 ───────────────────────────────────────────────────────────────────────
Role example-role - Applying object
Role example-role - New object, will be created.
Role example-role - Application completed.
ServiceAccount example-serviceaccount - Applying object
ServiceAccount example-serviceaccount - New object, will be created.
ServiceAccount example-serviceaccount - Application completed.
StorageClass resizable - Applying object
StorageClass resizable - Comparing existing object...
Existing object matches the desired spec; no action needed.
StorageClass resizable - Application completed.
                   Completed Level 0
 ─────────────────────────────────────────────────────
  Name                     Kind             Status
 ─────────────────────────────────────────────────────
  example-role             Role             Completed
  example-serviceaccount   ServiceAccount   Completed
  resizable                StorageClass     Completed
 ─────────────────────────────────────────────────────
────────────────────────────  Level Completed Successfully ────────────────────────────
──────────────────────────────────  Starting Level 1 ──────────────────────────────────
                         Applying Level 1
 ─────────────────────────────────────────────────────────────────
  Name                  Kind          Group               Version
 ─────────────────────────────────────────────────────────────────
  example-rolebinding   RoleBinding   RbacAuthorization   v1
 ─────────────────────────────────────────────────────────────────
RoleBinding example-rolebinding - Applying object
RoleBinding example-rolebinding - New object, will be created.
RoleBinding example-rolebinding - Application completed.
                Completed Level 1
 ───────────────────────────────────────────────
  Name                  Kind          Status
 ───────────────────────────────────────────────
  example-rolebinding   RoleBinding   Completed
 ───────────────────────────────────────────────
────────────────────────────  Level Completed Successfully ────────────────────────────
──────────────────────────────────  Starting Level 2 ──────────────────────────────────
                 Applying Level 2
 ─────────────────────────────────────────────────
  Name                Kind        Group   Version
 ─────────────────────────────────────────────────
  example-secret      Secret      Core    v1
  example-configmap   ConfigMap   Core    v1
 ─────────────────────────────────────────────────
Secret example-secret - Applying object
ConfigMap example-configmap - Applying object
Secret example-secret - New object, will be created.
Secret example-secret - Application completed.
ConfigMap example-configmap - New object, will be created.
ConfigMap example-configmap - Application completed.
              Completed Level 2
 ───────────────────────────────────────────
  Name                Kind        Status
 ───────────────────────────────────────────
  example-secret      Secret      Completed
  example-configmap   ConfigMap   Completed
 ───────────────────────────────────────────
────────────────────────────  Level Completed Successfully ────────────────────────────
──────────────────────────────────  Starting Level 3 ──────────────────────────────────
                             Applying Level 3
 ─────────────────────────────────────────────────────────────────────────
  Name                            Kind                    Group   Version
 ─────────────────────────────────────────────────────────────────────────
  example-persistentvolumeclaim   PersistentVolumeClaim   Core    v1
 ─────────────────────────────────────────────────────────────────────────
PersistentVolumeClaim example-persistentvolumeclaim - Applying object
PersistentVolumeClaim example-persistentvolumeclaim - New object, will be created.
PersistentVolumeClaim example-persistentvolumeclaim - Application completed.
                          Completed Level 3
 ───────────────────────────────────────────────────────────────────
  Name                            Kind                    Status
 ───────────────────────────────────────────────────────────────────
  example-persistentvolumeclaim   PersistentVolumeClaim   Completed
 ───────────────────────────────────────────────────────────────────
────────────────────────────  Level Completed Successfully ────────────────────────────
──────────────────────────────────  Starting Level 4 ──────────────────────────────────
                  Applying Level 4
 ───────────────────────────────────────────────────
  Name                 Kind         Group   Version
 ───────────────────────────────────────────────────
  example-deployment   Deployment   Apps    v1
 ───────────────────────────────────────────────────
Deployment example-deployment - Applying object
Deployment example-deployment - New object, will be created.
Deployment example-deployment - Application completed.
               Completed Level 4
 ─────────────────────────────────────────────
  Name                 Kind         Status
 ─────────────────────────────────────────────
  example-deployment   Deployment   Completed
 ─────────────────────────────────────────────
────────────────────────────  Level Completed Successfully ────────────────────────────
──────────────────────────────────  Starting Level 5 ──────────────────────────────────
               Applying Level 5
 ─────────────────────────────────────────────
  Name              Kind      Group   Version
 ─────────────────────────────────────────────
  example-service   Service   Core    v1
 ─────────────────────────────────────────────
Service example-service - Applying object
Service example-service - New object, will be created.
Service example-service - Application completed.
            Completed Level 5
 ───────────────────────────────────────
  Name              Kind      Status
 ───────────────────────────────────────
  example-service   Service   Completed
 ───────────────────────────────────────
────────────────────────────  Level Completed Successfully ────────────────────────────
──────────────────────────────────  Starting Level 6 ──────────────────────────────────
               Applying Level 6
 ─────────────────────────────────────────────
  Name              Kind      Group   Version
 ─────────────────────────────────────────────
  example-cronjob   CronJob   Batch   v1
 ─────────────────────────────────────────────
CronJob example-cronjob - Applying object
CronJob example-cronjob - New object, will be created.
CronJob example-cronjob - Application completed.
╭───────────────────────────────────╮
│ Deployment completed successfully │
╰───────────────────────────────────╯
Deployment ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100% 0:00:00

```
