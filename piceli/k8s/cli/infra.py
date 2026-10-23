"""``piceli infra plan|apply|status|destroy|install|register`` and the infra secrets.

- ``infra plan MODULE:ATTR`` renders the OpenTofu configuration of a
  :class:`~piceli.infra.Infrastructure`, runs ``tofu plan`` and prints the
  changes, the monthly estimate and the plan hash (exit 3; 0 when nothing
  changes). ``infra apply MODULE:ATTR --approve HASH`` plans again and
  applies only that plan; ``infra destroy`` the same for a destroy plan,
  which never touches what Piceli did not create. An apply that fails after
  changing something exits 1 with ``partially_applied`` and what it applied.
- ``infra status MODULE:ATTR`` shows servers, addresses, installs, cluster
  registrations and cost from the state directory's records (read-only).
- ``infra install MODULE:ATTR SERVER`` runs the server's install hook after
  approval of its rendered command; ``infra register MODULE:ATTR SERVER``
  waits for its k3s and makes it a cluster Piceli deploys to, or, for a
  server without ``cluster=``, pins its SSH host keys only.
- ``secrets provider NAME --prompt`` stores a provider API token,
  ``secrets state-key NAME --generate|--prompt`` a state passphrase,
  ``secrets user-data NAME --prompt|--file PATH`` a server's user data: read
  from stdin or a file only, never printed.

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
FAILED = {
    "infra-apply-failed",
    "infra-install-failed",
    "infra-k3s-not-ready",
    "secrets-backup-failed",
    "secrets-backup-key-failed",
    "secrets-restore-decrypt-failed",
}
#: Variables a token is never read from.
TOKEN_ENVS = (
    "HCLOUD_TOKEN",
    "OS_APPLICATION_CREDENTIAL_SECRET",
    "PORKBUN_API_KEY",
    "PORKBUN_SECRET_API_KEY",
    "PICELI_PROVIDER_TOKEN",
    "PICELI_STATE_KEY",
)


def _refuse(error: InfraError) -> NoReturn:
    for line in error.details:
        say(f"  tofu: {line}")
    for item in error.fields.get("conflicts") or ():
        record = item["record"]
        say(f"  conflict: {record['type']} {record['name']} {record['content']}: "
            f"{item['why']}")  # fmt: skip
    if error.code in FAILED or error.fields.get("partially_applied"):
        # Something changed: a failure that ran, whatever refused it.
        fail(error.code, str(error), **error.fields)
    reject(error.code, str(error), **error.fields)


def _say_partial(error: InfraError, command: str) -> None:
    say("applied before the failure:")
    for item in error.fields.get("applied") or ():
        found = f" (id {item['id']})" if item.get("id") else ""
        say(f"  {item['action']:8} {item['address']}{found}")
    if error.fields.get("not_applied"):
        say("not applied:")
    for item in error.fields.get("not_applied") or ():
        failed = " (failed)" if item.get("failed") else ""
        say(f"  {item['action']:8} {item['address']}{failed}")
    say(f"the next plan shows what remains: {command}")


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
    for item in result.user_data:
        say(
            f"  user data of {item['server']}: secret {item['secret']} "
            f"({item['digest']}), passed at creation only"
        )
    for item in result.s3_keys:
        enforced = item.get("read_only_enforced")
        limit = (
            ""
            if item["access"] != "read"
            else " (read-only enforced)"
            if enforced
            else " (read-only NOT enforced by the cloud)"
        )
        say(
            f"  s3 key {item['name']}: {item['access']} on bucket {item['bucket']}, "
            f"{item['action']}{limit}; stored as credential {item['credential']}"
        )
    for check in result.checks:
        if not check.get("ran"):
            say(f"  not checked {check['check']}: {check.get('why')}")
        elif "domains" in check:
            say(f"  checked {check['check']}: " + ", ".join(sorted(check["domains"])))
        else:
            say(f"  checked {check['check']}")
        for warning in check.get("warnings") or ():
            record = warning.get("record") or {}
            say(
                f"    warning: {record.get('type')} {record.get('name')} "
                f"{record.get('content')}: {warning.get('why')}"
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
    from piceli.infra.machines.model import InfraError
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
        try:
            done, inventory = apply(ws, approve, destroy=destroy, say=say)
        except InfraError as error:
            if error.fields.get("partially_applied"):
                _say_partial(error, command)
            raise
        _say_plan(done)
        state = "destroyed" if destroy else "applied"
        say(f"{state}: {len(done.changes)} change(s)")
        for name, server in sorted((inventory.get("servers") or {}).items()):
            say(
                f"  server {name}: {server.get('ipv4') or '-'} {server.get('ipv6') or ''}".rstrip()
            )
        left = _profiles_left(infra) if destroy else []
        if left:
            say(
                "the credential profiles of their clusters are kept (piceli logout NAME): "
                + ", ".join(left)
            )
        emit_json(
            {
                **done.to_dict(),
                "state": state,
                "servers": inventory.get("servers") or {},
                "resources": len(inventory.get("resources") or ()),
                **({"profiles_left": left} if destroy else {}),
            }
        )


def _profiles_left(infra: Infrastructure) -> list[str]:
    """Profiles of the destroyed servers' clusters that still exist."""
    from piceli.profiles import ProfileError, load_profile

    left: list[str] = []
    for server in infra.servers:
        if server.cluster is None:
            continue
        try:
            load_profile(server.cluster.credentials)
        except ProfileError:
            continue
        left.append(server.cluster.credentials)
    return sorted(set(left))


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
    """Wait for the server's k3s and register it as its Cluster, or pin its SSH host keys (approved by digest)."""
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
            if preview.cluster is None:
                say(f"pin the SSH host keys of {server} ({preview.address})")
            else:
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
    if preview.cluster is None:
        say(f"pinned: piceli host reads {server} with these keys (--infra {ref})")
        emit_json({**body, "state": "pinned", "outcome": outcome})
        return
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


