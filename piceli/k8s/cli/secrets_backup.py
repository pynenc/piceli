"""``piceli secrets backup|restore``: the secret store, encrypted with age (0.25.0).

- ``secrets backup --to PATH [--recipient R]... [--recipients-file F]
  [--force]`` streams every file of the store and the profiles into ``age``;
  the recipients are remembered for the next backup.
- ``secrets restore PATH [--identity FILE] [--approve DIGEST]`` decrypts,
  validates and restores; a store that is not empty gets a plan first (exit
  3).

Values are never printed. Machine output is one JSON object on stdout; human
text on stderr. Importing this module is side-effect free.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from piceli.cli_contract import EXIT_APPROVAL, emit_json, say


def backup_secrets(
    to: Annotated[
        Path, typer.Option("--to", help="The encrypted archive to write (age format)")
    ],
    recipient: Annotated[
        list[str] | None,
        typer.Option(
            "--recipient",
            help="An age (age1...), plugin (age1yubikey1...) or SSH public key; "
            "repeatable; remembered for later backups",
        ),
    ] = None,
    recipients_file: Annotated[
        Path | None,
        typer.Option(
            "--recipients-file", help="A file of recipients, one per line (age -R)"
        ),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Replace an existing backup file")
    ] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print one JSON object on stdout")
    ] = False,
) -> None:
    """Write the whole secret store, encrypted with age, to one file (never printed)."""
    from piceli.infra.machines import secrets_backup as sb
    from piceli.k8s.cli.infra import _guard

    with _guard():
        chosen = sb.parse_recipients(recipient or [], recipients_file)
        result = sb.backup(to, chosen, force=force)
    say(
        f"backed up {result['count']} files to {result['path']} for "
        f"{len(result['recipients'])} recipient(s); {result['sha256']}; no value "
        "was printed"
    )
    for item in result["skipped"]:
        say(f"  skipped {item['area']}/{item['path']}: {item['why']}")
    for report in result["incomplete"]:
        say(f"  incomplete: {report['message']}")
    if len(result["recipients"]) < 2:
        say(
            "  one recipient: add a second (a hardware key) so one lost key is not fatal"
        )
    emit_json(result)


def restore_secrets(
    path: Annotated[
        Path, typer.Argument(help="The encrypted archive", show_default=False)
    ],
    identity: Annotated[
        Path | None,
        typer.Option(
            "--identity",
            help="An age identity file (age -i); without it age asks for a "
            "passphrase or uses its plugins",
        ),
    ] = None,
    approve: Annotated[
        str | None,
        typer.Option("--approve", help="The plan digest, to restore into a used store"),
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print one JSON object on stdout")
    ] = False,
) -> None:
    """Restore the secret store from an encrypted backup (plans first when not empty)."""
    from piceli.infra.machines import secrets_backup as sb
    from piceli.k8s.cli.infra import _guard

    with _guard():
        result = sb.restore(path, identity, approve)
    groups = (("add", "add"), ("identical", "identical"), ("differ", "replace"))
    if result["state"] == "approval-required":
        for key, verb in groups:
            for entry in result[key]:
                say(f"  {verb:9} {entry['area']}/{entry['name']} ({entry['kind']})")
        command = f"piceli secrets restore {path}"
        if identity is not None:
            command += f" --identity {identity}"
        command += f" --approve {result['digest']}"
        say(f"the store is not empty; nothing was written; to restore: {command}")
        emit_json({**result, "approve_command": command})
        raise typer.Exit(EXIT_APPROVAL)
    say(
        f"restored {len(result['added'])} new and {len(result['replaced'])} replaced "
        f"files ({len(result['identical'])} identical) into {result['credentials_dir']}; "
        "no value was printed"
    )
    emit_json(result)


def register(secrets_app: typer.Typer) -> None:
    secrets_app.command("backup")(backup_secrets)
    secrets_app.command("restore")(restore_secrets)
