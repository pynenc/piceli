"""Release candidates on the command line (0.83.0).

- ``piceli gitops candidate request COMMIT [--env ENV]``: prepare the
  candidate of a commit now, without the quiet time (the controller copies
  the images of the run that passed it);
- ``piceli gitops candidate release COMMIT``: mark a ready candidate
  released, so it stays whatever ``keep`` says;
- ``piceli gitops candidate list [--json]``: each candidate, newest first:
  ``candidate abc1234: ready`` or ``candidate abc1234: not ready: <reason>``.

The controller declares them with ``Controller(candidate=Candidate(...))``
(:mod:`piceli.gitops.candidates`); ``request`` and ``release`` use the
request inbox as ``gitops approve`` does, ``list`` reads the published
status. Importing this module is side-effect free.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from piceli.cli_contract import emit_json, say
from piceli.k8s.cli.gitops import (
    ContextOption,
    JsonOption,
    KubeconfigOption,
    NamespaceOption,
    StateDirOption,
    TransportOption,
    _channel,
    _guard,
    app,
    status_body,
)

CANDIDATES_SCHEMA = "piceli.gitops-candidates.v1"

candidate_app = typer.Typer(
    help="Release candidates: the images of a passed commit, copied out.",
    no_args_is_help=True,
)
app.add_typer(candidate_app, name="candidate")

CommitArgument = Annotated[
    str, typer.Argument(help="A commit (7 to 40 hexadecimal characters)")
]


def _send(
    action: str,
    commit: str,
    env: str | None,
    kubeconfig: Path | None,
    context: str | None,
    namespace: str,
    state_dir: Path | None,
    transport: str,
) -> tuple[str, str]:
    from piceli.gitops.state import candidate_request

    with _guard():
        key, body = candidate_request(action, commit, env)
    with _channel(state_dir, kubeconfig, context, namespace, transport) as channel:
        with _guard():
            channel.add_request(key, body)
    return key, str(body["commit"])


@candidate_app.command("request")
def request(
    commit: CommitArgument,
    env: Annotated[
        str | None,
        typer.Option(
            "--env", help="The candidate's environment (the controller has one)"
        ),
    ] = None,
    kubeconfig: KubeconfigOption = None,
    context: ContextOption = None,
    namespace: NamespaceOption = "piceli-system",
    state_dir: StateDirOption = None,
    transport: TransportOption = "https",
) -> None:
    """Prepare the candidate of COMMIT now: no quiet time to wait for."""
    key, found = _send(
        "request", commit, env, kubeconfig, context, namespace, state_dir, transport
    )
    say(
        f"candidate {found[:7]} requested; the controller copies its images on its "
        "next poll (piceli gitops candidate list)"
    )
    emit_json({"state": "requested", "request": key, "commit": found})


@candidate_app.command("release")
def release(
    commit: CommitArgument,
    env: Annotated[
        str | None,
        typer.Option(
            "--env", help="The candidate's environment (the controller has one)"
        ),
    ] = None,
    kubeconfig: KubeconfigOption = None,
    context: ContextOption = None,
    namespace: NamespaceOption = "piceli-system",
    state_dir: StateDirOption = None,
    transport: TransportOption = "https",
) -> None:
    """Mark the ready candidate of COMMIT released: it stays, with its digests."""
    key, found = _send(
        "release", commit, env, kubeconfig, context, namespace, state_dir, transport
    )
    say(
        f"candidate {found[:7]} marked released on the controller's next poll "
        "(only a ready candidate can be)"
    )
    emit_json({"state": "requested", "request": key, "commit": found})


@candidate_app.command("list")
def list_candidates(
    kubeconfig: KubeconfigOption = None,
    context: ContextOption = None,
    namespace: NamespaceOption = "piceli-system",
    state_dir: StateDirOption = None,
    transport: TransportOption = "https",
    as_json: JsonOption = False,
) -> None:
    """Each candidate the controller holds, newest first, and its state."""
    from piceli.gitops.candidates import describe

    with _channel(state_dir, kubeconfig, context, namespace, transport) as channel:
        with _guard():
            body = status_body(channel, namespace)
    found = list(body.get("candidates") or ())
    for item in found:
        say(describe(item))
    if not found:
        say("no candidates")
    if as_json:
        emit_json({"schema": CANDIDATES_SCHEMA, "candidates": found})
