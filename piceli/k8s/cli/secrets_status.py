"""``piceli secrets status|setup MODULE[:ATTR]``: every secret a declaration needs (0.25.0).

- ``secrets status MODULE[:ATTR] [--json]`` lists every secret the module's
  declarations need (:mod:`piceli.infra.machines.secrets_status`): present,
  missing, incomplete or damaged, who creates it, the next command, what it
  is for and where else it must live; then the backup's state and warnings.
  Exit 0 when every secret is stored and complete, 1 otherwise, 2 for a
  module that does not load or declares nothing.
- ``secrets setup MODULE[:ATTR] [--yes] [--json]`` walks the missing ones
  in order: provider credentials (asked for part by part), the keys Piceli
  generates (printing only their public halves), the user data (offered,
  skipped by default), the S3 pairs (``infra apply`` makes them), and a
  backup. It never replaces a stored key without a typed confirmation.

Values are never printed. Machine output goes to stdout (one object, or
JSON lines with ``setup --json``); human text to stderr. Importing this
module is side-effect free.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from piceli.cli_contract import EXIT_FAILED, emit_json, reject, say
from piceli.infra.machines.secrets_status import MISMATCH

if TYPE_CHECKING:
    from piceli.infra.machines.secrets_status import Item

RefArgument = Annotated[
    str,
    typer.Argument(
        help="MODULE (or file.py), or MODULE:ATTR for one declaration: an "
        "Infrastructure, Bucket, NixCache, NixHost, NixRelease, DevBuilds, or a "
        "composition",
        show_default=False,
    ),
]
JsonOption = Annotated[
    bool, typer.Option("--json", help="Print one JSON object on stdout")
]
_INTO = {"nix-signing-key": "NixCache(public_key=...)", "age-identity": ".sops.yaml"}
_TITLES = {
    "provider": "1. Provider credentials: only you have them",
    "profile": "1. Cluster profiles: kubeconfig files only you have",
    "generated": "2. Keys Piceli generates",
    "user-data": "3. User data: needed only when infra apply creates the server",
    "s3": "4. S3 key pairs",
}


def _document(ref: str) -> dict[str, Any]:
    from piceli.infra.machines.secrets_status import report
    from piceli.k8s.cli.host import _is_ref, load_module_of

    module = load_module_of(ref)
    name, attr = ref, None
    if _is_ref(ref):
        name, _, attr = ref.rpartition(":")
    try:
        return report(name, vars(module), attr)
    except LookupError:
        reject("infra-not-found", f"{name} has no attribute {attr}")
    except ValueError:
        reject(
            "secrets-nothing-declared",
            f"{ref} declares no Infrastructure, Bucket, NixCache, NixHost, "
            "NixRelease, DevBuilds or Cluster",
        )


def secrets_status(ref: RefArgument, as_json: JsonOption = False) -> None:
    """Every secret a declaration needs: present or missing, who creates it, the next command.

    For each secret: present, missing, incomplete (a credential that lacks a
    part) or damaged; who creates it (infra-apply: piceli infra apply;
    piceli: --generate; owner: --prompt, only you have it); the exact next
    command; what it is for and where else it must live. Then the backup's
    state and warnings. Never prints a value. Exit 0: every secret stored
    and complete; 1: something missing, incomplete or damaged.
    """
    from piceli.infra.machines.secrets_status import table

    document = _document(ref)
    for line in table(document):
        say(line)
    if as_json:
        emit_json(document)
    if not document["complete"]:
        raise typer.Exit(EXIT_FAILED)


# ------------------------------------------------------------- setup


def _interactive() -> bool:
    return bool(
        sys.stdin is not None
        and sys.stdin.isatty()
        and sys.stderr is not None
        and sys.stderr.isatty()
    )


def _piped_parts(shape: Any) -> dict[str, str] | None:
    """The parts of ``shape``, one line each from piped stdin; ``None`` when
    stdin ended before the first one (nothing to read: skipped)."""
    from piceli.infra.machines.credential_parts import check_part

    parts: dict[str, str] = {}
    for step, part in enumerate(shape.parts):
        line = sys.stdin.readline() if sys.stdin is not None else ""
        if not line and step == 0:
            return None
        value = line.rstrip("\r\n")
        check_part(shape, part, value, parts)
        parts[part.key] = value
        del value, line
    return parts


class _Walk:
    """One run of ``setup``: the results, and whether prompts still read."""

    def __init__(self, *, yes: bool, as_json: bool, interactive: bool) -> None:
        self.yes = yes
        self.as_json = as_json
        self.interactive = interactive
        self.reading = True
        self.step = 0
        self.results: list[dict[str, Any]] = []

    def result(self, item: Item | None, state: str, **fields: Any) -> None:
        self.step += 1
        body: dict[str, Any] = {"step": self.step, "state": state}
        if item is not None:
            body.update(group=item.group, name=item.secret.name, kind=item.secret.kind)
        body.update(fields)
        self.results.append(body)
        if self.as_json:
            emit_json(body)

    def ask(self, prompt: str) -> str:
        """One line typed on the terminal (the prompt on stderr: stdout
        stays machine output)."""
        sys.stderr.write(f"{prompt}: ")
        sys.stderr.flush()
        line = sys.stdin.readline() if sys.stdin is not None else ""
        return line.strip()

    def confirm(self, question: str, *, default: bool) -> bool:
        while True:
            answer = self.ask(f"{question} [{'Y/n' if default else 'y/N'}]").lower()
            if not answer:
                return default
            if answer in ("y", "yes", "n", "no"):
                return answer.startswith("y")
            say("  answer y or n")


def _prompt_credential(walk: _Walk, item: Item) -> None:
    """A provider credential or the owner's S3 keys, part by part."""
    from piceli.infra.machines import credential_parts as cp
    from piceli.infra.machines.credentials import save_credential, save_parts
    from piceli.infra.machines.model import InfraError
    from piceli.k8s.cli.infra import _ask_parts

    secret = item.secret
    shape = (
        cp.SHAPES["s3"]
        if secret.kind == "s3-keys"
        else cp.shape_of(secret.provider)
        if secret.provider is not None
        else cp.shape_for_name(secret.name)
    )
    if not walk.reading:
        say(f"  skipped {secret.name}: an earlier answer was refused; run {item.next}")
        walk.result(item, "skipped", next=item.next, why="earlier-refusal")
        return
    say(f"  {secret.name}: {shape.title} ({secret.purpose}); {shape.names()}")
    try:
        if walk.interactive:
            parts: dict[str, str] | None = _ask_parts(shape, from_env=False)
        else:
            parts = _piped_parts(shape)
        if parts is None:
            walk.reading = False
            say(f"  skipped {secret.name}: no input; run {item.next}")
            walk.result(item, "skipped", next=item.next, why="no-input")
            return
        if len(shape.parts) == 1:
            save_credential(
                secret.name, shape.kind, parts[shape.keys[0]], provider=shape.provider
            )
        else:
            save_parts(secret.name, shape.provider, parts)
        del parts
    except InfraError as error:
        if not walk.interactive:
            walk.reading = False
        say(f"  not stored: {error} [{error.code}]")
        walk.result(item, "failed", reason=error.code, next=item.next,
                    **({"part": error.fields["part"]} if "part" in error.fields else {}))  # fmt: skip
        return
    say(f"  stored {secret.name}: {shape.names()}; no value was printed")
    walk.result(item, "stored", parts=shape.keys)


