"""Public Kubernetes planning and durable execution APIs."""

from typing import TYPE_CHECKING

from piceli._lazy import lazy_exports
from piceli.k8s.ops.legacy_execution import (
    LEGACY_EXECUTION_ARCHIVE_SCHEMA_VERSION,
    LegacyEvidenceInsufficient,
    LegacyExecutionArchive,
    LegacyExecutionImport,
    import_legacy_execution,
)
from piceli.k8s.ops.revision import DeploymentRevision, ExecutionBundle
from piceli.k8s.ops.session import (
    DEPLOYMENT_SESSION_SCHEMA_VERSION,
    DeploymentSession,
    DeploymentSessionArchive,
)

__all__ = [
    "DEPLOYMENT_SESSION_SCHEMA_VERSION",
    "LEGACY_EXECUTION_ARCHIVE_SCHEMA_VERSION",
    "DeploymentComponent",
    "DeploymentComposition",
    "DeploymentPlan",
    "DeploymentRevision",
    "DeploymentSession",
    "DeploymentSessionArchive",
    "ExecutionAuthorization",
    "ExecutionBundle",
    "ExecutionJournal",
    "LegacyEvidenceInsufficient",
    "LegacyExecutionArchive",
    "LegacyExecutionImport",
    "ObservedSnapshot",
    "PlanAuthorization",
    "PlanExecutor",
    "ResourceIdentity",
    "ResourceIntent",
    "SecretVersionRef",
    "SecretVersionStore",
    "component_from_objects",
    "import_legacy_execution",
    "load_all",
]

# Loaded on first use (piceli/_lazy.py): importing the facade stays cheap and
# loads no Kubernetes client.
_LAZY = {
    "DeploymentComponent": "piceli.k8s.ops.plan",
    "DeploymentComposition": "piceli.k8s.ops.plan",
    "DeploymentPlan": "piceli.k8s.ops.plan",
    "ExecutionAuthorization": "piceli.k8s.ops.executor",
    "ExecutionJournal": "piceli.k8s.ops.execution_journal",
    "ObservedSnapshot": "piceli.k8s.ops.plan",
    "PlanAuthorization": "piceli.k8s.ops.plan",
    "PlanExecutor": "piceli.k8s.ops.executor",
    "ResourceIdentity": "piceli.k8s.ops.discovery",
    "ResourceIntent": "piceli.k8s.ops.plan",
    "SecretVersionRef": "piceli.k8s.ops.secret_versions",
    "SecretVersionStore": "piceli.k8s.ops.secret_versions",
    "component_from_objects": "piceli.k8s.ops.plan",
    "load_all": "piceli.k8s.ops.loader",
}
__getattr__, __dir__ = lazy_exports(__name__, _LAZY, globals())

if TYPE_CHECKING:
    from piceli.k8s.ops.discovery import ResourceIdentity
    from piceli.k8s.ops.execution_journal import ExecutionJournal
    from piceli.k8s.ops.executor import ExecutionAuthorization, PlanExecutor
    from piceli.k8s.ops.loader import load_all
    from piceli.k8s.ops.plan import (
        DeploymentComponent,
        DeploymentComposition,
        DeploymentPlan,
        ObservedSnapshot,
        PlanAuthorization,
        ResourceIntent,
        component_from_objects,
    )
    from piceli.k8s.ops.secret_versions import SecretVersionRef, SecretVersionStore