def _no_secret_arguments(context: typer.Context, *, from_env: bool = False) -> None:
    if context.args:
        # Never echo what was passed: it may be the secret itself.
        reject(
            "infra-credential-refused",
            "a secret is never an argument; pass --prompt and type it (or pipe it on stdin)",
        )
    if from_env:
        return
    for name in TOKEN_ENVS:
        if os.environ.get(name):
            reject(
                "infra-credential-refused",
                f"a secret is never read from the environment unasked; unset {name} "
                "and use --prompt, or pass --from-env to read the provider's variables",
            )


NameArgument = Annotated[str, typer.Argument(help="Credential name, e.g. hcloud")]
PromptOption = Annotated[
    bool,
    typer.Option(
        "--prompt", help="Read the value from stdin (typed without echo, or piped)"
    ),
]


def _terminal() -> bool:
    return bool(sys.stdin is not None and sys.stdin.isatty())


def _echoed(prompt: str) -> str:
    """An identifier, typed with echo (it is not a secret)."""
    sys.stderr.write(prompt)
    sys.stderr.flush()
    line = sys.stdin.readline() if sys.stdin is not None else ""
    return line.rstrip("\r\n")


def _hidden(prompt: str) -> str:
    return _read_secret(prompt)


def _ask_parts(shape: Any, *, from_env: bool) -> dict[str, str]:
    """Each part of ``shape``: from the provider's variables, or asked for
    one by one (on a terminal: what it is and where to find it; piped: one
    per line). Each is checked as it comes; nothing is printed."""
    from piceli.infra.machines.credential_parts import check_part
    from piceli.infra.machines.model import InfraError

    parts: dict[str, str] = {}
    total = len(shape.parts)
    for step, part in enumerate(shape.parts, 1):
        if from_env:
            value = os.environ.get(part.env, "")
            if not value:
                raise InfraError(
                    "infra-credential-part-invalid",
                    f"{part.label}: {part.env} is not set; nothing was stored",
                    fields={"part": part.key},
                )
        elif _terminal():
            say(f"Step {step} of {total}: {part.label}. Where: {part.where}.")
            if part.secret:
                head = part.label[:-1] if part.label.endswith(")") else part.label
                tail = ", not echoed)" if part.label.endswith(")") else " (not echoed)"
                value = _hidden(f"{head}{tail}: ")
            else:
                value = _echoed(f"{part.label}: ")
        else:
            value = _read_secret("")
        check_part(shape, part, value, parts)
        parts[part.key] = value
        del value
    return parts


