"""Import one verified legacy journal execution without Kubernetes IO."""

from __future__ import annotations

from piceli.k8s.ops.execution_journal import ExecutionJournal
from piceli.k8s.ops.executor import PlanExecutor
from piceli.k8s.ops.legacy_execution import (
    LegacyExecutionArchive,
    import_legacy_execution,
)
from piceli.k8s.ops.secret_versions import SecretVersionStore


def import_and_resume(
    *,
    legacy_journal: ExecutionJournal,
    legacy_execution_id: str,
    archive_json: str,
    destination_journal: ExecutionJournal,
    private_versions: SecretVersionStore,
    executor: PlanExecutor,
) -> dict[str, object]:
    """Validate canonical evidence, copy safe receipts, then explicitly resume."""
    imported = import_legacy_execution(
        legacy_journal=legacy_journal,
        legacy_execution_id=legacy_execution_id,
        archive=LegacyExecutionArchive.from_json(archive_json),
        journal=destination_journal,
        secrets=private_versions,
    )
    return executor.run_bundle(imported.bundle, resume=True)
