from piceli.k8s.constants.namespace import Namespace
from piceli.k8s.k8s_client.client import ClientContext
from piceli.k8s.k8s_objects.base import K8sObject


def find_all_objects(
    ctx: ClientContext, namespace: Namespace | None = None
) -> list[K8sObject]:
    """Find all the objects that exists in the cluster/namespace."""
    # TODO
    return []


def find_out_of_model(
    ctx: ClientContext, k8s_objects: list[K8sObject], namespace: Namespace | None = None
) -> list[K8sObject]:
    """Find the objects that exists in the cluster/namespace that are not defined in k8s_objects."""
    # TODO
    return []
