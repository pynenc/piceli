"""``piceli host``: Nix host releases (0.24.0).

``piceli host module MODULE:ATTR`` renders the NixOS module of a
:class:`~piceli.hosts.NixHost` (the agent, its configuration, a service and
a timer); ``piceli host status`` and ``piceli host wait`` read the agent's
status over SSH, with no cluster involved.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated, Any

import typer

from piceli.cli_contract import emit_json, fail, reject, say
from piceli.k8s.cli.cluster import JsonOption

host_app = typer.Typer(
    rich_markup_mode=None,
    help="Nix host releases: the host agent's module, status and wait (0.24.0).",
    no_args_is_help=True,
)

TargetArgument = Annotated[
    str,
    typer.Argument(
        help="MODULE:ATTR of a piceli.infra.NixHost, or an SSH target "
        "(ssh://user@host:port, user@host, host)",
        show_default=False,
    ),
]
InfraOption = Annotated[
    str | None,
    typer.Option(
        "--infra",
        help="MODULE:ATTR of the Infrastructure whose applied state has the "
        "server's address (when its Ssh has no address=)",
    ),
]


def load_host(ref: str) -> Any:
    """The ``NixHost`` that ``ref`` (``MODULE:ATTR``) names."""
    from piceli.app.render import load_target
    from piceli.cli_contract import describe_user_error
    from piceli.hosts.model import HostError, NixHost

    try:
        value = load_target(ref, Path.cwd())
    except HostError as error:
        reject(error.code, str(error))
    except Exception as error:
        reject("host-not-found", f"loading {ref} failed: {describe_user_error(error)}")
    if not isinstance(value, NixHost):
        reject("host-not-found", f"{ref} is not a piceli.infra.NixHost")
    return value


def _is_ref(text: str) -> bool:
    head, sep, attr = text.rpartition(":")
    return (
        bool(sep and head)
        and not text.startswith("ssh://")
        and attr.replace(".", "_").isidentifier()
    )


def _target(text: str, infra_ref: str | None) -> tuple[Any, Any, Path | None]:
    """``(NixHost or None, SshTarget, known_hosts)`` of the argument."""
    from piceli.hosts.model import HostError, SshTarget

    if not _is_ref(text):
        try:
            return None, SshTarget.parse(text), None
        except HostError as error:
            reject(error.code, str(error))
    host = load_host(text)
    address, known = None, None
    if infra_ref is not None and not isinstance(host.server, str):
        from piceli.infra.machines.install import server_values
        from piceli.infra.machines.model import InfraError
        from piceli.infra.machines.register import KNOWN_HOSTS
        from piceli.infra.machines.state import state_dir
        from piceli.k8s.cli.infra import load_infra

        infra = load_infra(infra_ref)
        try:
            directory = state_dir(infra.name, infra.state_dir, create=False)
            address = server_values(infra, directory, host.server).get("address")
        except InfraError as error:
            reject(error.code, str(error))
        if (directory / KNOWN_HOSTS).is_file():
            known = directory / KNOWN_HOSTS
    try:
        return host, host.ssh_target(address), known
    except HostError as error:
        reject(error.code, str(error))


@host_app.command("module")
def module(
    ref: Annotated[
        str,
        typer.Argument(
            help="MODULE:ATTR of a piceli.infra.NixHost", show_default=False
        ),
    ],
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            help="Write the module to this file (default: print it on stdout)",
        ),
    ] = None,
) -> None:
    """Render the NixOS module that installs the host agent (commit it, import it)."""
    import hashlib

    from piceli.hosts.module import render_module

    host = load_host(ref)
    text = render_module(host, ref)
    if output is None:
        typer.echo(text, nl=False)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    digest = "sha256:" + hashlib.sha256(text.encode()).hexdigest()
    say(f"wrote {output}: import it from {host.name}'s NixOS configuration")
    emit_json(
        {
            "state": "written",
            "host": host.name,
            "build": host.build,
            "path": str(output),
            "sha256": digest,
        }
    )


def _describe(entry: Any) -> str:
    if not isinstance(entry, dict):
        return "unknown"
    commit = str(entry.get("commit") or "")[:12] or "not a published build"
    tag = f" ({entry['tag']})" if entry.get("tag") else ""
    return f"{commit}{tag} {entry.get('store_path') or ''}".strip()


def _say_status(status: dict[str, Any]) -> None:
    say(f"{status.get('host')}: current {_describe(status.get('current'))}")
    poll = status.get("poll") or {}
    say(f"  last poll {poll.get('at')}: {poll.get('state')} {poll.get('reason') or ''}")
    attempt = status.get("last_attempt")
    if isinstance(attempt, dict):
        stages = ", ".join(
            f"{s.get('name')} {s.get('state')} {s.get('seconds')}s"
            for s in attempt.get("stages") or ()
        )
        say(f"  last attempt {_describe(attempt)}: {attempt.get('state')}")
        say(f"    {stages}")
        for check in attempt.get("checks") or ():
            say(f"    check {check.get('name')}: {check.get('state')}")
        if attempt.get("rollback"):
            say(f"    rolled back: {attempt['rollback'].get('state')}")


@host_app.command("status")
def status(
    target: TargetArgument,
    infra: InfraOption = None,
    as_json: JsonOption = False,
) -> None:
    """Read the host agent's status over SSH (current build, last attempt, checks)."""
    from piceli.hosts.model import HostError
    from piceli.hosts.remote import read_status

    _, ssh, known = _target(target, infra)
    try:
        body = read_status(ssh, known_hosts=known)
    except HostError as error:
        reject(error.code, str(error))
    _say_status(body)
    if as_json:
        emit_json({**body, "target": ssh.address})


