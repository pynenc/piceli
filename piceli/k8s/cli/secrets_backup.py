"""``piceli secrets backup|restore``: the secret store, encrypted with age (0.25.0).

- ``secrets backup --to PATH [--recipient R]... [--recipients-file F]
  [--force] [--single-recipient]`` streams every file of the store and the
  profiles into ``age``; the recipients are remembered for the next backup.
  At least two recipients, unless ``--single-recipient`` (recorded).
- ``secrets backup-key --out PATH [--add] [--force]`` creates the offline
  age key of the recommended pair, writes it to PATH (``0600``) and prints
  only its ``age1…`` recipient.
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
    single_recipient: Annotated[
        bool,
        typer.Option(
            "--single-recipient",
            help="Allow one recipient (losing it loses the store and the backup "
            "together); recorded in the backup and its marker",
        ),
    ] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print one JSON object on stdout")
    ] = False,
) -> None:
    """Write the whole secret store, encrypted with age, to one file (never printed).

    It needs at least two recipients, kept apart: an age key whose identity
    lives in your password manager, plus an offline age key (piceli secrets
    backup-key --out PATH --add: paper or USB) or an age-plugin-yubikey
    identity. With one, losing that key loses the store and its backup
    together: it is refused unless --single-recipient.
    """
    from piceli.infra.machines import secrets_backup as sb
    from piceli.k8s.cli.infra import _guard

    with _guard():
        chosen = sb.parse_recipients(recipient or [], recipients_file)
        result = sb.backup(to, chosen, force=force, single_recipient=single_recipient)
    say(
        f"backed up {result['count']} files to {result['path']} for "
        f"{len(result['recipients'])} recipient(s); {result['sha256']}; no value "
        "was printed"
    )
    for item in result["skipped"]:
        say(f"  skipped {item['area']}/{item['path']}: {item['why']}")
    for report in result["incomplete"]:
        say(f"  incomplete: {report['message']}")
    if result["single_recipient"]:
        say(
            "  single recipient (--single-recipient, recorded): losing that key loses "
            "the store and this backup; add an offline key with piceli secrets "
            "backup-key --out PATH --add"
        )
    emit_json(result)


def backup_key(
    out: Annotated[
        Path,
        typer.Option(
            "--out",
            help="Where to write the private key (outside the store; offline: a "
            "USB drive, or print it and delete the file)",
        ),
    ],
    add: Annotated[
        bool,
        typer.Option("--add", help="Add its recipient to the remembered recipients"),
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Replace an existing file at --out")
    ] = False,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print one JSON object on stdout")
    ] = False,
) -> None:
    """Create the offline age key for backups: prints only its age1... recipient.

    The private key is written to --out (mode 0600; refused inside the store,
    and over an existing file without --force) and never printed. It is the
    only copy: keep it offline (paper or USB), away from this machine and the
    password manager that holds the other recipient's identity. --add puts
    its recipient among those the next piceli secrets backup uses.
    """
    from piceli.infra.machines import secrets_backup as sb
    from piceli.k8s.cli.infra import _guard

    with _guard():
        result = sb.generate_backup_key(out, force=force, add=add)
    say(f"wrote the private key to {result['path']} (0600); it was not printed")
    say(
        "  it is the only copy: keep it offline (print it, or move it to a USB "
        "drive) and delete it from this machine"
    )
    say(f"recipient: {result['recipient']}")
    if add:
        say(f"  remembered recipients: {len(result['recipients'])}")
    else:
        say("  add it to backups: --recipient with it, or run again with --add")
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
    secrets_app.command("backup-key")(backup_key)
    secrets_app.command("restore")(restore_secrets)
