"""``piceli infra plan|apply|status|destroy|install|register`` and the infra secrets.

- ``infra plan MODULE:ATTR`` renders the OpenTofu configuration of a
  :class:`~piceli.infra.Infrastructure`, runs ``tofu plan`` and prints the
  changes, the monthly estimate and the plan hash (exit 3; 0 when nothing
  changes). ``infra apply MODULE:ATTR --approve HASH`` plans again and
  applies only that plan; ``infra destroy`` the same for a destroy plan,
  which never touches what Piceli did not create.
- ``infra status MODULE:ATTR`` shows servers, addresses, installs, cluster
  registrations and cost from the state directory's records (read-only).
- ``infra install MODULE:ATTR SERVER`` runs the server's install hook after
  approval of its rendered command; ``infra register MODULE:ATTR SERVER``
  waits for its k3s and makes it a cluster Piceli deploys to.
- ``secrets provider NAME --prompt`` stores a provider API token,
  ``secrets state-key NAME --generate|--prompt`` a state passphrase: read
  from stdin only, never printed.

Machine output is one JSON object on stdout; human text on stderr.

Importing this module is side-effect free.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, NoReturn

import typer

from piceli.cli_contract import EXIT_APPROVAL, emit_json, fail, reject, say

if TYPE_CHECKING:
    from piceli.infra import Infrastructure
    from piceli.infra.machines.model import InfraError
    from piceli.infra.machines.plan import InfraPlan

app = typer.Typer(
    rich_markup_mode=None,
    help="Machines in typed Python (piceli.infra.Infrastructure), provisioned with OpenTofu.",
    no_args_is_help=True,
)

InfraArgument = Annotated[
    str,
    typer.Argument(
        help="MODULE:ATTR (or path/file.py:ATTR) of a piceli.infra.Infrastructure",
        show_default=False,
    ),
]
ServerArgument = Annotated[
    str, typer.Argument(help="The server's name", show_default=False)
]
ApproveOption = Annotated[
    str | None, typer.Option("--approve", help="The plan hash (or digest) to execute")
]
TofuOption = Annotated[
    str | None,
    typer.Option(
        "--tofu", help="The tofu binary (default: $PICELI_TOFU, else tofu on PATH)"
    ),
]
JsonOption = Annotated[
    bool, typer.Option("--json", help="Print one JSON object on stdout")
]
#: Failures that ran (exit 1) rather than refusals (exit 2).
FAILED = {"infra-apply-failed", "infra-install-failed", "infra-k3s-not-ready"}
#: Variables a token is never read from.
TOKEN_ENVS = ("HCLOUD_TOKEN", "PICELI_PROVIDER_TOKEN", "PICELI_STATE_KEY")


def _refuse(error: InfraError) -> NoReturn:
    for line in error.details:
        say(f"  tofu: {line}")
    if error.code in FAILED:
        fail(error.code, str(error))
    reject(error.code, str(error))


@contextmanager
def _guard() -> Iterator[None]:
    from piceli.infra.machines.model import InfraError
    from piceli.profiles import ProfileError

    try:
        yield
    except InfraError as error:
        _refuse(error)
    except ProfileError as error:
        reject(error.code or "profile-invalid", str(error))


def load_infra(ref: str) -> Infrastructure:
    """The ``Infrastructure`` that ``ref`` names (a registered refusal otherwise)."""
    from piceli.app.render import RenderError, load_target
    from piceli.infra import Infrastructure
    from piceli.infra.machines.model import InfraError

    try:
        value = load_target(ref, Path.cwd())
    except RenderError as error:
        reject("infra-not-found", str(error))
    except InfraError as error:
        _refuse(error)
    except Exception as error:  # the module raised while importing
        from piceli.cli_contract import describe_user_error

        reject(
            "infra-load-failed", f"importing {ref} failed: {describe_user_error(error)}"
        )
    if not isinstance(value, Infrastructure):
        reject("infra-not-found", f"{ref} is not a piceli.infra.Infrastructure")
    return value


def _say_plan(result: InfraPlan) -> None:
    say(f"infrastructure {result.infra} {result.operation} plan (tofu {result.tofu}):")
    for change in result.changes:
        say(f"  {change.action:8} {change.kind} {change.name} ({change.address})")
    if not result.changes:
        say("  nothing to change")
    if result.foreign:
        say(
            "  refused: these resources were not created by Piceli: "
            + ", ".join(result.foreign)
        )
    if result.estimate is not None:
        estimate = result.estimate
        note = "" if estimate["complete"] else " (some prices unknown)"
        say(
            f"  estimate: {estimate['monthly_net']:.2f} {estimate['currency']}/month net{note}"
        )
    say(f"plan hash: {result.plan_hash}")


def _command(head: str, ref: str, tofu: str | None) -> str:
    return " ".join([head, ref, *(["--tofu", tofu] if tofu else [])])


def _plan_or_apply(
    ref: str, approve: str | None, tofu: str | None, *, destroy: bool
) -> None:
    from piceli.infra.machines.plan import apply, plan, workspace

    infra = load_infra(ref)
    head = "piceli infra destroy" if destroy else "piceli infra apply"
    command = _command(head, ref, tofu)
    with _guard(), workspace(infra, tofu=tofu) as ws:
        if approve is None:
            result = plan(ws, destroy=destroy)
            _say_plan(result)
            body = result.to_dict()
            if result.foreign:
                reject(
                    "infra-foreign-resource",
                    "the plan would change or delete resources Piceli did not create",
                    **body,
                )
            if not result.changes:
                emit_json({**body, "state": "unchanged"})
                return
            say("approve with:")
            say(f"  {command} --approve {result.plan_hash}")
            emit_json(
                {
                    **body,
                    "state": "approval-required",
                    "approve_command": f"{command} --approve {result.plan_hash}",
                }
            )
            raise typer.Exit(EXIT_APPROVAL)
        done, inventory = apply(ws, approve, destroy=destroy, say=say)
        _say_plan(done)
        state = "destroyed" if destroy else "applied"
        say(f"{state}: {len(done.changes)} change(s)")
        for name, server in sorted((inventory.get("servers") or {}).items()):
            say(
                f"  server {name}: {server.get('ipv4') or '-'} {server.get('ipv6') or ''}".rstrip()
            )
        emit_json(
            {
                **done.to_dict(),
                "state": state,
                "servers": inventory.get("servers") or {},
                "resources": len(inventory.get("resources") or ()),
            }
        )


@app.command("plan")
def plan_command(ref: InfraArgument, tofu: TofuOption = None) -> None:
    """Plan the declared machines with OpenTofu; print the changes, estimate and plan hash."""
    _plan_or_apply(ref, None, tofu, destroy=False)


@app.command("apply")
def apply_command(
    ref: InfraArgument, approve: ApproveOption = None, tofu: TofuOption = None
) -> None:
    """Apply the plan whose hash is --approve (without it: plan, exit 3)."""
    _plan_or_apply(ref, approve, tofu, destroy=False)


@app.command("destroy")
def destroy_command(
    ref: InfraArgument, approve: ApproveOption = None, tofu: TofuOption = None
) -> None:
    """Destroy what Piceli created for it, with approval of the destroy plan's hash."""
    _plan_or_apply(ref, approve, tofu, destroy=True)


