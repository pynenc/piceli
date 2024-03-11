from piceli.k8s.k8s_objects.base import K8sObject
from piceli.k8s.object_manager import service, volumes
from piceli.k8s.object_manager.base import ObjectManager


class ManagerFactory:
    @staticmethod
    def get_manager(k8s_object: K8sObject) -> ObjectManager:
        kind = k8s_object.kind
        if kind == "PersistentVolume":
            return volumes.PersistentVolumeManager(k8s_object)
        elif kind == "PersistentVolumeClaim":
            return volumes.PersistentVolumeClaimManager(k8s_object)
        elif kind == "Service":
            return service.ServiceManager(k8s_object)
        else:
            return ObjectManager(k8s_object)
