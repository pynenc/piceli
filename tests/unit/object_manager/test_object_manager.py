import re


from piceli.k8s.k8s_client.client import ClientContext
from piceli.k8s.object_manager import base

UPPER_FOLLOWED_BY_LOWER_RE = re.compile("(.)([A-Z][a-z]+)")
LOWER_OR_NUM_FOLLOWED_BY_UPPER_RE = re.compile("([a-z0-9])([A-Z])")


def test_x(object_manager: base.ObjectManager) -> None:
    from kubernetes.client.exceptions import ApiException

    try:
        ctx = ClientContext()
        object_manager.create(ctx)
        object_manager.wait(ctx)
        object_manager.delete(ctx)
    except ApiException as ex:
        print(ex)
