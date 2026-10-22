"""``piceli host``: Nix host releases (0.24.0).

``piceli host module MODULE:ATTR`` renders the NixOS module of a
:class:`~piceli.hosts.NixHost` (the agent, its configuration, a service and
a timer); ``piceli host status`` and ``piceli host wait`` read the agent's
status over SSH, with no cluster involved. ``piceli host pin|unpin|rollback``
run the agent once over SSH; ``piceli host secrets`` puts the host's age
identity (``piceli secrets age``) where sops-nix reads it, after approval.
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
    help="Nix host releases: the agent's module, status, wait, pins, rollback "
    "and bootstrap secret (0.24.0).",
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
    hold = status.get("hold")
    if isinstance(hold, dict):
        what = "pinned to" if hold.get("kind") == "pin" else "rolled back to"
        say(f"  {what} {_describe(hold)} at {hold.get('at')}: no newer build "
            "until `piceli host unpin`")  # fmt: skip
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


# ------------------------------------------------------------- pin, rollback

_DONE = {"pinned": "pinned", "unpinned": "unpinned", "rolled-back": "rolled back"}


def _control(
    target: str, infra: str | None, action: str, tag: str | None, as_json: bool
) -> None:
    from piceli.hosts.model import HostError
    from piceli.hosts.remote import control

    host, ssh, known = _target(target, infra)
    try:
        result = control(ssh, action, tag, known_hosts=known)
    except HostError as error:
        reject(error.code, str(error))
    body = {
        "schema": "piceli.host-control.v1",
        "target": ssh.address,
        "host": host.name if host is not None else None,
        **{k: result.get(k) for k in ("action", "state", "reason", "hold",
                                      "current", "attempt")},
    }  # fmt: skip
    state, reason = str(result.get("state")), str(result.get("reason") or "")
    if state not in _DONE:
        if result.get("attempt"):
            _say_status({"host": body["host"] or ssh.address,
                         "current": result.get("current"),
                         "last_attempt": result.get("attempt")})  # fmt: skip
        from piceli.errors import ERRORS

        code = reason if reason in ERRORS else "host-control-unsupported"
        message = f"{action} on {ssh.address}: {state}"
        fields = {k: v for k, v in body.items() if k not in ("state", "reason")}
        if state == "failed":
            fail(code, message + " (the host runs what it ran before)", **fields)
        reject(code, message, **fields)
    say(f"{ssh.address}: {_DONE[state]}; current {_describe(result.get('current'))}")
    if as_json:
        emit_json(body)


@host_app.command("pin")
def pin(
    target: TargetArgument,
    tag: Annotated[
        str,
        typer.Argument(
            help="The tag whose published build the host holds", show_default=False
        ),
    ],
    infra: InfraOption = None,
    as_json: JsonOption = False,
) -> None:
    """Deploy a tag's build (an older one too) and hold it: no newer build until unpin."""
    _control(target, infra, "pin", tag, as_json)


@host_app.command("unpin")
def unpin(
    target: TargetArgument, infra: InfraOption = None, as_json: JsonOption = False
) -> None:
    """Drop a pin or rollback hold: the host follows its declared ref again."""
    _control(target, infra, "unpin", None, as_json)


@host_app.command("rollback")
def rollback(
    target: TargetArgument, infra: InfraOption = None, as_json: JsonOption = False
) -> None:
    """Switch back to the previous system (in the store) and hold it until unpin."""
    _control(target, infra, "rollback", None, as_json)


# ------------------------------------------------------------- secrets


