import logging
from abc import abstractmethod
from multiprocessing.pool import ApplyResult
from typing import Any, Optional

from kubernetes.utils.quantity import parse_quantity

from piceli.k8s.constants import phases
from piceli.k8s.constants.dry_run import DryRun
from piceli.k8s.k8s_client.client import ClientContext
from piceli.k8s.object_manager import base
from piceli.k8s.utils import utils_wait

logger = logging.getLogger(__name__)


class VolumeManager(base.ObjectManager):
    """Common manager for PV and PVC objects."""

    @staticmethod
    @abstractmethod
    def get_storage(spec: dict) -> str:
        """Get the capacity of a volume."""

    @property
    def storage(self) -> str:
        """Get the capacity of the volume."""
        return self.get_storage(self.k8s_object.spec)

    def apply(
        self,
        ctx: ClientContext,
        namespace: Optional[str] = None,
        async_req: bool = False,
        dry_run: DryRun = DryRun.OFF,
    ) -> Any | ApplyResult:
        """Creates the volume or applies if previous volume had less storage"""
        logger.info(f"applying {self.k8s_object} ")
        if volume := self.read(ctx, namespace):
            current_storage = self.get_storage(volume.spec)
            logger.info(f"{self.k8s_object} already exists")
            if parse_quantity(current_storage) > parse_quantity(self.storage):
                logger.warning(
                    f"Not possible to apply {self.k8s_object}: "
                    f"specified capacity {self.storage} is smaller than existing {current_storage}"
                )
            else:
                logger.info(f"patching {self.k8s_object}")
                return self.patch(ctx, namespace, async_req, dry_run)
            return volume
        logger.info(f"creating {self.k8s_object}")
        return self.create(ctx, namespace, async_req, dry_run)

    def replace(
        self,
        ctx: ClientContext,
        namespace: str | None = None,
        async_req: bool = False,
        dry_run: DryRun = DryRun.OFF,
    ) -> Any | ApplyResult:
        del ctx, namespace, async_req, dry_run
        raise RuntimeError(
            "Not automatized, replacing the volume will remove all the data"
        )
        # TODO: this option should be configurable

    def delete(
        self,
        ctx: ClientContext,
        namespace: Optional[str] = None,
        async_req: bool = False,
        dry_run: DryRun = DryRun.OFF,
    ) -> Any | ApplyResult:
        del ctx, namespace, async_req, dry_run
        raise RuntimeError(
            "Not automatized, deleting the volume will remove all the data"
        )
        # TODO: this option should be configurable

    @property
    @abstractmethod
    def wait_phases(self) -> list[phases.Phase]:
        """Get the phases to wait for."""

    def wait(self, ctx: ClientContext, namespace: Optional[str] = None) -> None:
        args = (self._resolve_namespace(namespace),) if self.namespaced else ()
        utils_wait.wait(
            ctx=ctx,
            list_func=self._api_method(ctx, "list"),
            args=args,
            obj_name=self.k8s_object.name,
            phases=self.wait_phases,
        )


class PersistentVolumeManager(VolumeManager):
    """Manager for PV objects."""

    @staticmethod
    def get_storage(spec: dict) -> str:
        """Get the capacity of a volume."""
        return spec["capacity"]["storage"]

    @property
    def wait_phases(self) -> list[phases.Phase]:
        return [phases.PhaseVolume.AVAILABLE, phases.PhaseVolume.BOUND]


class PersistentVolumeClaimManager(VolumeManager):
    """Manager for PV objects."""

    @staticmethod
    def get_storage(spec: dict) -> str:
        """Get the capacity of a volume."""
        return spec["spec"]["resources"]["requests"]["storage"]

    @property
    def wait_phases(self) -> list[phases.Phase]:
        return [
            phases.PhaseVolume.AVAILABLE,
            phases.PhasePVC.PENDING,
            phases.PhaseVolume.BOUND,
        ]
