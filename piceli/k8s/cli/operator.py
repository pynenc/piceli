"""Piceli Operator CLI: unified operations for status, releases, artifacts, automation, and backup.

Enforces identical authorization and operations across Library, CLI, versioned REST, and UI.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated, Any

import typer

from piceli.k8s.automation import (
    ApprovalStore,
    PRApproval,
    promote_release,
)
from piceli.k8s.cli.observe import (
    UI_CONFIG_HELP,
    bind_local_server,
    serve_until_interrupted,
)
from piceli.k8s.observe import (
    ForwardSupervisor,
    KubernetesDynamicInventoryReader,
    PreferenceStore,
)
from piceli.k8s.observe_server import LocalObserveServer
from piceli.k8s.operator import build_operator_report
from piceli.k8s.operator_state import FileStateStore
from piceli.k8s.ops.session import DeploymentSessionArchive
from piceli.k8s.release import ReleaseCatalog
from piceli.k8s.ui_config import UI_CONFIG_ENV, load_ui_config

app = typer.Typer(
    help="Piceli Operator commands for reactive delivery, inventory, and artifacts."
)


MaybePath = Path | None
MaybeString = str | None


def _archive(path: Path) -> DeploymentSessionArchive:
    return DeploymentSessionArchive.from_json(path.read_text())


@app.command("status")
def status(
    kubeconfig: Annotated[Path, typer.Option(exists=True, readable=True)],
    namespace: Annotated[str, typer.Option()] = "default",
    context: Annotated[MaybeString, typer.Option()] = None,
    archive: Annotated[MaybePath, typer.Option(exists=True, readable=True)] = None,
    catalog: Annotated[MaybePath, typer.Option(exists=True, readable=True)] = None,
    include_common_types: Annotated[bool, typer.Option()] = True,
) -> None:
    """Print classified operator inventory: managed, unmanaged, unknown, and releases."""
    reader = KubernetesDynamicInventoryReader(kubeconfig=kubeconfig, context=context)
    cat = ReleaseCatalog(catalog) if catalog else None
    arch = _archive(archive) if archive else None

    report = build_operator_report(
        reader,
        namespace,
        catalog=cat,
        session_archive=arch,
        include_common_types=include_common_types,
    )
    typer.echo(json.dumps(report.to_dict(), sort_keys=True, indent=2))


@app.command("promote")
def promote(
    catalog: Annotated[Path, typer.Option(exists=True, readable=True)],
    source: Annotated[str, typer.Option(help="Source release name to promote")],
    target: Annotated[str, typer.Option(help="Target tag/release name")],
) -> None:
    """Promote an existing built digest to a new tag without rebuilding."""
    cat = ReleaseCatalog(catalog)
    rec = promote_release(cat, source, target)
    typer.echo(
        json.dumps(
            {
                "promoted": rec.name,
                "artifact_digest": rec.source.artifact_digest,
                "namespace": rec.namespace,
            },
            sort_keys=True,
            indent=2,
        )
    )


@app.command("approve")
def approve(
    state_dir: Annotated[Path, typer.Option()],
    pr_id: Annotated[int, typer.Option()],
    commit: Annotated[str, typer.Option()],
    namespace: Annotated[str, typer.Option()],
    approved_by: Annotated[str, typer.Option()],
) -> None:
    """Record an explicit operator approval for a pull request rollout."""
    store = FileStateStore(state_dir)
    approvals = ApprovalStore(store)
    approval = PRApproval(
        pr_id=pr_id,
        commit_hash=commit,
        approved_by=approved_by,
        target_namespace=namespace,
    )
    approvals.record_approval(approval)
    typer.echo(
        json.dumps(
            {
                "approved": True,
                "pr_id": pr_id,
                "commit": commit,
                "namespace": namespace,
                "approved_by": approved_by,
            },
            sort_keys=True,
            indent=2,
        )
    )


@app.command("backup")
def backup(
    state_dir: Annotated[Path, typer.Option()],
    output: Annotated[Path, typer.Option(help="Target .tar.gz archive path")],
) -> None:
    """Create a verified, mode-restricted backup archive of operator state."""
    store = FileStateStore(state_dir)
    res = store.create_backup(output)
    typer.echo(json.dumps({"backup_created": str(res)}, sort_keys=True))


@app.command("restore")
def restore(
    archive_file: Annotated[Path, typer.Option(exists=True, readable=True)],
    destination: Annotated[Path, typer.Option(help="Target directory to restore into")],
) -> None:
    """Safely verify and restore operator state into empty destination."""
    store = FileStateStore(destination)
    store.restore_backup(archive_file, destination=destination)
    typer.echo(json.dumps({"restored_to": str(destination)}, sort_keys=True))


@app.command("serve")
def serve(
    kubeconfig: Annotated[Path, typer.Option(exists=True, readable=True)],
    namespace: Annotated[str, typer.Option()] = "default",
    context: Annotated[MaybeString, typer.Option()] = None,
    archive: Annotated[MaybePath, typer.Option(exists=True, readable=True)] = None,
    catalog: Annotated[MaybePath, typer.Option(exists=True, readable=True)] = None,
    preferences: Annotated[MaybePath, typer.Option()] = None,
    state_dir: Annotated[MaybePath, typer.Option()] = None,
    user: Annotated[MaybeString, typer.Option()] = None,
    port: Annotated[int, typer.Option(min=1, max=65535)] = 9876,
    ui_config: Annotated[
        MaybePath,
        typer.Option(
            exists=True, readable=True, envvar=UI_CONFIG_ENV, help=UI_CONFIG_HELP
        ),
    ] = None,
) -> None:
    """Launch the Piceli Operator dashboard and unified REST API."""
    config = load_ui_config(ui_config)
    reader = KubernetesDynamicInventoryReader(kubeconfig=kubeconfig, context=context)
    cat = ReleaseCatalog(catalog) if catalog else None
    arch = _archive(archive) if archive else None
    pref_store = PreferenceStore(preferences)
    file_state = FileStateStore(state_dir) if state_dir else None

    effective_user = user or os.environ.get("USER") or "operator"
    supervisor = ForwardSupervisor(
        preferences=pref_store,
        user=effective_user,
        kubeconfig=kubeconfig,
        context=context,
        shortcuts=config.shortcuts,
        namespace=namespace,
    )
    supervisor.restore()

    def report_fn() -> Any:
        return build_operator_report(
            reader,
            namespace,
            catalog=cat,
            session_archive=arch,
            managed_labels=config.inventory.managed_labels,
            revision_label=config.inventory.revision_label,
        )

    server = bind_local_server(
        lambda: LocalObserveServer(
            ("127.0.0.1", port),
            report_fn,
            pref_store,
            supervisor=supervisor,
            user=effective_user,
            catalog=cat,
            state_store=file_state,
            namespace=namespace,
            kubeconfig=kubeconfig,
            context=context,
            ui_config=config,
        ),
        port,
        supervisor,
    )

    typer.echo(json.dumps({"address": f"http://127.0.0.1:{port}", "operator": True}))
    serve_until_interrupted(server, supervisor)