@host_app.command("secrets")
def host_secrets(
    target: Annotated[
        str,
        typer.Argument(
            help="MODULE:ATTR of a piceli.infra.NixHost with age_identity=",
            show_default=False,
        ),
    ],
    infra: InfraOption = None,
    replace: Annotated[
        bool,
        typer.Option(
            "--replace",
            help="Replace a different key the host has "
            "(what it decrypts with it changes)",
        ),
    ] = False,
    approve: Annotated[
        str | None, typer.Option("--approve", help="The plan's digest")
    ] = None,
    as_json: JsonOption = False,
) -> None:
    """Put the host's age identity where sops-nix reads it: plan, then --approve."""
    import hashlib
    import json

    from piceli.cli_contract import EXIT_APPROVAL
    from piceli.hosts import remote
    from piceli.hosts.age import AgeError, check_identity, recipient
    from piceli.hosts.model import HostError
    from piceli.infra.machines.credentials import load_credential
    from piceli.infra.machines.model import InfraError

    host, ssh, known = _target(target, infra)
    if host is None or host.age_identity is None:
        reject(
            "host-secret-undeclared",
            "give a NixHost with age_identity= (piceli secrets age NAME --generate)",
        )
    try:
        identity = check_identity(load_credential(host.age_identity, "age-identity"))
        public = recipient(identity)
        existing = remote.age_key_state(
            ssh, host.age_key_file, identity, known_hosts=known
        )
    except (InfraError, AgeError, HostError) as error:
        reject(error.code, str(error))
    plan = {
        "host": host.name, "target": ssh.address, "port": ssh.port,
        "user": ssh.user, "path": host.age_key_file, "mode": "0600",
        "owner": "root", "credential": host.age_identity, "public_key": public,
        "existing": existing, "replace": replace,
    }  # fmt: skip
    digest = (
        "sha256:"
        + hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
    )
    body = {"schema": "piceli.host-secrets.v1", **plan, "digest": digest}
    say(f"{host.name}: age identity {host.age_identity} -> {ssh.address}:"
        f"{host.age_key_file} (0600 root), public key {public}")  # fmt: skip
    say(f"  the host has: {existing} key")
    if existing == "same":
        emit_json({**body, "state": "current"})
        return
    if existing == "different" and not replace:
        reject(
            "host-secret-exists",
            f"{host.age_key_file} on {ssh.address} holds another key; what sops-nix "
            "decrypts with it changes (--replace if you mean it)",
            **body,
        )
    if approve is None:
        command = f"piceli host secrets {target}"
        command += f" --infra {infra}" if infra else ""
        command += " --replace" if replace else ""
        say("approve with:")
        say(f"  {command} --approve {digest}")
        emit_json({**body, "state": "approval-required",
                   "approve_command": f"{command} --approve {digest}"})  # fmt: skip
        raise typer.Exit(EXIT_APPROVAL)
    if approve != digest:
        reject(
            "host-secret-plan-changed",
            "what host secrets would do changed since it was approved; review it again",
        )
    content = f"# public key: {public}\n{identity}\n".encode()
    del identity
    try:
        remote.write_age_key(ssh, host.age_key_file, content, known_hosts=known)
        del content
        activated = remote.activate(ssh, known_hosts=known)
    except HostError as error:
        fail(error.code, str(error), **body)
    if not activated:
        fail(
            "host-secret-activation-failed",
            "the key was written; re-running the activation failed",
            **body,
        )
    say(f"written; {ssh.address} re-ran its activation (sops-nix decrypts with it)")
    emit_json({**body, "state": "written", "activated": True})


def age_secret(
    context: typer.Context,
    name: Annotated[str, typer.Argument(help="Credential name, e.g. web-1-age")],
    generate: Annotated[
        bool, typer.Option("--generate", help="Generate a new identity")
    ] = False,
    prompt: Annotated[
        bool,
        typer.Option(
            "--prompt",
            help="Read an existing identity (AGE-SECRET-KEY-1...) from stdin",
        ),
    ] = False,
    replace: Annotated[
        bool,
        typer.Option(
            "--replace",
            help="Replace an existing identity (what was "
            "encrypted to the old one needs re-encrypting)",
        ),
    ] = False,
) -> None:
    """A host's age identity for sops-nix: generate or import it; print its public key."""
    from piceli.hosts.age import AgeError, check_identity, generate_identity, recipient
    from piceli.infra.machines.credentials import (
        credential_present,
        load_credential,
        save_credential,
    )
    from piceli.infra.machines.model import InfraError
    from piceli.k8s.cli.infra import _guard, _no_secret_arguments, _read_secret

    _no_secret_arguments(context)
    if generate and prompt:
        reject("infra-prompt-required", "pass one of --prompt or --generate")
    with _guard():
        try:
            if not generate and not prompt:
                public = recipient(load_credential(name, "age-identity"))
                state = "present"
            else:
                if credential_present(name) and not replace:
                    reject(
                        "infra-credential-exists",
                        f"credential {name!r} exists; .sops.yaml lists its public "
                        "key (--replace if you mean it)",
                    )
                value = check_identity(
                    generate_identity()
                    if generate
                    else _read_secret("age identity (not echoed): ")
                )
                public = recipient(value)
                save_credential(name, "age-identity", value)
                del value
                state = "stored"
        except (AgeError, InfraError) as error:
            reject(error.code, str(error))
    say(f"credential {name} (age-identity); the identity was not printed")
    say(f"public key: {public}")
    emit_json({"state": state, "name": name, "kind": "age-identity",
               "public_key": public})  # fmt: skip


def register(root: typer.Typer) -> None:
    from piceli.k8s.cli.cluster import secrets_app

    root.add_typer(host_app, name="host")
    settings: dict[str, Any] = {
        "allow_extra_args": True,
        "ignore_unknown_options": True,
    }
    secrets_app.command("age", context_settings=settings)(age_secret)