@app.command("status")
def status_command(ref: InfraArgument, as_json: JsonOption = False) -> None:
    """Servers, addresses, installs, cluster registrations and cost (read-only)."""
    from piceli.infra.machines.status import summarize

    infra = load_infra(ref)
    with _guard():
        body = summarize(infra)
    say(f"infrastructure {body['name']}: {body['state']}")
    for server in body["servers"]:
        cluster = server["cluster"]
        registered = (
            "" if cluster is None
            else f", cluster {cluster['name']} {'registered' if cluster['registered'] else 'not registered'}"
        )  # fmt: skip
        install = server["install"]["state"] or (
            "not run" if server["install"]["declared"] else "none"
        )
        say(
            f"  server {server['name']}: {server['state']}, {server['ipv4'] or '-'}"
            f", install {install}{registered}"
        )
    for record in body["records"]:
        say(f"  record {record['key']}: {record['state']}")
    if body["estimate"]:
        say(
            f"  estimate: {body['estimate']['monthly_net']:.2f} {body['estimate']['currency']}/month net"
        )
    if as_json:
        emit_json(body)


@app.command("install")
def install_command(
    ref: InfraArgument, server: ServerArgument, approve: ApproveOption = None
) -> None:
    """Run the server's install hook (its rendered command, approved by digest)."""
    from piceli.infra.machines.install import preview_install, run_install
    from piceli.infra.machines.state import locked, state_dir

    infra = load_infra(ref)
    with _guard():
        directory = state_dir(infra.name, infra.state_dir)
        with locked(directory):
            preview = preview_install(infra, directory, server)
            say(f"install hook of {server}: {' '.join(preview.argv)}")
            say(f"digest: {preview.digest}")
            command = f"piceli infra install {ref} {server}"
            if approve is None:
                say("approve with:")
                say(f"  {command} --approve {preview.digest}")
                emit_json(
                    {
                        **preview.to_dict(),
                        "state": "approval-required",
                        "approve_command": f"{command} --approve {preview.digest}",
                    }
                )
                raise typer.Exit(EXIT_APPROVAL)
            if approve != preview.digest:
                reject(
                    "infra-install-changed",
                    "the install command changed since it was approved",
                )
            outcome = run_install(infra, directory, preview)
    if outcome["state"] != "installed":
        fail(
            "infra-install-failed",
            f"the install hook ended with {outcome['state']} (exit {outcome['exit_code']})",
            **preview.to_dict(),
            outcome=outcome,
        )
    say(f"installed: {server} ({outcome['seconds']}s)")
    emit_json({**preview.to_dict(), "state": "installed", "outcome": outcome})


