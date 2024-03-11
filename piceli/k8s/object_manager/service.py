import logging
from typing import Optional

from piceli import settings
from piceli.k8s.k8s_client.client import ClientContext
from piceli.k8s.object_manager import base
from piceli.k8s.utils import utils_wait

logger = logging.getLogger(__name__)


class ServiceManager(base.ObjectManager):
    """Manager for Service objects."""

    def wait(self, ctx: ClientContext, namespace: Optional[str] = None) -> None:
        logger.info(f"Waiting for service {self.k8s_object}")
        # todo retry for urllib3.exceptions.ProtocolError
        for event in ctx.watch.stream(
            ctx.core_api.list_namespaced_endpoints,
            self._resolve_namespace(namespace),
            field_selector=f"metadata.name={self.k8s_object.name}",
            timeout_seconds=settings.K8S_WAIT_TIMEOUT,
            _request_timeout=settings.K8S_WAIT_REQUEST_TIMEOUT,
        ):
            details = []
            for subset in getattr(event["object"], "subsets", []) or []:
                for address in subset.addresses or []:
                    details.append(
                        f"Endpoint({address.ip} --> {address.target_ref.kind} {address.target_ref.name})"
                    )
            if details:
                ctx.watch.stop()
                logger.info(f"Done, found service {self.k8s_object} {details=}")
                return
        raise utils_wait.WaitException(f"Service {self.k8s_object} is not available")
