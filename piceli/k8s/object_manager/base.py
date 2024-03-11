import logging
import time
from dataclasses import dataclass
from functools import cached_property
from multiprocessing.pool import ApplyResult
from typing import Any, Callable, Optional

from kubernetes import client
from kubernetes.client.exceptions import ApiException

from piceli import settings
from piceli.k8s.constants.dry_run import DryRun
from piceli.k8s.constants.namespace import Namespace
from piceli.k8s.exceptions import api_exceptions
from piceli.k8s.k8s_client.client import ClientContext
from piceli.k8s.k8s_objects.base import K8sObject, OriginCluster
from piceli.k8s.utils import utils_api, utils_retry, utils_wait

logger = logging.getLogger(__name__)


@dataclass
class ObjectManager:
    k8s_object: K8sObject

    def get_api(self, ctx: ClientContext) -> Any:
        return ctx.get_api(self.k8s_object.api_name)

    @cached_property
    def api_methods(self) -> list[str]:
        api = ClientContext.get_api_class(self.k8s_object.api_name)
        return utils_api.get_available_api_methods(api, self.k8s_object.kind)

    def get_method_name(self, method: str) -> str:
        return utils_api.build_api_method_name(
            method, self.namespaced, self.k8s_object.kind
        )

    @cached_property
    def namespaced(self) -> bool:
        return utils_api.is_namespaced(self.api_methods)

    def _resolve_namespace(self, namespace: Optional[str]) -> str:
        return namespace or self.k8s_object.namespace or Namespace.DEFAULT.value

    def _prepare_args(
        self,
        namespace: Optional[str] = None,
        with_name: bool = False,
        with_spec: bool = True,
    ) -> tuple:
        """Prepare common arguments for API calls."""
        args: list = []
        if with_name:
            args.append(self.k8s_object.name)
        if self.namespaced:
            args.append(self._resolve_namespace(namespace))
        if with_spec:
            args.append(self.k8s_object.spec)
        return tuple(args)

    def _api_method(self, ctx: ClientContext, method: str) -> Callable:
        return getattr(self.get_api(ctx), self.get_method_name(method))

    def _invoke_api(
        self, ctx: ClientContext, method: str, *args: Any, **kwargs: Any
    ) -> Any:
        """Generic method to invoke API calls."""
        try:
            return self._api_method(ctx, method)(*args, **kwargs)
        except ApiException as ex:
            raise api_exceptions.ApiOperationException.from_api_exception(ex) from ex

    def read(self, ctx: ClientContext, namespace: Optional[str] = None) -> K8sObject:
        """reads the k8s object from the cluster, exception if not exists"""
        args = self._prepare_args(with_name=True, with_spec=False, namespace=namespace)
        spec = self._invoke_api(ctx, "read", *args)
        spec_dict = client.ApiClient().sanitize_for_serialization(spec)
        return K8sObject(
            spec_dict, OriginCluster(ctx.kubeconfig, self._resolve_namespace(namespace))
        )

    def patch(
        self,
        ctx: ClientContext,
        namespace: Optional[str] = None,
        async_req: bool = False,
        dry_run: DryRun = DryRun.OFF,
        patch_doc: Optional[dict] = None,
    ) -> Any | ApplyResult:
        """patch the existing k8s object"""
        args = self._prepare_args(namespace, with_name=True, with_spec=False)
        args = args + ((patch_doc,) if patch_doc else (self.k8s_object.spec,))
        return self._invoke_api(
            ctx, "patch", *args, async_req=async_req, dry_run=dry_run.value
        )

    @utils_retry.retry
    def create(
        self,
        ctx: ClientContext,
        namespace: Optional[str] = None,
        async_req: bool = False,
        dry_run: DryRun = DryRun.OFF,
    ) -> Any | ApplyResult:
        """Internal method to attempt creation of the k8s object, with retry logic."""
        args = self._prepare_args(namespace)
        try:
            return self._invoke_api(
                ctx, "create", *args, async_req=async_req, dry_run=dry_run.value
            )
        except api_exceptions.ApiOperationException as ex:
            if ex.already_exists:
                if ex.is_being_deleted:
                    raise utils_retry.RetryException(
                        f"{self.k8s_object} is being deleted"
                    ) from ex
                logger.error(f"{self.k8s_object} already exists: {ex.body}")
                return None
            raise

    def delete(
        self,
        ctx: ClientContext,
        namespace: Optional[str] = None,
        async_req: bool = False,
        dry_run: DryRun = DryRun.OFF,
    ) -> Any | ApplyResult:
        """delete the k8s object"""
        try:
            args = self._prepare_args(namespace, with_name=True, with_spec=False)
            return self._invoke_api(
                ctx, "delete", *args, async_req=async_req, dry_run=dry_run.value
            )
        except api_exceptions.ApiOperationException as ex:
            if ex.not_found:
                logger.info(f"{self.k8s_object} do not exists, nothing to delete")
                return None
            raise

    def apply(
        self,
        ctx: ClientContext,
        namespace: Optional[str] = None,
        async_req: bool = False,
        dry_run: DryRun = DryRun.OFF,
    ) -> Any | ApplyResult:
        """deletes the object if already exists and then creates new version"""
        logger.info(f"applying {self.k8s_object} ")
        try:
            self.read(ctx, namespace)
            logger.info(f"{self.k8s_object} already exists, deleting and recreating")
            return self.replace(ctx, namespace, async_req, dry_run)
        except api_exceptions.ApiOperationException as ex:
            if not ex.not_found:
                raise
            logger.info(f"{self.k8s_object} do not exists, creating")
        return self.create(ctx, namespace, async_req, dry_run)

    def replace(
        self,
        ctx: ClientContext,
        namespace: Optional[str] = None,
        async_req: bool = False,
        dry_run: DryRun = DryRun.OFF,
    ) -> Any | ApplyResult:
        """deletes the object and  creates new version"""
        logger.info(f"replacing {self.k8s_object} ")
        self.delete(ctx, namespace, async_req, dry_run)
        if dry_run == DryRun.ON:
            logger.warning(
                "Running apply with dry_run:ON, aborting before creating"
                "because the previous delete on dry_run did nothing,"
                "so the create will fail with already exists"
            )
            return self.read(ctx, namespace)
        timeout = time.time() + settings.K8S_DELETE_TIMEOUT
        while True:
            logger.debug(f"waiting for {self.k8s_object} to be deleted")
            self.read(ctx, namespace)
            if time.time() > timeout:
                break
            time.sleep(1)
        return self.create(ctx, namespace, async_req, dry_run)

    def wait(self, ctx: ClientContext, namespace: Optional[str] = None) -> None:
        """waits until the k8s object exists"""
        args = (self._resolve_namespace(namespace),) if self.namespaced else ()
        utils_wait.wait(
            ctx=ctx,
            list_func=self._api_method(ctx, "list"),
            args=args,
            obj_name=self.k8s_object.name,
        )
