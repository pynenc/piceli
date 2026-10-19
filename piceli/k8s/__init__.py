"""Public Kubernetes planning, durable execution and release APIs."""

from typing import TYPE_CHECKING

from piceli._lazy import lazy_exports
from piceli.k8s.release import (
    RELEASE_CATALOG_SCHEMA_VERSION,
    ReleaseCatalog,
    ReleaseRecord,
    ReleaseSource,
    ReleaseWorkflow,
    load_release_input,
)

__all__ = (
    "RELEASE_CATALOG_SCHEMA_VERSION",
    "ConcurrentWriterError",
    "FileStateStore",
    "InstanceLock",
    "ReleaseCatalog",
    "ReleaseRecord",
    "ReleaseSource",
    "ReleaseWorkflow",
    "StandingPolicy",
    "UserStore",
    "health_aware_rollback",
    "load_release_input",
    "promote_release",
)

# Loaded on first use (piceli/_lazy.py): importing the facade stays cheap and
# loads no Kubernetes client.
_LAZY = {
    "ConcurrentWriterError": "piceli.k8s.operator_state",
    "FileStateStore": "piceli.k8s.operator_state",
    "InstanceLock": "piceli.k8s.operator_state",
    "StandingPolicy": "piceli.k8s.operator_state",
    "UserStore": "piceli.k8s.operator_state",
    "health_aware_rollback": "piceli.k8s.automation",
    "promote_release": "piceli.k8s.automation",
}
__getattr__, __dir__ = lazy_exports(__name__, _LAZY, globals())

if TYPE_CHECKING:
    from piceli.k8s.automation import health_aware_rollback, promote_release
    from piceli.k8s.operator_state import (
        ConcurrentWriterError,
        FileStateStore,
        InstanceLock,
        StandingPolicy,
        UserStore,
    )
