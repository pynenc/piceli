# Volumes in Kubernetes with Piceli

Kubernetes volumes offer a way to persist data, share data between containers, and expose configuration information and secrets to your applications. Piceli streamlines the creation and management of different types of volumes and their mounts in pods, providing flexible solutions for various storage needs.

## Volume (Abstract Base Class)

Serves as the foundation for defining persistent volumes (PVs) and persistent volume claims (PVCs). It abstracts common attributes necessary for volume management in Kubernetes.

## PersistentVolume

Represents a Kubernetes PersistentVolume, providing durable storage that persists beyond the lifecycle of individual pods.

### Properties

- `name`: The volume's unique name.
- `disk_name`: Identifier for the disk in the underlying infrastructure, e.g., for GCE Persistent Disks.
- `storage`: The size of the storage allocated for the volume.
- `labels`: Optional labels for organizational purposes.

## PersistentVolumeClaim

Defines a PersistentVolumeClaim, which allows pods to specify their storage requirements dynamically.

### PersistentVolumeClaim Properties

- `name`: The claim's unique name.
- `storage`: The size of the storage requested.
- `labels`: Optional labels for the claim.

## PersistentVolumeClaimTemplate

Used within StatefulSets to define a template for generating PVCs automatically.

### PersistentVolumeClaimTemplate Properties

- `name`: Template's name, used to generate PVCs.
- `storage`: The size of the storage for each PVC created from the template.
- `labels`: Optional labels for the generated PVCs.

## VolumeMounts

Defines how volumes are mounted into pods, including paths and subpaths within the container's filesystem.

### Types

- `VolumeMountPVC`: Mounts a PVC to a specified path within the pod.
- `VolumeMountPVCTemplate`: Uses a PVC template for mounting, useful in stateful applications.
- `VolumeMountConfigMap`: Mounts a ConfigMap as a volume, exposing configuration data.
- `VolumeMountSecret`: Exposes secrets as volumes, ensuring sensitive data is securely available.
- `VolumeMountEmptyDir`: Utilizes an empty directory shared by all containers in a pod, useful for temporary storage and inter-container data sharing.

### Example

```python
from piceli.k8s import templates

# Create a volume mount for a ConfigMap
volume_mount_config_map = templates.VolumeMountConfigMap(
    mount_path="/app/config",
    config_map=my_config_map,
    default_mode=0o640
)

# Define a service with a PVC volume mount
service_with_pvc = templates.Service(
    name="my-service",
    ports=[service_port],
    volumes=[volume_mount_pvc],
    selector={"app": "myApp"}
)
```

These classes and their configurations offer comprehensive solutions for managing storage and data within Kubernetes applications using Piceli.
