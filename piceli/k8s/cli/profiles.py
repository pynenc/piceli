"""``piceli login``, ``profiles`` and ``logout``: named credential profiles.

A profile is a reference (the kubeconfig path and context) stored outside the
repository, see :mod:`piceli.profiles`. ``--profile NAME`` then replaces
``--kubeconfig FILE --context CTX`` on every command that takes them, and
replaces the declared target's credentials on commands that read a pipeline or
release spec (``deploy``, ``release``, ``status``, ``access``, ``env``,
``logs``).

Importing this module is side-effect free.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

import typer

from piceli.cli_contract import emit_json, reject, reject_error, say
from piceli.profiles import (
    ProfileError,
    list_profiles,
    remove_profile,
    resolve,
    save_profile,
    set_override,
)

NameArgument = Annotated[str, typer.Argument(help="Profile name, e.g. my-cluster")]


def login(
    name: NameArgument,
    kubeconfig: Annotated[
        Path,
        typer.Option(
            "--kubeconfig",
            help="Kubeconfig file the profile points at (a reference is stored, "
            "never its contents)",
        ),
    ],
    context: Annotated[
        str | None,
        typer.Option(
            "--context",
            help="Context in it (optional when the file defines exactly one; "
            "current-context is never used)",
        ),
    ] = None,
) -> None:
    """Store a credential profile outside the repository (mode 0600)."""
    try:
        stored = save_profile(name, kubeconfig, context)
    except ProfileError as error:
        reject_error(error, "profile-invalid")
    emit_json({"state": "stored", "profile": stored.describe()})
    say(f"piceli: stored profile {name!r} (use --profile {name} or Target.profile)")


def profiles(
    as_json: Annotated[
        bool, typer.Option("--json", help="Print one JSON object on stdout")
    ] = False,
) -> None:
    """List the stored credential profiles (references only, never secrets)."""
    found = [item.describe() for item in list_profiles()]
    if as_json:
        emit_json({"state": "listed", "profiles": found})
        return
    if not found:
        say(
            "piceli: no profiles; create one with `piceli login NAME --kubeconfig FILE`"
        )
    for item in found:
        flag = "" if item["kubeconfig_present"] else "  (kubeconfig file missing)"
        say(f"{item['name']}  {item['context']}  {item['kubeconfig']}{flag}")


def logout(name: NameArgument) -> None:
    """Delete a stored profile (never the kubeconfig file it points at)."""
    try:
        removed = remove_profile(name)
    except ProfileError as error:
        reject_error(error, "profile-invalid")
    emit_json({"state": "removed" if removed else "absent", "name": name})
    say(f"piceli: {'removed' if removed else 'no such'} profile {name!r}")


def register(root: typer.Typer) -> None:
    root.command("login")(login)
    root.command("profiles")(profiles)
    root.command("logout")(logout)


# ------------------------------------------------------------- --profile


def _profile_values(argv: Sequence[str]) -> tuple[list[str], str | None]:
    """``argv`` without ``--profile NAME`` / ``--profile=NAME``, and the name."""
    rest: list[str] = []
    name: str | None = None
    items = iter(range(len(argv)))
    for index in items:
        token = argv[index]
        if token == "--":
            rest.extend(argv[index:])
            break
        if token == "--profile":
            if index + 1 >= len(argv):
                reject("profile-invalid", "--profile needs a profile name")
            name = argv[index + 1]
            next(items)
        elif token.startswith("--profile="):
            name = token.split("=", 1)[1]
        else:
            rest.append(token)
    return rest, name


def _accepts_kubeconfig(argv: Sequence[str]) -> bool:
    """Whether the command ``argv`` names declares ``--kubeconfig`` and ``--context``."""
    from typer.main import get_command

    from piceli.k8s.cli import app

    command = get_command(app)
    for token in argv:
        if token.startswith("-"):
            continue
        children = getattr(command, "commands", None)
        if not children or token not in children:
            break
        command = children[token]
    options = {
        option
        for parameter in getattr(command, "params", ())
        for option in getattr(parameter, "opts", ())
    }
    return {"--kubeconfig", "--context"} <= options


def expand_profile_argv(argv: Sequence[str], *, artifacts: bool = False) -> list[str]:
    """Apply ``--profile NAME`` to a command line (before the parser sees it).

    A command that takes ``--kubeconfig`` and ``--context`` gets them from the
    profile (naming both is refused). Any other command (one that reads its
    target from a pipeline or release spec) uses the profile in place of the
    declared target's credentials, through :func:`piceli.profiles.set_override`.
    """
    rest, name = _profile_values(argv)
    if name is None:
        return list(argv)
    if any(
        token in {"--kubeconfig", "--context"}
        or token.startswith(("--kubeconfig=", "--context="))
        for token in rest
    ):
        reject(
            "profile-conflict",
            "--profile cannot be combined with --kubeconfig or --context",
        )
    if "--help" in rest:
        return rest
    try:
        found = resolve(name)
    except ProfileError as error:
        reject_error(error, "profile-invalid")
    if artifacts or _accepts_kubeconfig(rest):
        return [
            *rest,
            "--kubeconfig",
            str(found.kubeconfig),
            "--context",
            found.context,
        ]
    set_override(name)
    return rest


def main_argv() -> list[str]:
    """``sys.argv[1:]`` with any ``--profile`` applied."""
    return expand_profile_argv(
        sys.argv[1:], artifacts=bool(sys.argv[1:2] == ["artifacts"])
    )
