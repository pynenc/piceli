from typing import Optional

from piceli.k8s.constants import wait_conditions
from piceli.k8s.k8s_client.client import ClientContext
from piceli.k8s.object_manager import base
from piceli.k8s.utils import utils_wait


class DeploymentManager(base.ObjectManager):
    """Manager for deployment objects."""

    def wait(self, ctx: ClientContext, namespace: Optional[str] = None) -> None:
        """waits until the deployment is available"""
        utils_wait.wait(
            ctx=ctx,
            list_func=self._api_method(ctx, "list"),
            args=(self._resolve_namespace(namespace),),
            obj_name=self.k8s_object.name,
            condition=wait_conditions.WaitConditionDeployment.AVAILABLE,
        )