@app.command("register")
def register_command(
    ref: InfraArgument,
    server: ServerArgument,
    kubeconfig: Annotated[
        Path | None,
        typer.Option(
            "--kubeconfig",
            help="The server's k3s kubeconfig, given by the owner (default: read over SSH)",
        ),
    ] = None,
    context: Annotated[
        str | None, typer.Option("--context", help="Its context (when it has several)")
    ] = None,
    wait: Annotated[
        int,
        typer.Option(
            "--wait", help="Seconds to wait for k3s to answer with a Ready node"
        ),
    ] = 600,
    approve: ApproveOption = None,
    transport: Annotated[
        str,
        typer.Option(
            "--transport", help="https, or loopback-http for a local test API"
        ),
    ] = "https",
) -> None:
    """Wait for the server's k3s and register it as its Cluster (approved by digest)."""
    from piceli.infra.machines.register import preview_register, run_register
    from piceli.infra.machines.state import locked, state_dir

    if transport not in ("https", "loopback-http"):
        reject("infra-invalid", "--transport is https or loopback-http")
    infra = load_infra(ref)
    with _guard():
        directory = state_dir(infra.name, infra.state_dir)
        with locked(directory):
            preview = preview_register(
                infra, directory, server, kubeconfig=kubeconfig, context=context
            )
            body = preview.to_dict()
            say(
                f"register {server} as cluster {preview.cluster} ({preview.api}), profile {preview.profile}"
            )
            if preview.source == "ssh":
                for key in body["host_keys"]:
                    say(f"  host key {key['type']} {key['fingerprint']}")
            else:
                say("  kubeconfig: given with --kubeconfig")
            if preview.profile_exists:
                say(f"  the profile {preview.profile} exists and will be replaced")
            say(f"digest: {preview.digest}")
            parts = ["piceli infra register", ref, server]
            if kubeconfig is not None:
                parts += ["--kubeconfig", str(kubeconfig)]
            if context is not None:
                parts += ["--context", context]
            if transport != "https":
                parts += ["--transport", transport]
            command = " ".join(parts)
            if approve is None:
                say("approve with:")
                say(f"  {command} --approve {preview.digest}")
                emit_json(
                    {
                        **body,
                        "state": "approval-required",
                        "approve_command": f"{command} --approve {preview.digest}",
                    }
                )
                raise typer.Exit(EXIT_APPROVAL)
            if approve != preview.digest:
                reject(
                    "infra-register-changed",
                    "what register would do changed since it was approved (host keys, "
                    "kubeconfig or profile); review it again",
                )
            outcome = run_register(
                infra, directory, preview, kubeconfig=kubeconfig, context=context,
                wait=max(0, wait), transport=transport, say=say,
            )  # fmt: skip
    say(
        f"registered: cluster {outcome['cluster']} through profile {outcome['profile']}"
    )
    emit_json({**body, "state": "registered", "outcome": outcome})