def _declared_provider(ref: str, name: str) -> str:
    """The provider of ``ref`` whose ``credentials=`` is ``name``."""
    from piceli.infra.machines.model import InfraError

    found = {p.name for p in load_infra(ref).providers() if p.credentials == name}
    if len(found) != 1:
        raise InfraError(
            "infra-credential-invalid",
            f"no single provider of {ref} uses credentials={name!r}"
            if not found
            else f"providers of different kinds use credentials={name!r}",
        )
    return found.pop()


def provider_secret(
    context: typer.Context,
    name: NameArgument,
    prompt: Annotated[
        bool,
        typer.Option(
            "--prompt",
            help="Ask for each part on stdin (typed: an identifier echoed, a "
            "secret not; piped: one part per line, in order)",
        ),
    ] = False,
    provider: Annotated[
        str | None,
        typer.Option(
            "--provider",
            help="Whose credential it is: porkbun, openstack (or infomaniak), "
            "hetzner (or hcloud), or token (one API token)",
            show_default=False,
        ),
    ] = None,
    infra_ref: Annotated[
        str | None,
        typer.Option(
            "--infra",
            help="MODULE:ATTR of the Infrastructure whose provider uses "
            "credentials=NAME: it tells which provider NAME is for",
            show_default=False,
        ),
    ] = None,
    from_env: Annotated[
        bool,
        typer.Option(
            "--from-env",
            help="Read the parts from the provider's standard variables instead "
            "of stdin (for scripts)",
        ),
    ] = False,
) -> None:
    """Store a provider's API credential, part by part (never printed).

    Which provider NAME is for: --provider, else the provider of --infra
    MODULE:ATTR that uses credentials=NAME, else the provider stored under NAME
    before, else the name itself (porkbun; openstack or infomaniak; hetzner or
    hcloud; alone or before a dash, as in porkbun-dns). Otherwise it is one API
    token.

    A credential with two parts is asked for part by part, as the provider's
    console names them, and each part is checked before anything is stored
    (not empty, no space around it, no ':' inside, the provider's known shape):

    \b
      porkbun:    1. Porkbun API key (pk1_…)
                  2. Porkbun secret API key (sk1_…)
                  (porkbun.com > Account > API Access > Create API Key)
      openstack,  1. Application credential ID (32 hex)
      infomaniak: 2. Application credential secret
                  (Horizon > Identity > Application Credentials, or
                  `openstack application credential create`)
      hetzner:    Hetzner Cloud API token
      token:      Provider API token

    Piped (scripts, tests): one part per line, in that order; a KEY:SECRET line
    is refused. --from-env reads PORKBUN_API_KEY and PORKBUN_SECRET_API_KEY,
    OS_APPLICATION_CREDENTIAL_ID and OS_APPLICATION_CREDENTIAL_SECRET,
    HCLOUD_TOKEN, or PICELI_PROVIDER_TOKEN. Never pass a secret as an argument.

    Running it again replaces the stored credential (a one-part value a plan
    reported, an old token): no delete is needed.
    """
    from piceli.infra.machines import credential_parts as cp
    from piceli.infra.machines.credentials import (
        credential_present,
        save_credential,
        save_parts,
    )
    from piceli.infra.machines.model import InfraError

    _no_secret_arguments(context, from_env=from_env)
    if prompt == from_env:
        reject(
            "infra-prompt-required",
            "pass --prompt (the parts are read from stdin) or --from-env",
        )
    with _guard():
        if provider is not None:
            if provider not in cp.PROVIDER_OPTIONS:
                raise InfraError(
                    "infra-credential-invalid",
                    "--provider is one of " + ", ".join(cp.PROVIDER_OPTIONS),
                )
            shape = cp.shape_of(provider)
        elif infra_ref is not None:
            shape = cp.shape_of(_declared_provider(infra_ref, name))
        else:
            recorded = cp.credential_report(name) if credential_present(name) else None
            shape = (
                cp.shape_of(recorded["provider"])
                if recorded and recorded["kind"] == "provider-token"
                else cp.shape_for_name(name)
            )
        replaced = credential_present(name)
        parts = _ask_parts(shape, from_env=from_env)
        if len(shape.parts) == 1:
            path = save_credential(
                name, "provider-token", parts[shape.keys[0]], provider=shape.provider
            )
        else:
            path = save_parts(name, shape.provider, parts)
        del parts
    verb = "replaced" if replaced else "stored"
    say(
        f"{verb} credential {name} (provider-token, {shape.title}): "
        f"{shape.names()}; no value was printed"
    )
    emit_json(
        {"state": "stored", "name": name, "kind": "provider-token",
         "provider": shape.provider, "parts": shape.keys, "replaced": replaced,
         "path": str(path)}
    )  # fmt: skip


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


