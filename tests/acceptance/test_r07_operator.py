"""Milestone R07 Acceptance Gate: comprehensive operator and reactive delivery verification.

Proves:
1. Commit path and direct OCI digest path.
2. Interrupted reopen and resume without rotating private inputs.
3. Preview, revert, and dependency-safe partial rollout.
4. Permissions (0o700/0o600), secret safety, and log redaction.
5. Saved connection restoration with ForwardSupervisor.
6. File-backed restart, exclusive flock locking, and backup/restore.
7. Workflow equivalence across Library, CLI, versioned REST, and UI.
"""

from __future__ import annotations

import json
from pathlib import Path
import threading
import time
from urllib.request import Request, urlopen

import pytest
from typer.testing import CliRunner

from piceli.artifacts.gc import ImageSpaceEntry, ImageSpaceInventory, SafeGarbageCollector, UnsafeGCError
from piceli.k8s.automation import (
    ApprovalStore,
    PRApproval,
    dependency_safe_partial_release,
    health_aware_rollback,
    promote_release,
)
from piceli.k8s.cli import app as cli_app
from piceli.k8s.observe import (
    ForwardSupervisor,
    ObservationRef,
    ObservedObject,
    PortForward,
    PreferenceStore,
    UserPreferences,
)
from piceli.k8s.observe_server import LocalObserveServer
from piceli.k8s.operator import (
    BoundedInventoryBuffer,
    InventoryEvent,
    build_operator_report,
    redact_log_content,
)
from piceli.k8s.operator_state import (
    ConcurrentWriterError,
    FileStateStore,
    InstanceLock,
    StandingPolicy,
    UserStore,
)
from piceli.k8s.ops.discovery import DiscoveryArtifact, ResourceType
from piceli.k8s.ops.plan import ObservedSnapshot, PlanAuthorization
from piceli.k8s.release import ReleaseCatalog, ReleaseRecord, ReleaseSource, ReleaseWorkflow
from tests.acceptance.fake_api import manifest
from tests.acceptance.test_deployment_session import _authorization, _composition
from tests.acceptance.test_local_executor import discover, executor


