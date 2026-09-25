from abc import ABC, abstractmethod
from typing import Annotated

from kubernetes import client
from pydantic import BaseModel, Field

from piceli.k8s.templates.auxiliary import names, quantity
from piceli.k8s.templates.auxiliary.labels import Labels
from piceli.k8s.templates.deployable import base, configmap, secret


class Volume(ABC, BaseModel):
    """
    Abstract base class for defining Kubernetes volumes.

    :param names.Name name: The name of the volume.
    :param quantity.Quantity storage: The storage size allocated for the volume.
    :param Optional[Labels] labels: Custom labels to apply to the volume.
    """

    name: names.Name
    storage: quantity.Quantity
    labels: Labels | None = None

    @staticmethod
    @abstractmethod
    def get_volume_capacity(
        volume: client.V1PersistentVolume | client.V1PersistentVolumeClaim,
    ) -> str:
        """gets the capacity of the volume"""


class PersistentVolume(base.Deployable, Volume):
    """
    Represents a Kubernetes PersistentVolume.

    :param str disk_name: The disk identifier in the underlying infrastructure.
    Inherits :param name, :param storage, and :param labels from Volume.
    """

    disk_name: str

    def get(self) -> list[client.V1PersistentVolume]:
        obj = client.V1PersistentVolume(
            api_version="v1",
            kind="PersistentVolume",
            metadata=client.V1ObjectMeta(name=self.name, labels=self.labels),
            spec=client.V1PersistentVolumeSpec(
                capacity={"storage": self.storage},
                storage_class_name="manual",
                access_modes=["ReadWriteOnce"],
                gce_persistent_disk=client.V1GCEPersistentDiskVolumeSource(
                    pd_name=self.disk_name, fs_type="ext4"
                ),
            ),
        )
        return [obj]

    @staticmethod
    def get_volume_capacity(volume: client.V1PersistentVolume) -> str:
        """gets the capacity of the volume"""
        return volume.spec.capacity["storage"]


class PersistentVolumeClaim(Volume, base.Deployable):
    """
    Represents a Kubernetes PersistentVolumeClaim.

    Inherits :param name, :param storage, and :param labels from Volume.
    """

    def get(self) -> list[client.V1PersistentVolumeClaim]:
        obj = client.V1PersistentVolumeClaim(
            api_version="v1",
            kind="PersistentVolumeClaim",
            metadata=client.V1ObjectMeta(name=self.name, labels=self.labels),
            spec=client.V1PersistentVolumeClaimSpec(
                access_modes=["ReadWriteOnce"],
                resources=client.V1ResourceRequirements(
                    requests={"storage": self.storage}
                ),
            ),
        )
        return [obj]

    @staticmethod
    def get_volume_capacity(volume: client.V1PersistentVolumeClaim) -> str:
        """gets the capacity of the volume"""
        return volume.spec.resources.requests["storage"]


class PersistentVolumeClaimTemplate(BaseModel):
    """
    Template for PersistentVolumeClaim, used in stateful applications.

    :param names.Name name: The template name.
    :param quantity.Quantity storage: The storage size for PVCs created from this template.
    :param Optional[Labels] labels: Labels for the generated PVCs.
    """

    name: names.Name
    storage: quantity.Quantity
    labels: Labels | None = None

    def get_template(self) -> client.V1PersistentVolumeClaim:
        """get a volume claim template for stateful sets"""
        return client.V1PersistentVolumeClaimTemplate(
            metadata=client.V1ObjectMeta(name=self.name, labels=self.labels),
            spec=client.V1PersistentVolumeClaimSpec(
                access_modes=["ReadWriteOnce"],
                volume_mode="Filesystem",
                resources=client.V1ResourceRequirements(
                    requests={"storage": self.storage}
                ),
            ),
        )


Path = Annotated[str, Field(pattern=r"^(/[^/]+)+/?$")]
SubPath = Annotated[str, Field(pattern=r"^(?:[^/][^/]*/)*[^/]+$")]


class VolumeMount(BaseModel):
    """
    Base class for volume mounts in a pod.

    :param Path mount_path: The path within the container where the volume is mounted.
    """

    mount_path: Path


class VolumeMountPVC(VolumeMount):
    """
    Mounts a PersistentVolumeClaim to a pod.

    :param PersistentVolumeClaim pvc: The PVC to be mounted.
    :param Optional[SubPath] sub_path: The subpath within the volume to mount.
    """

    pvc: PersistentVolumeClaim
    sub_path: SubPath | None = None


class VolumeMountPVCTemplate(VolumeMount):
    """
    Utilizes a PVC template for volume mounting, particularly in StatefulSets.

    :param PersistentVolumeClaimTemplate pvc_template: The PVC template used for mounting.
    :param Optional[SubPath] sub_path: The subpath within the volume to mount.
    """

    pvc_template: PersistentVolumeClaimTemplate
    sub_path: SubPath | None = None


DefaultMode = Annotated[int, Field(ge=0, le=511)]


class VolumeMountConfigMap(VolumeMount):
    """
    Mounts a ConfigMap as a volume to expose configuration data.

    :param configmap.ConfigMap config_map: The ConfigMap to mount.
    :param DefaultMode default_mode: Permissions for the mounted files.
    """

    config_map: configmap.ConfigMap
    default_mode: DefaultMode


class VolumeMountSecret(VolumeMount):
    """
    Exposes secrets as volumes, securing sensitive data.

    :param secret.Secret secret: The secret to mount.
    :param DefaultMode default_mode: Permissions for the mounted files.
    """

    secret: secret.Secret
    default_mode: DefaultMode = 0o600
    # defaultMode is Optional: mode bits used to set permissions on created files by default.
    # Must be an octal value between 0000 and 0777 or a decimal value between 0 and 511.
    # YAML accepts both octal and decimal values, JSON requires decimal values for mode bits.
    # Defaults to 0644. Directories within the path are not affected by this setting.
    # This might be in conflict with other options that affect the file mode,
    # like fsGroup, and the result can be other mode bits set.


class VolumeMountEmptyDir(VolumeMount):
    """
    Utilizes an EmptyDir volume for temporary storage shared between containers.

    :param names.Name name: The name of the EmptyDir volume.
    """

    name: names.Name