def user_data_secret(
    context: typer.Context,
    name: NameArgument,
    prompt: Annotated[
        bool,
        typer.Option(
            "--prompt",
            help="Read it from stdin (typed: one line without echo; piped: until EOF)",
        ),
    ] = False,
    file: Annotated[
        Path | None,
        typer.Option(
            "--file", help="Read it from this file (a cloud-config, a script)"
        ),
    ] = None,
) -> None:
    """Store a server's user data (a cloud-config, a script or a bootstrap key)."""
    from piceli.infra.machines.credentials import save_credential
    from piceli.infra.machines.plan import user_data_digest

    _no_secret_arguments(context)
    if prompt == (file is not None):
        reject("infra-prompt-required", "pass exactly one of --prompt or --file PATH")
    with _guard():
        if file is not None:
            try:
                value = file.read_text()
            except (OSError, UnicodeDecodeError):
                reject("infra-credential-invalid", "the --file is not readable text")
        elif sys.stdin is not None and not sys.stdin.isatty():
            value = sys.stdin.read()
        else:
            value = _read_secret("User data (not echoed): ")
        path = save_credential(name, "user-data", value)
        digest, size = user_data_digest(value), len(value.encode())
        del value
    say(f"stored credential {name} (user-data, {size} bytes, {digest}); not printed")
    emit_json(
        {"state": "stored", "name": name, "kind": "user-data", "digest": digest,
         "bytes": size, "path": str(path)}
    )  # fmt: skip


def _interactive() -> bool:
    return bool(sys.stdin is not None and sys.stdin.isatty() and sys.stderr.isatty())


def delete_secret(
    name: NameArgument,
    kind: Annotated[
        str | None,
        typer.Option(
            "--kind",
            help="Delete it only if it is this kind: provider, state-key, "
            "nix-cache, s3, age or user-data",
            show_default=False,
        ),
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", help="Delete without asking (owner only)")
    ] = False,
) -> None:
    """Delete a stored credential (a spent bootstrap key, an old token); never printed."""
    from piceli.infra.machines.credentials import (
        delete_credential,
        kind_of,
        require_kind,
        stored_kind,
    )

    with _guard():
        wanted = kind_of(kind) if kind is not None else None
        path, found = stored_kind(name)
        require_kind(name, found, wanted)
        what = f"{name} ({found or 'damaged'})"
        if not yes:
            if _interactive():
                answer = typer.prompt(
                    f"Type the credential name ({name}) to delete {path}",
                    default="",
                    err=True,
                )
            else:
                answer = None
            if answer != name:
                command = " ".join(
                    ["piceli secrets delete", name,
                     *(["--kind", kind] if kind is not None else
                       ["--kind", _option_of(found)] if found else []),
                     "--yes"]
                )  # fmt: skip
                say(f"not deleted: {what}; to delete it: {command}")
                emit_json(
                    {"state": "approval-required", "name": name, "kind": found,
                     "path": str(path), "approve_command": command}
                )  # fmt: skip
                raise typer.Exit(EXIT_APPROVAL)
        path, found = delete_credential(name, wanted)
    say(f"deleted credential {what}; the value was not printed")
    emit_json({"state": "deleted", "name": name, "kind": found, "path": str(path)})


def _option_of(kind: str) -> str:
    from piceli.infra.machines.credentials import KIND_OPTIONS

    return next((o for o, k in KIND_OPTIONS.items() if k == kind), kind)


def register(root: typer.Typer) -> None:
    from piceli.k8s.cli.cluster import secrets_app

    root.add_typer(app, name="infra")
    settings: dict[str, Any] = {
        "allow_extra_args": True,
        "ignore_unknown_options": True,
    }
    secrets_app.command("provider", context_settings=settings)(provider_secret)
    secrets_app.command("state-key", context_settings=settings)(state_key_secret)
    secrets_app.command("user-data", context_settings=settings)(user_data_secret)
    secrets_app.command("delete")(delete_secret)
    from piceli.k8s.cli.secrets_backup import register as register_backup
    from piceli.k8s.cli.secrets_status import register as register_status

    register_backup(secrets_app)
    register_status(secrets_app)
