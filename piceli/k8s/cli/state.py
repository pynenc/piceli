"""``piceli state``: show, pull, export and import the deployment state of a release.

``--spec`` names the release as ``piceli release`` does: a ``release.toml``
path or ``MODULE:ATTR`` naming a :class:`~piceli.pipeline.Pipeline`. With
``state = "cluster"`` the state lives in the release namespace (see
:mod:`piceli.state`); with ``local`` it is the state directory.

Output follows the CLI contract: one JSON object on stdout, human text on
stderr; exit ``0`` success, ``2`` rejected, ``3`` approval required (an
import prints the digest to approve).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from piceli.cli_contract import EXIT_APPROVAL, emit_json, rejecting, say

app = typer.Typer(
    rich_markup_mode=None,
    help=(
        "Show, pull, export and import the deployment state of a release "
        "(local directory or shared in the cluster)."
    ),
    no_args_is_help=True,
)

SpecOption = Annotated[
    str,
    typer.Option(
        "--spec",
        help="path/to/release.toml, or MODULE:ATTR naming a piceli Pipeline",
        show_default=False,
    ),
]


def _scope(spec: str) -> Any:
    from piceli.k8s.cli.release import is_pipeline_target
    from piceli.k8s.release_spec import ReleaseSpec, ReleaseSpecError
    from piceli.state.scopes import pipeline_scope, release_scope

    if is_pipeline_target(spec):
        from piceli.k8s.cli.deploy_pipeline import load_pipeline

        return pipeline_scope(load_pipeline(spec))
    path = Path(spec).expanduser()
    if not path.is_file():
        raise ReleaseSpecError(f"release spec not found: {spec}")
    return release_scope(ReleaseSpec.from_toml(path))


def _rules() -> tuple[tuple[Any, str], ...]:
    from piceli.state.snapshot import SnapshotError

    return (
        (SnapshotError, "state-corrupt"),
        ((ValueError, OSError), "state-unavailable"),
    )


def _store(scope: Any) -> Any:
    from piceli.state.backend import STORE_FACTORY

    return STORE_FACTORY[0](scope)


@app.command("show")
def show(spec: SpecOption) -> None:
    """Show where the state lives, its generation and who holds the release lock.

    Read-only; never prints state content.
    """
    from piceli.state.snapshot import members

    with rejecting(*_rules()):
        scope = _scope(spec)
        value: dict[str, Any] = {
            "state": "shown",
            "backend": scope.settings.backend,
            "release": scope.name,
            "namespace": scope.namespace,
            "layout": scope.layout,
            "local_files": len(members(scope.directory)),
        }
        if scope.settings.backend == "cluster":
            store = _store(scope)
            try:
                _head, manifest = store.head()
                value["lock"] = store.describe_lock()
                value["lease_seconds"] = scope.settings.lease_seconds
                value["shared"] = (
                    None
                    if manifest is None
                    else {
                        key: manifest.get(key)
                        for key in (
                            "generation",
                            "bytes",
                            "files",
                            "sha256",
                            "updated_at",
                        )
                    }
                    | {"chunks": len(manifest.get("chunks", []))}
                )
            finally:
                store.close()
    lock = value.get("lock")
    say(
        f"state of {scope.name!r}: {value['backend']}"
        + (
            f", generation {value['shared']['generation']}"
            if value.get("shared")
            else ""
        )
        + (f", locked by {lock['holder']} ({lock['expires_in']}s)" if lock else "")
    )
    emit_json(value)


@app.command("pull")
def pull(spec: SpecOption) -> None:
    """Refresh the local working copy from the shared state (reads the cluster).

    With ``local`` state there is nothing to pull. Skipped while a run on
    this machine holds the working copy (it is the freshest).
    """
    from piceli.state import session
    from piceli.state.snapshot import members

    with rejecting(*_rules()):
        scope = _scope(spec)
        with session(scope, write=False, say=say) as held:
            described = held.describe()
            generation = getattr(held, "manifest", None) or {}
        value = {
            "state": "pulled" if scope.settings.backend == "cluster" else "local",
            "backend": described["backend"],
            "generation": generation.get("generation"),
            "local_files": len(members(scope.directory)),
        }
    say(f"state of {scope.name!r}: {value['state']}")
    emit_json(value)


def _current_snapshot(scope: Any) -> tuple[bytes, str]:
    """The state to export: the shared snapshot, or the local directory."""
    from piceli.state.snapshot import pack

    if scope.settings.backend == "cluster":
        store = _store(scope)
        try:
            data, _manifest = store.pull()
        finally:
            store.close()
        if data is not None:
            return data, "cluster"
    data, _names = pack(scope.directory)
    return data, "local"


@app.command("export")
def export(
    spec: SpecOption,
    out: Annotated[Path, typer.Option("--out", help="The export file to write")],
    include_secrets: Annotated[
        bool,
        typer.Option(
            "--include-secrets",
            help="Also export the secret store, stored discovery, journals and "
            "backups, encrypted with --key-file (left out otherwise)",
        ),
    ] = False,
    key_file: Annotated[
        Path | None,
        typer.Option(
            "--key-file",
            help="Owner-only file with the encryption key (at least 32 characters)",
        ),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite an existing --out file")
    ] = False,
) -> None:
    """Write the release's state to one file (secret material excluded unless asked).

    Reads the shared state (or the local directory); changes nothing.
    """
    from piceli.pipeline.journal import write_private
    from piceli.state.archive import export_document, read_key
    from piceli.state.errors import StateError

    with rejecting(*_rules()):
        if out.exists() and not force:
            raise StateError("state-output-exists", "the export file exists")
        if include_secrets and key_file is None:
            raise StateError("state-key-required", "--include-secrets needs --key-file")
        key = read_key(key_file) if include_secrets and key_file else None
        scope = _scope(spec)
        snapshot, source = _current_snapshot(scope)
        document = export_document(scope, snapshot, key=key, source=source)
        write_private(out, json.dumps(document, sort_keys=True) + "\n")
    private = document["private"]
    say(
        f"exported the {source} state of {scope.name!r}: "
        f"{len(document['files'])} file(s), {len(private['files'])} private "
        f"file(s) {private['state']}"
    )
    emit_json(
        {
            "state": "exported",
            "source": source,
            "files": len(document["files"]),
            "private": {"state": private["state"], "files": len(private["files"])},
            "public_sha256": document["public_sha256"],
        }
    )


@app.command("import")
def import_(
    spec: SpecOption,
    in_: Annotated[
        Path, typer.Option("--in", help="The export file (from piceli state export)")
    ],
    key_file: Annotated[
        Path | None,
        typer.Option(
            "--key-file", help="The key the export's secrets were encrypted with"
        ),
    ] = None,
    allow_partial: Annotated[
        bool,
        typer.Option(
            "--allow-partial",
            help="Import an export without secret material (the next release "
            "generates new secret values)",
        ),
    ] = False,
    approve: Annotated[
        str | None,
        typer.Option("--approve", help="The import digest printed without --approve"),
    ] = None,
) -> None:
    """Replace the release's state with an export (needs --approve DIGEST).

    Without ``--approve`` nothing changes: it prints what the import replaces
    and the digest to approve (exit 3). With shared state it holds the release
    lock and writes the state to the cluster.
    """
    from piceli.state import session
    from piceli.state.archive import import_digest, load_export, read_key, snapshot_of
    from piceli.state.errors import StateError
    from piceli.state.snapshot import entries, unpack

    with rejecting(*_rules()):
        scope = _scope(spec)
        document, file_sha = load_export(in_)
        key = read_key(key_file) if key_file is not None else None
        data = snapshot_of(scope, document, key=key, allow_partial=allow_partial)
        digest = import_digest(scope, file_sha)
        summary = {
            "backend": scope.settings.backend,
            "files": len(list(entries(data))),
            "private": document["private"]["state"],
            "import_digest": digest,
        }
        if approve is None:
            say(
                f"import into the {scope.settings.backend} state of {scope.name!r}: "
                f"{summary['files']} file(s) replace the current state"
            )
            say(f"approve with: --approve {digest}")
            emit_json({"state": "approval-required", **summary})
            raise typer.Exit(EXIT_APPROVAL)
        if approve != digest:
            raise StateError(
                "state-import-changed", "the approved digest is not this import's"
            )
        with session(scope, write=True, say=say) as held:
            unpack(data, scope.directory)
            held.checkpoint(force=True)
            described = held.describe()
    say(f"imported {summary['files']} file(s) into the state of {scope.name!r}")
    emit_json(
        {"state": "imported", **summary, "generation": described.get("generation")}
    )