@host_app.command("wait")
def wait(
    target: TargetArgument,
    rev: Annotated[
        str,
        typer.Argument(help="A commit (7+ hex digits) or a tag", show_default=False),
    ],
    infra: InfraOption = None,
    timeout: Annotated[
        int, typer.Option("--timeout", help="Seconds to wait", min=1)
    ] = 1800,
    interval: Annotated[
        int, typer.Option("--interval", help="Seconds between reads", min=1)
    ] = 10,
    published_after: Annotated[
        str | None,
        typer.Option(
            "--published-after",
            help="Judge only attempts of a publication at or after this "
            "published_at (from dev nix build --publish --json): an earlier "
            "failure of the same commit is not reported",
        ),
    ] = None,
    as_json: JsonOption = False,
) -> None:
    """Wait until the host runs REV with its checks passed (exit 1: failed or timeout)."""
    from piceli.hosts.model import HostError
    from piceli.hosts.remote import outcome, read_status

    _, ssh, known = _target(target, infra)
    started = time.monotonic()
    body: dict[str, Any] = {"schema": "piceli.host-wait.v1", "rev": rev,
                            "target": ssh.address,
                            "published_after": published_after}  # fmt: skip
    last_error: str | None = None
    while True:
        try:
            current = read_status(ssh, known_hosts=known)
            last_error = None
        except HostError as error:
            current, last_error = {}, error.code
        state, reason = (
            outcome(current, rev, published_after) if current else ("waiting", None)
        )
        attempt = current.get("last_attempt")
        body.update(
            host=current.get("host"),
            current=current.get("current"),
            attempt=attempt if isinstance(attempt, dict) else None,
            waited_seconds=round(time.monotonic() - started, 1),
        )
        if state == "deployed":
            say(f"{rev} runs on {ssh.address}: {_describe(current.get('current'))}")
            emit_json({**body, "state": "deployed"})
            return
        if state == "failed":
            _say_status(current)
            fail(str(reason), f"{rev} did not deploy on {ssh.address}", **body)
        if time.monotonic() - started >= timeout:
            fail(
                "host-wait-timeout",
                f"{rev} was not running on {ssh.address} after {timeout}s"
                + (f" (last read: {last_error})" if last_error else ""),
                **body,
                last_error=last_error,
            )
        time.sleep(interval)


def register(root: typer.Typer) -> None:
    root.add_typer(host_app, name="host")