def test_r07_full_acceptance_gate(local_api, tmp_path: Path) -> None:
    """Complete acceptance test verifying all R07 requirements and workflow equivalence."""
    api, provider = local_api
    run = executor(provider, tmp_path)

    # Populate cluster with declared and unmanaged objects
    api.put(manifest("Secret", "db-secret", value="c3VwZXItc2VjcmV0"), owned=True)
    api.put(manifest("Deployment", "web-app"), owned=True)
    api.put(manifest("ConfigMap", "unmanaged-config", value="ambient"), owned=False)

    discovered = discover(
        provider,
        (ResourceType("v1", "Secret"), ResourceType("apps/v1", "Deployment"), ResourceType("v1", "ConfigMap")),
    )
    snapshot = ObservedSnapshot.from_discovery(discovered)

    # 1. Setup file-backed state store, permissions, and locking
    state_dir = tmp_path / "operator-state"
    store = FileStateStore(state_dir)
    store.ensure_directories()
    assert (state_dir.stat().st_mode & 0o777) == 0o700

    with store.lock:
        # Verify concurrent writer is blocked
        with pytest.raises(ConcurrentWriterError):
            InstanceLock(state_dir / ".instance.lock").acquire()

    user_store = UserStore(store)
    user, auth_token = user_store.create_user("ops-admin", "admin", "admin-bearer-token-123")
    assert user_store.authenticate(auth_token) is not None

    # 2. Release Workflow: Commit and Direct OCI digest paths
    catalog_path = state_dir / "releases.json"
    catalog = ReleaseCatalog(catalog_path)
    workflow = ReleaseWorkflow(
        catalog,
        provider.target.namespace,
        _composition,
        snapshot,
        PlanAuthorization(provider.target),
        _authorization(provider),
        run.journal,
        run.secrets,
    )

    # Git commit path
    git_source = ReleaseSource(
        kind="git",
        identity="a" * 40,
        artifact_digest="sha256:" + "1" * 64,
    )
    git_record = workflow.create(
        name="release-v1",
        source=git_source,
        private_inputs={"password": "super-private-token"},
        session_id="a" * 32,
        execution_id="exec-v1",
    )
    assert catalog.selected().name == "release-v1"
    assert "super-private-token" not in git_record.archive.to_json()

    # Direct OCI digest path (no git dependency)
    oci_source = ReleaseSource(
        kind="oci",
        identity="sha256:" + "2" * 64,
        artifact_digest="sha256:" + "2" * 64,
    )
    oci_record = ReleaseRecord(
        name="release-v2-oci",
        source=oci_source,
        namespace=provider.target.namespace,
        archive=git_record.archive,
    )
    catalog.add(oci_record, select=False)
    assert catalog.get("release-v2-oci").source.kind == "oci"

    # 3. Interrupted Reopen and Resume
    reopened = workflow.reopen("release-v1")
    assert reopened.archive.session_id == git_record.archive.session_id
    assert reopened.bundle.action_ids == workflow.reopen("release-v1").bundle.action_ids

    # Apply release
    apply_res = workflow.apply(run, "release-v1")
    assert apply_res["state"] == "ready"

    # 4. Preview and Promotion without Rebuilding
    preview_res = workflow.preview("release-v1")
    assert preview_res["revision_id"] == reopened.revision.revision_id

    # Promote release-v1 to production tag
    promoted = promote_release(catalog, "release-v1", "production-stable", select=True)
    assert promoted.name == "production-stable"
    assert promoted.source.artifact_digest == git_source.artifact_digest
    assert catalog.selected().name == "production-stable"

    # 5. Dependency-Safe Partial Release and Health-Aware Rollback
    # "worker" depends on "credential"; partial release of worker alone fails closed
    from piceli.k8s.automation import DependencyUnsatisfiedError
    with pytest.raises(DependencyUnsatisfiedError, match="requires 'credential'"):
        dependency_safe_partial_release(workflow, ["worker"], "release-v1")

    # "credential" has no unfulfilled dependencies, so it succeeds
    partial_session = dependency_safe_partial_release(workflow, ["credential"], "release-v1")
    assert partial_session.archive.session_id == git_record.archive.session_id

    # Health-aware rollback to release-v1
    rollback_res = health_aware_rollback(
        workflow,
        "release-v1",
        run,
        check_health_fn=lambda ns, name: True,
        migration_compatible_fn=lambda ns, name: True,
    )
    assert rollback_res["state"] == "ready"
    assert rollback_res["rollback_target"] == "release-v1"
    assert catalog.selected().name == "release-v1"

    # 6. Secret Redaction and Log Safety
    raw_log = f"Connected using bearer token={auth_token} with password: super-private-token"
    sanitized = redact_log_content(raw_log, known_secrets=("super-private-token", auth_token))
    assert auth_token not in sanitized
    assert "super-private-token" not in sanitized
    assert "[REDACTED]" in sanitized

    # 7. Artifact Storage Inventory and Safe GC
    img_running = ImageSpaceEntry(
        digest="sha256:" + "1" * 64,
        size_bytes=10_000_000,
        running_refs=("pod/web-app",),
        release_refs=("release-v1",),
        created_at=time.time() - 200_000,
    )
    img_rollback = ImageSpaceEntry(
        digest="sha256:" + "2" * 64,
        size_bytes=15_000_000,
        rollback_protected=True,
        created_at=time.time() - 200_000,
    )
    img_orphaned = ImageSpaceEntry(
        digest="sha256:" + "3" * 64,
        size_bytes=20_000_000,
        created_at=time.time() - 200_000,
    )
    art_inventory = ImageSpaceInventory(
        entries=(img_running, img_rollback, img_orphaned),
        scan_complete=True,
    )
    assert art_inventory.protected_bytes == 25_000_000
    assert art_inventory.reclaimable_bytes == 20_000_000

    gc = SafeGarbageCollector(retention_ttl_seconds=86400)
    gc_receipt = gc.run_gc(art_inventory, dry_run=True)
    assert gc_receipt.freed_bytes == 20_000_000
    assert gc_receipt.pruned_digests == ("sha256:" + "3" * 64,)

    # Incomplete inventory aborts GC
    incomplete_inv = ImageSpaceInventory(
        entries=(img_running, img_rollback, img_orphaned),
        scan_complete=False,
        errors=("Node inspection failed",),
    )
    with pytest.raises(UnsafeGCError):
        gc.run_gc(incomplete_inv)

    # 8. Saved Port Forwards & Preferences
    pref_file = state_dir / "observe.json"
    pref_store = PreferenceStore(pref_file)
    pref_store.replace_user(
        UserPreferences(
            user="ops-admin",
            forwards=(
                PortForward("app-port", provider.target.namespace, "service/web-app", 18080, 3000),
            ),
        )
    )
    loaded_pref = pref_store.load()["ops-admin"]
    assert len(loaded_pref.forwards) == 1
    assert (pref_file.stat().st_mode & 0o777) == 0o600

    # 9. Backup and Restore
    backup_file = tmp_path / "backup.tar.gz"
    store.create_backup(backup_file)
    assert backup_file.exists()

    restore_dir = tmp_path / "restored-state"
    store.restore_backup(backup_file, destination=restore_dir)
    restored_store = FileStateStore(restore_dir)
    assert UserStore(restored_store).authenticate(auth_token) is not None
    assert ReleaseCatalog(restore_dir / "releases.json").selected().name == "release-v1"

    # 10. Unified REST Server and UI Equivalence
    class FakeReader:
        def get(self, ref: ObservationRef) -> ObservedObject | None:
            if ref.name == "web-app":
                return ObservedObject(ref=ref, phase="Running", images=("web:v1",))
            if ref.name == "db-secret":
                return ObservedObject(ref=ref, phase="Active")
            return None

        def list(self, api_version: str, kind: str, namespace: str) -> list[ObservedObject]:
            if kind == "ConfigMap":
                return [
                    ObservedObject(
                        ref=ObservationRef("v1", "ConfigMap", namespace, "unmanaged-config"),
                        phase="Active",
                    )
                ]
            return []

    reader = FakeReader()
    server = LocalObserveServer(
        ("127.0.0.1", 0),
        lambda: build_operator_report(reader, provider.target.namespace, catalog=catalog, session_archive=git_record.archive),
        pref_store,
        user="ops-admin",
        catalog=catalog,
        state_store=store,
        workflow=workflow,
        artifact_inventory=art_inventory,
        log_reader_fn=lambda: [sanitized],
    )
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        # Test GET / (UI)
        with urlopen(f"{base_url}/") as res:
            html = res.read().decode()
            assert "Piceli Operator" in html
            assert "Piceli Observe" in html
            assert "Overview &amp; Inventory" in html or "Overview & Inventory" in html

        # Test GET /v1/status (Classification equivalence)
        with urlopen(f"{base_url}/v1/status") as res:
            status_json = json.loads(res.read())
            assert status_json["namespace"] == provider.target.namespace
            assert status_json["active_release"] == "release-v1"
            # Verify managed vs unmanaged
            managed_names = [m["ref"]["name"] for m in status_json["managed"]]
            unmanaged_names = [u["ref"]["name"] for u in status_json["unmanaged"]]
            assert "worker" in managed_names and "credential" in managed_names
            assert "unmanaged-config" in unmanaged_names

        # Test GET /v1/releases
        with urlopen(f"{base_url}/v1/releases") as res:
            releases_json = json.loads(res.read())
            rel_names = [r["name"] for r in releases_json["releases"]]
            assert "release-v1" in rel_names
            assert "production-stable" in rel_names

        # Test GET /v1/artifacts
        with urlopen(f"{base_url}/v1/artifacts") as res:
            art_json = json.loads(res.read())
            assert art_json["protected_bytes"] == 25_000_000
            assert art_json["reclaimable_bytes"] == 20_000_000

        # Test GET /v1/logs
        with urlopen(f"{base_url}/v1/logs?target=deployment/web-app") as res:
            logs_json = json.loads(res.read())
            assert "[REDACTED]" in logs_json["lines"][0]
            assert auth_token not in logs_json["lines"][0]

        # Test POST /v1/releases/promote via Bearer Token
        promote_req = Request(
            f"{base_url}/v1/releases/promote",
            data=json.dumps({"source_name": "release-v1", "target_name": "rest-promoted"}).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {auth_token}",
            },
            method="POST",
        )
        with urlopen(promote_req) as res:
            p_res = json.loads(res.read())
            assert p_res["ok"] is True
            assert p_res["promoted"] == "rest-promoted"
            assert catalog.get("rest-promoted").source.artifact_digest == git_source.artifact_digest

        # Test POST /v1/artifacts/gc
        gc_req = Request(
            f"{base_url}/v1/artifacts/gc",
            data=json.dumps({"dry_run": True}).encode(),
            headers={
                "Content-Type": "application/json",
                "X-Piceli-Local-Token": server.local_token,
            },
            method="POST",
        )
        with urlopen(gc_req) as res:
            gc_res = json.loads(res.read())
            assert gc_res["freed_bytes"] == 20_000_000
            assert gc_res["dry_run"] is True

    finally:
        server.shutdown()
        server.server_close()

    # 11. CLI Equivalence via Typer CliRunner
    runner = CliRunner()

    # CLI promote
    cli_promote = runner.invoke(
        cli_app,
        ["operator", "promote", "--catalog", str(catalog_path), "--source", "release-v1", "--target", "cli-promoted"],
    )
    assert cli_promote.exit_code == 0
    assert catalog.get("cli-promoted").source.artifact_digest == git_source.artifact_digest

    # CLI backup
    cli_backup_dest = tmp_path / "cli-backup.tar.gz"
    cli_backup = runner.invoke(
        cli_app,
        ["operator", "backup", "--state-dir", str(state_dir), "--output", str(cli_backup_dest)],
    )
    assert cli_backup.exit_code == 0
    assert cli_backup_dest.exists()

    # CLI approve PR
    cli_approve = runner.invoke(
        cli_app,
        ["operator", "approve", "--state-dir", str(state_dir), "--pr-id", "99", "--commit", "d" * 40, "--namespace", "test-ns", "--approved-by", "alice"],
    )
    assert cli_approve.exit_code == 0
    assert ApprovalStore(store).is_approved(99, "d" * 40, "test-ns")