def _generate(walk: _Walk, item: Item, key_names: Mapping[str, list[str]]) -> None:
    from piceli.dev.nix import generate_key, public_key
    from piceli.hosts.age import generate_identity, recipient
    from piceli.infra.machines.credentials import generate_passphrase, save_credential

    secret = item.secret
    if item.detail == MISMATCH:
        # The key is sound; the declaration names another. Replacing it would
        # not help: its public key goes into the declaration.
        from piceli.infra.machines.credentials import load_credential

        stored = public_key(load_credential(secret.name, secret.kind))
        say(f"  {secret.name}: the declaration names another key; public key: {stored}")
        say("  put it in NixCache(public_key=...), or restore the key hosts trust")
        walk.result(item, "copy", public_key=stored)
        return
    if item.state == "damaged":
        say(f"  {secret.name} ({secret.kind}) is damaged: {item.detail}")
        if not (
            walk.interactive
            and walk.confirm(
                f"  Replace {secret.name} with a new key? Whatever trusts the old "
                "one stops trusting it",
                default=False,
            )
        ):
            say(f"  kept {secret.name}; to replace it: {item.next}")
            walk.result(item, "kept", next=item.next)
            return
    elif walk.interactive and not walk.yes:
        if not walk.confirm(
            f"  Generate {secret.kind} {secret.name} now?", default=True
        ):
            say(f"  skipped {secret.name}; later: {item.next}")
            walk.result(item, "skipped", next=item.next, why="declined")
            return
    elif not walk.yes:
        say(f"  not generated {secret.name}: pass --yes, or run {item.next}")
        walk.result(item, "needs-yes", next=item.next)
        return
    public: str | None = None
    if secret.kind == "state-key":
        value = generate_passphrase()
    elif secret.kind == "nix-signing-key":
        names = key_names.get(secret.name) or [secret.name]
        value = generate_key(names[0])
        public = public_key(value)
    else:
        value = generate_identity()
        public = recipient(value)
    save_credential(secret.name, secret.kind, value)
    del value
    say(f"  generated {secret.name} ({secret.kind}); the secret was not printed")
    if secret.kind == "state-key":
        say("  back it up: the state cannot be read without it")
    elif secret.kind == "nix-signing-key":
        say(f"  public key: {public}")
        say("  put it in NixCache(public_key=...) in your declaration")
    else:
        say(f"  public key: {public}")
        say("  add it to .sops.yaml (the key your host's secrets are encrypted to)")
    walk.result(item, "generated", **({"public_key": public} if public else {}))


