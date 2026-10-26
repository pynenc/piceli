"""``piceli secrets signing-key`` and ``piceli sign-file`` (0.32.0).

A release signing key (ssh-ed25519) kept in the local secret store, and
signatures of files that ``ssh-keygen -Y verify`` checks
(:mod:`piceli.artifacts.file_signing`). Neither command prints a private key.
Importing this module is side-effect free.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from piceli.cli_contract import emit_json, reject, say


def signing_key_secret(
    context: typer.Context,
    name: Annotated[str, typer.Argument(help="Credential name, e.g. release-signing")],
    generate: Annotated[
        bool, typer.Option("--generate", help="Generate a new ssh-ed25519 key")
    ] = False,
    file: Annotated[
        Path | None,
        typer.Option(
            "--file",
            help="Import an existing ed25519 private key file (OpenSSH, no passphrase)",
        ),
    ] = None,
    comment: Annotated[
        str | None,
        typer.Option(
            "--comment",
            help="The public key's comment and the allowed_signers principal "
            "(default: NAME)",
        ),
    ] = None,
    replace: Annotated[
        bool,
        typer.Option(
            "--replace",
            help="Replace an existing key (verifiers that trust the old one "
            "refuse new signatures)",
        ),
    ] = False,
) -> None:
    """A release signing key: generate or import it; print its public key."""
    from piceli.artifacts.file_signing import (
        KIND,
        SigningError,
        allowed_signers_line,
        generate_private_key,
        load_signing_key,
        save_signing_key,
    )
    from piceli.infra.machines.credentials import credential_present
    from piceli.infra.machines.model import InfraError
    from piceli.k8s.cli.infra import _guard, _no_secret_arguments

    _no_secret_arguments(context)
    if generate and file is not None:
        reject("infra-prompt-required", "pass one of --generate or --file")
    label = comment or name
    with _guard():
        try:
            if not generate and file is None:
                public = load_signing_key(name)[1]
                state = "present"
            else:
                if credential_present(name) and not replace:
                    reject(
                        "infra-credential-exists",
                        f"credential {name!r} exists; verifiers trust its public "
                        "key (--replace if you mean it)",
                    )
                if generate:
                    private = generate_private_key()
                else:
                    assert file is not None
                    try:
                        private = file.expanduser().read_text()
                    except (OSError, UnicodeDecodeError):
                        reject("infra-credential-invalid", "the key file is unreadable")
                public = save_signing_key(name, private, label)
                del private
                state = "stored"
            principal = public.split()[2] if len(public.split()) > 2 else label
            signers = allowed_signers_line(public, principal)
        except (SigningError, InfraError) as error:
            reject(error.code, str(error))
    say(f"credential {name} ({KIND}); the private key was not printed")
    say(f"public key: {public}")
    say(f"allowed_signers: {signers}")
    emit_json(
        {
            "state": state,
            "name": name,
            "kind": KIND,
            "public_key": public,
            "allowed_signers": signers,
        }
    )


def sign_file_command(
    files: Annotated[list[Path], typer.Argument(help="The files to sign")],
    key: Annotated[
        str | None,
        typer.Option("--key", help="The stored signing key's credential name"),
    ] = None,
    agent: Annotated[
        Path | None,
        typer.Option(
            "--agent",
            help="Sign with an SSH agent ($SSH_AUTH_SOCK) holding the key of "
            "this public key file",
        ),
    ] = None,
    agent_socket: Annotated[
        Path | None,
        typer.Option(
            "--agent-socket", help="The agent's socket (default: $SSH_AUTH_SOCK)"
        ),
    ] = None,
    namespace: Annotated[
        str, typer.Option("--namespace", help="The signature namespace (-n)")
    ] = "file",
) -> None:
    """Write FILE.sig for each file, as `ssh-keygen -Y sign -n file` would.

    The key is a stored one (--key NAME, `piceli secrets signing-key`) or one
    an SSH agent holds (--agent KEY.pub). The private key is never printed.
    """
    from piceli.artifacts.file_signing import (
        SigningError,
        load_signing_key,
        parse_public_key,
        sign_file,
    )
    from piceli.infra.machines.model import InfraError

    if (key is None) == (agent is None):
        reject("sign-file-invalid", "pass one of --key NAME or --agent KEY.pub")
    if agent_socket is not None and agent is None:
        reject("sign-file-invalid", "--agent-socket needs --agent")
    if not files:
        reject("sign-file-invalid", "give the files to sign")
    signed: list[dict[str, Any]] = []
    try:
        if key is not None:
            private, public = load_signing_key(key)
            for path in files:
                signed.append(sign_file(path, key=private, namespace=namespace))
            del private
        else:
            assert agent is not None
            try:
                public = agent.expanduser().read_text().strip()
            except (OSError, UnicodeDecodeError):
                reject("sign-key-invalid", "the public key file is unreadable")
            parse_public_key(public)
            for path in files:
                signed.append(
                    sign_file(
                        path,
                        agent_key=public,
                        agent_socket=agent_socket,
                        namespace=namespace,
                    )
                )
    except (SigningError, InfraError) as error:
        reject(error.code, str(error))
    for item in signed:
        say(f"signed {item['file']} -> {item['signature']}")
    emit_json(
        {
            "state": "signed",
            "namespace": namespace,
            "public_key": public,
            "source": "store" if key is not None else "agent",
            "files": signed,
        }
    )


def register(root: typer.Typer) -> None:
    from piceli.k8s.cli.cluster import secrets_app

    settings: dict[str, Any] = {
        "allow_extra_args": True,
        "ignore_unknown_options": True,
    }
    secrets_app.command("signing-key", context_settings=settings)(signing_key_secret)
    root.command("sign-file")(sign_file_command)
