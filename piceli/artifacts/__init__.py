"""Pure build plans and explicitly authorized local artifact production.

Importing this package loads no kubeconfig, builder, daemon or application code.
"""

from typing import TYPE_CHECKING

from piceli._lazy import lazy_exports
from piceli.artifacts.base_image import DockerArchiveOciBuilder
from piceli.artifacts.local_import import DockerLocalImporter, LocalImportGrant
from piceli.artifacts.oci import (
    OciBuilder,
    OciReceipt,
    RunnableOciReceipt,
    inspect_oci,
    inspect_runnable_oci,
    unpack_oci_archive,
)
from piceli.artifacts.plan import ArtifactFile, BuildPlan, SourcePin
from piceli.artifacts.process import (
    BuildCommand,
    ExecutionGrant,
    ProcessLimits,
    ToolPin,
)
from piceli.artifacts.runnable import DockerLocalRunner, LocalRunGrant, RunnableBuild
from piceli.artifacts.source_identity import (
    InputsLock,
    InputsSpec,
    InputsVerification,
    SourceDrift,
    SourceDriftError,
    SourceIdentity,
    SourceIdentityError,
    SourceSpec,
    capture_source_identity,
    compare_inputs,
    pinned_sources,
    record_inputs,
    verify_inputs,
)

__all__ = [
    "ArchiveSource",
    "ArtifactFile",
    "BuildCommand",
    "BuildGrant",
    "BuildPlan",
    "BuildSpec",
    "DeliveryGrant",
    "DockerArchiveOciBuilder",
    "DockerImageSource",
    "DockerLocalImporter",
    "DockerLocalRunner",
    "ExecutionGrant",
    "InputsLock",
    "InputsSpec",
    "InputsVerification",
    "LocalImportGrant",
    "LocalRunGrant",
    "NodeDelivery",
    "NodeTarget",
    "OciBuilder",
    "OciReceipt",
    "ProcessLimits",
    "RegistryCredentials",
    "RegistryDelivery",
    "RegistryForward",
    "RegistryTarget",
    "RunnableBuild",
    "RunnableOciReceipt",
    "SourceDrift",
    "SourceDriftError",
    "SourceIdentity",
    "SourceIdentityError",
    "SourcePin",
    "SourceSpec",
    "ToolPin",
    "append_journal",
    "capture_source_identity",
    "compare_inputs",
    "inspect_oci",
    "inspect_runnable_oci",
    "pinned_sources",
    "record_inputs",
    "unpack_oci_archive",
    "verify_inputs",
]

# Loaded on first use (piceli/_lazy.py): importing the facade stays cheap and
# loads no Kubernetes client.
_LAZY = {
    "ArchiveSource": "piceli.artifacts.delivery",
    "BuildGrant": "piceli.artifacts.build_spec",
    "BuildSpec": "piceli.artifacts.build_spec",
    "DeliveryGrant": "piceli.artifacts.delivery",
    "DockerImageSource": "piceli.artifacts.delivery",
    "NodeDelivery": "piceli.artifacts.delivery",
    "NodeTarget": "piceli.artifacts.node_transport",
    "RegistryCredentials": "piceli.artifacts.registry",
    "RegistryDelivery": "piceli.artifacts.registry_delivery",
    "RegistryForward": "piceli.artifacts.registry_delivery",
    "RegistryTarget": "piceli.artifacts.registry",
    "append_journal": "piceli.artifacts.delivery",
}
__getattr__, __dir__ = lazy_exports(__name__, _LAZY, globals())

if TYPE_CHECKING:
    from piceli.artifacts.build_spec import BuildGrant, BuildSpec
    from piceli.artifacts.delivery import (
        ArchiveSource,
        DeliveryGrant,
        DockerImageSource,
        NodeDelivery,
        append_journal,
    )
    from piceli.artifacts.node_transport import NodeTarget
    from piceli.artifacts.registry import RegistryCredentials, RegistryTarget
    from piceli.artifacts.registry_delivery import RegistryDelivery, RegistryForward