def _user_data(walk: _Walk, item: Item) -> None:
    from piceli.infra.machines.credentials import save_credential
    from piceli.infra.machines.plan import user_data_digest
    from piceli.k8s.cli.infra import _read_secret

    secret = item.secret
    say(f"  {secret.name}: {secret.purpose}; {secret.where or ''}".rstrip("; "))
    say(
        "  a bootstrap key in it should be short-lived (single-use, expiring in "
        "an hour): create it right before infra apply"
    )
    if not (
        walk.interactive and walk.confirm(f"  Store {secret.name} now?", default=False)
    ):
        say(f"  later, right before infra apply: {item.next}")
        walk.result(item, "later", next=item.next)
        return
    value = _read_secret("  User data (not echoed): ")
    from piceli.infra.machines.model import InfraError

    try:
        save_credential(secret.name, "user-data", value)
    except InfraError as error:
        say(f"  not stored: {error} [{error.code}]")
        walk.result(item, "failed", reason=error.code, next=item.next)
        return
    digest, size = user_data_digest(value), len(value.encode())
    del value
    say(f"  stored {secret.name} ({size} bytes, {digest}); not printed")
    walk.result(item, "stored", digest=digest, bytes=size)


def _step(walk: _Walk, item: Item, key_names: Mapping[str, list[str]]) -> None:
    if item.state == "pending":
        say(
            f"  {item.secret.name}: {item.secret.purpose}, {item.detail}; nothing "
            f"to do here; next: {item.next}"
        )
        walk.result(item, "later", next=item.next)
        return
    if not item.blocking:
        say(f"  {item.state}: {item.secret.name} ({item.secret.kind})")
        walk.result(item, item.state)
        return
    group = item.group
    if group == "profile":
        say(f"  {item.secret.name}: {item.secret.purpose}; run {item.next}")
        walk.result(item, "later", next=item.next)
    elif group == "generated":
        _generate(walk, item, key_names)
    elif group == "user-data":
        _user_data(walk, item)
    elif item.created_by == "infra-apply":
        say(
            f"  {item.secret.name}: {item.secret.purpose}, made by piceli infra "
            f"apply; nothing to do here; next: {item.next}"
        )
        walk.result(item, "later", next=item.next)
    else:
        _prompt_credential(walk, item)


def _backup(walk: _Walk) -> None:
    from piceli.infra.machines import secrets_backup as sb
    from piceli.infra.machines.model import InfraError
    from piceli.infra.machines.secrets_status import backup_state

    state = backup_state()
    say("5. Backup")
    if state["state"] in ("current", "empty"):
        say(f"  {state['state']}")
        walk.result(None, state["state"], group="backup")
        return
    say(f"  backup: {state['state']}")
    if not walk.interactive:
        then = (
            ", then piceli secrets backup --to PATH"
            if "backup-key" in str(state["next"])
            else ""
        )
        say(f"  later: {state['next']}{then}")
        walk.result(None, "later", group="backup", next=state["next"])
        return
    try:
        if state["recipients"] < 2:
            out = walk.ask(
                "  Offline backup key: a path outside the store (a USB drive), "
                "empty to skip"
            )
            if out:
                created = sb.generate_backup_key(Path(out), add=True)
                say(f"  wrote the offline key to {created['path']} (0600); recipient: "
                    f"{created['recipient']}")  # fmt: skip
                say("  keep it offline and delete it from this machine")
            if len(sb.load_config().get("recipients") or ()) < 2:
                own = walk.ask(
                    "  Your age recipient (age1..., from your password manager), "
                    "empty to skip"
                )
                if own:
                    sb.remember_recipients([own])
        count = len(sb.load_config().get("recipients") or ())
        if count < 2:
            more = "--recipient age1..." if count else "--recipient age1... " * 2
            say(f"  a backup needs two recipients ({count} remembered); later: "
                f"piceli secrets backup --to PATH {more.strip()}")  # fmt: skip
            walk.result(None, "later", group="backup", next=state["next"])
            return
        to = walk.ask("  Write a backup now: its path, empty to skip")
        if not to:
            walk.result(None, "later", group="backup",
                        next="piceli secrets backup --to PATH")  # fmt: skip
            return
        done = sb.backup(Path(to))
    except InfraError as error:
        say(f"  not backed up: {error} [{error.code}]")
        walk.result(None, "failed", group="backup", reason=error.code)
        return
    say(f"  backed up {done['count']} files to {done['path']}; {done['sha256']}")
    walk.result(None, "backed-up", group="backup", path=done["path"],
                sha256=done["sha256"])  # fmt: skip