# ------------------------------------------------------------- secrets


def _read_secret(prompt: str) -> str:
    import getpass

    if sys.stdin is not None and sys.stdin.isatty():
        try:
            return getpass.getpass(prompt, stream=sys.stderr)
        except (EOFError, KeyboardInterrupt):
            return ""
    line = sys.stdin.readline() if sys.stdin is not None else ""
    return line.rstrip("\r\n")


def _no_secret_arguments(context: typer.Context) -> None:
    if context.args:
        # Never echo what was passed: it may be the secret itself.
        reject(
            "infra-credential-refused",
            "a secret is never an argument; pass --prompt and type it (or pipe it on stdin)",
        )
    for name in TOKEN_ENVS:
        if os.environ.get(name):
            reject(
                "infra-credential-refused",
                f"a secret is never read from the environment; unset {name} and use --prompt",
            )


NameArgument = Annotated[str, typer.Argument(help="Credential name, e.g. hcloud")]
PromptOption = Annotated[
    bool,
    typer.Option(
        "--prompt", help="Read the value from stdin (typed without echo, or piped)"
    ),
]


def provider_secret(
    context: typer.Context, name: NameArgument, prompt: PromptOption = False
) -> None:
    """Store a provider API token (read from stdin, never printed)."""
    from piceli.infra.machines.credentials import save_credential

    _no_secret_arguments(context)
    if not prompt:
        reject("infra-prompt-required", "pass --prompt: the token is read from stdin")
    with _guard():
        value = _read_secret("Provider token (not echoed): ")
        path = save_credential(name, "provider-token", value)
        del value
    say(f"stored credential {name} (provider-token); the token was not printed")
    emit_json(
        {"state": "stored", "name": name, "kind": "provider-token", "path": str(path)}
    )


def state_key_secret(
    context: typer.Context,
    name: NameArgument,
    prompt: PromptOption = False,
    generate: Annotated[
        bool,
        typer.Option("--generate", help="Generate a random passphrase (never printed)"),
    ] = False,
    replace: Annotated[
        bool,
        typer.Option(
            "--replace",
            help="Replace an existing key (the state it encrypts becomes unreadable)",
        ),
    ] = False,
) -> None:
    """Store the passphrase that encrypts an infrastructure's OpenTofu state."""
    from piceli.infra.machines.credentials import (
        credential_present,
        generate_passphrase,
        save_credential,
    )

    _no_secret_arguments(context)
    if prompt == generate:
        reject("infra-prompt-required", "pass exactly one of --prompt or --generate")
    with _guard():
        if credential_present(name) and not replace:
            reject(
                "infra-credential-exists",
                f"credential {name!r} exists; a new key cannot read the state the old one "
                "encrypted (--replace if you mean it)",
            )
        value = (
            generate_passphrase()
            if generate
            else _read_secret("State passphrase (not echoed): ")
        )
        path = save_credential(name, "state-key", value)
        del value
    say(
        f"stored credential {name} (state-key); back up {path}, the state cannot be read without it"
    )
    emit_json({"state": "stored", "name": name, "kind": "state-key", "path": str(path)})


def register(root: typer.Typer) -> None:
    from piceli.k8s.cli.cluster import secrets_app

    root.add_typer(app, name="infra")
    settings: dict[str, Any] = {
        "allow_extra_args": True,
        "ignore_unknown_options": True,
    }
    secrets_app.command("provider", context_settings=settings)(provider_secret)
    secrets_app.command("state-key", context_settings=settings)(state_key_secret)