def _deferred(row: Mapping[str, Any]) -> bool:
    """Left for later on purpose: user data (right before apply) and the
    pairs infra apply makes."""
    return row["state"] == "pending" or (
        row["state"] == "missing" and row["kind"] == "user-data"
    )


def _key_names(ref: str) -> dict[str, list[str]]:
    from piceli.infra.machines.secrets_status import declarations
    from piceli.k8s.cli.host import load_module_of

    found: dict[str, list[str]] = {}
    for value in declarations(vars(load_module_of(ref)).values()):
        cache = getattr(getattr(value, "host", value), "cache", value)
        if hasattr(cache, "signing_credential") and hasattr(cache, "key_name"):
            found.setdefault(cache.signing_credential, []).append(cache.key_name)
    return found


def secrets_setup(
    ref: RefArgument,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            help="Generate the keys Piceli generates without asking (never "
            "replaces one, never answers a prompt)",
        ),
    ] = False,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print one JSON object per step on stdout")
    ] = False,
) -> None:
    """Walk through the missing secrets of a declaration, in order.

    \b
    1. provider credentials: asked for part by part (piped: one part per line);
    2. keys Piceli generates (state key, Nix cache signing key, age identity):
       generated here, printing only the public halves to copy into the
       declaration or .sops.yaml; a damaged key is replaced only when you
       confirm it on a terminal;
    3. user data: offered on a terminal, skipped by default (store a
       short-lived key right before infra apply);
    4. S3 key pairs: infra apply makes them; nothing is created here;
    5. a backup, offered when none is current.

    Without a terminal, --yes generates; prompts read piped input. Never
    prints a value. Exit 0 when everything is stored except what is left
    for later on purpose (user data, the pairs infra apply makes); 1 when
    something is still missing, incomplete or damaged.
    """
    from piceli.infra.machines.secrets_status import items_of, ordered

    document = _document(ref)
    names = _key_names(ref)
    walk = _Walk(yes=yes, as_json=as_json, interactive=_interactive())
    group = None
    for item in ordered(items_of(document)):
        if item.group != group:
            group = item.group
            say(_TITLES[group])
        _step(walk, item, names)
    _backup(walk)
    after = _document(ref)
    generated = {
        r["name"]: r for r in walk.results if r["state"] in ("generated", "copy")
    }
    copy = [
        {"name": name, "kind": r["kind"], "public_key": r["public_key"],
         "into": _INTO[r["kind"]]}
        for name, r in generated.items() if r["kind"] in _INTO
    ]  # fmt: skip
    remaining = [
        {"name": r["name"], "kind": r["kind"], "state": r["state"], "next": r["next"]}
        for r in after["secrets"]
        if r["state"] in ("missing", "incomplete", "damaged")
        and not _deferred(r)
        # Generated just now: the declaration names another key until the
        # owner copies the new public key into it.
        and r["name"] not in generated
    ]
    later = [
        {"name": r["name"], "kind": r["kind"], "next": r["next"]}
        for r in after["secrets"]
        if _deferred(r)
    ]
    for row in later:
        say(f"later: {row['name']} ({row['kind']}): {row['next']}")
    for row in copy:
        say(f"copy: {row['public_key']} into {row['into']} ({row['name']})")
    for row in remaining:
        say(f"still {row['state']}: {row['name']} ({row['kind']}): {row['next']}")
    done = not remaining
    say(
        "done: every secret Piceli can set up now is stored"
        if done
        else f"not done: {len(remaining)} secret(s) still need you"
    )
    if as_json:
        emit_json({"state": "done" if done else "incomplete", "remaining": remaining,
                   "later": later, "copy": copy, "counts": after["counts"]})  # fmt: skip
    if not done:
        raise typer.Exit(EXIT_FAILED)


def register(secrets_app: typer.Typer) -> None:
    secrets_app.command("status")(secrets_status)
    secrets_app.command("setup")(secrets_setup)
