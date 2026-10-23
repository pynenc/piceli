"""Every secret a module's declarations need, and whether it is stored (0.25.0).

What ``piceli secrets status MODULE[:ATTR]`` prints and ``piceli secrets
setup`` walks through. :func:`declarations` finds the declarations among a
module's values (``Infrastructure``, ``Bucket``, ``NixCache``, ``NixHost``,
``NixRelease``, ``DevBuilds``, and the ``Cluster`` of a composition, whose
credentials are a ``piceli login`` profile); :func:`report` lists what they
need (:func:`~piceli.infra.machines.keys.needed_secrets`), each with:

- its state: ``present``, ``missing``, ``incomplete`` (a credential that
  lacks a part: :func:`~piceli.infra.machines.credential_parts.credential_report`),
  ``damaged`` (unreadable, readable by others, another kind, or a Nix
  signing key whose public key is not the declared one), ``copy`` (a Nix
  signing key stored for a cache declared with ``key_name=`` alone: its
  public key goes into ``NixCache(public_key=)``), or ``not-needed``
  (the user data of a server that exists: read only to create it again);
- who creates it: ``infra-apply`` (``piceli infra apply``), ``piceli``
  (``--generate``) or ``owner`` (``--prompt``: only the owner has it);
- the exact next command, what it is for and where else it must live;

and the backup's state (:func:`~piceli.infra.machines.secrets_backup.staleness`)
and one-line warnings. Names, kinds and states only: a value is read in
memory to check it (a signing key's public key, an age identity's shape)
and never returned or printed.

Importing this module is side-effect free.
"""

from __future__ import annotations

import json
import stat
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from piceli.infra.machines.keys import NeededSecret, needed_secrets

__all__ = [
    "GENERATED",
    "MISMATCH",
    "STATES",
    "STATUS_SCHEMA",
    "Item",
    "backup_state",
    "declarations",
    "report",
    "table",
]

STATUS_SCHEMA = "piceli.secrets-status.v1"
#: Kinds ``piceli secrets ... --generate`` creates: no one has to type them.
GENERATED = frozenset({"state-key", "nix-signing-key", "age-identity"})
STATES = (
    "present", "pending", "missing", "incomplete", "damaged", "copy", "not-needed",
)  # fmt: skip
#: The order ``setup`` walks them in: what the owner types, what Piceli
#: generates, the user data (right before apply), the S3 pairs.
GROUPS = ("provider", "profile", "generated", "user-data", "s3")
_REPLACE = {"state-key", "nix-signing-key", "age-identity"}
PENDING = "created by the next infra apply"
MISMATCH = (
    "its public key is not NixCache(public_key=): put the stored key's public "
    "key (the next command prints it) in the declaration, or restore the key "
    "hosts trust"
)


@dataclass(frozen=True)
class Item:
    """One needed secret and what is stored under its name."""

    secret: NeededSecret
    state: str
    created_by: str
    next: str | None
    detail: str | None = None

    @property
    def group(self) -> str:
        kind = self.secret.kind
        if kind == "profile":
            return "profile"
        if kind in GENERATED:
            return "generated"
        if kind == "user-data":
            return "user-data"
        if kind == "s3-keys":
            return "s3"
        return "provider"

    @property
    def blocking(self) -> bool:
        return self.state in ("missing", "incomplete", "damaged", "copy")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.secret.name,
            "kind": self.secret.kind,
            "state": self.state,
            "created_by": self.created_by,
            "next": self.next,
            "purpose": self.secret.purpose,
            "where": self.secret.where,
            "detail": self.detail,
            "provider": self.secret.provider,
            "build_host": self.secret.build_host,
        }


@dataclass
class _Index:
    """What the declarations say about each secret, beyond its name."""

    caches: dict[str, list[Any]] = field(default_factory=dict)
    infra_refs: dict[str, str] = field(default_factory=dict)
    key_infra: dict[str, Any] = field(default_factory=dict)
    user_data: dict[str, tuple[Any, Any]] = field(default_factory=dict)
    registered: dict[int, tuple[str, str]] = field(default_factory=dict)


# ------------------------------------------------------------- declarations


def _kinds() -> tuple[type, ...]:
    from piceli.dev.model import DevBuilds
    from piceli.dev.nix import NixCache
    from piceli.hosts.model import NixHost
    from piceli.infra import Cluster, Infrastructure
    from piceli.infra.machines.model import Bucket
    from piceli.infra.nix_releases import NixRelease

    return (Infrastructure, Bucket, NixCache, NixHost, NixRelease, DevBuilds, Cluster)


def declarations(values: Iterable[Any]) -> list[Any]:
    """The declarations among ``values`` (lists and tuples opened, each once,
    in order), with the clusters a composition's environments name."""
    from piceli.envs.model import BranchEnvironments, Environment

    kinds = _kinds()
    found: dict[int, Any] = {}
    items = list(values)
    while items:
        value = items.pop(0)
        if isinstance(value, list | tuple):
            items[:0] = list(value)
        elif isinstance(value, Environment | BranchEnvironments):
            clusters = [
                value.cluster,
                *(getattr(p, "cluster", p) for p in value.clusters),
            ]
            items[:0] = [c for c in clusters if c is not None]
        elif isinstance(value, kinds):
            found.setdefault(id(value), value)
    return list(found.values())


def _describe(value: Any, names: Mapping[int, str]) -> dict[str, Any]:
    kind = type(value).__name__
    for base in _kinds():
        if isinstance(value, base):
            kind = base.__name__
    name = getattr(value, "name", None)
    if kind == "NixCache":
        name = value.key_name
    elif kind == "NixRelease":
        name = value.host.name
    elif not isinstance(name, str):
        name = names.get(id(value))
    return {"kind": kind, "name": name, "attr": names.get(id(value))}


def _profiles(decls: list[Any], index: _Index) -> list[NeededSecret]:
    from piceli.infra import Cluster

    found: dict[str, NeededSecret] = {}
    for cluster in decls:
        if not isinstance(cluster, Cluster) or cluster.credentials is None:
            continue
        name = cluster.credentials
        if name in found:
            continue
        registered = index.registered.get(id(cluster))
        setup = (
            f"piceli infra register {registered[0]} {registered[1]}"
            if registered
            else f"piceli login {name} --kubeconfig FILE --context NAME"
        )
        found[name] = NeededSecret(
            name, "profile", "operator", setup,
            f"kubeconfig profile of cluster {cluster.name}",
            where="a reference to a kubeconfig file on this machine; the file "
            "itself is not in the store",
        )  # fmt: skip
    return list(found.values())


def _index(decls: list[Any], module: str, names: Mapping[int, str]) -> _Index:
    from piceli.dev.nix import NixCache
    from piceli.hosts.model import NixHost
    from piceli.infra import Infrastructure
    from piceli.infra.nix_releases import NixRelease

    index = _Index()

    def cache(item: Any) -> None:
        caches = index.caches.setdefault(item.signing_credential, [])
        if all(c.public_key != item.public_key for c in caches):
            caches.append(item)

    for value in decls:
        if isinstance(value, Infrastructure):
            attr = names.get(id(value))
            ref = f"{module}:{attr}" if attr else None
            for key, _bucket in value.s3_keys():
                index.key_infra.setdefault(key.name, value)
                if ref is not None:
                    index.infra_refs.setdefault(key.name, ref)
            for server in value.servers:
                if server.user_data is not None:
                    index.user_data.setdefault(server.user_data.secret, (value, server))
                if server.cluster is not None and ref is not None:
                    index.registered.setdefault(id(server.cluster), (ref, server.name))
        elif isinstance(value, NixCache):
            cache(value)
        elif isinstance(value, NixHost):
            cache(value.cache)
        elif isinstance(value, NixRelease):
            cache(value.host.cache)
    return index


# ------------------------------------------------------------- states


def _read(name: str) -> tuple[dict[str, Any] | None, str | None]:
    """The stored JSON of ``name``, or why it is damaged (never a value)."""
    from piceli.infra.machines.credentials import _path

    path = _path(name)
    try:
        info = path.stat()
    except FileNotFoundError:
        return None, None
    if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        return None, "readable by other users: chmod 600 it"
    try:
        body = json.loads(path.read_text())
    except (OSError, ValueError, UnicodeDecodeError):
        return None, "unreadable"
    if not isinstance(body, dict):
        return None, "unreadable"
    return body, None


def _created(infra: Any, server: Any) -> bool:
    """Whether the infrastructure's records say ``server`` exists."""
    from piceli.infra.machines.model import InfraError
    from piceli.infra.machines.status import summarize

    try:
        document = summarize(infra)
    except (InfraError, OSError):
        return False
    return any(
        row.get("name") == server.name and row.get("state") == "created"
        for row in document.get("servers") or ()
    )


def _profile_state(name: str) -> tuple[str, str | None]:
    from piceli.profiles import ProfileError, load_profile

    try:
        profile = load_profile(name)
    except ProfileError as error:
        if error.code == "profile-not-found":
            return "missing", None
        return "damaged", "unreadable"
    if not profile.kubeconfig.is_file():
        return "damaged", "its kubeconfig file is gone"
    return "present", None


def copy_detail(public: str) -> str:
    """What a ``copy`` state says: the public key goes into the declaration."""
    return f"put {public} into NixCache(public_key=)"


def _value_state(
    secret: NeededSecret, body: Mapping[str, Any], index: _Index
) -> tuple[str, str | None]:
    """A one-value kind: there, and (signing key, age identity) usable."""
    value = body.get("value")
    if not isinstance(value, str) or not value:
        return "damaged", "no value stored"
    if secret.kind == "nix-signing-key":
        from piceli.dev.model import DevError
        from piceli.dev.nix import public_key

        try:
            public = public_key(value)
        except (DevError, ValueError):
            return "damaged", "not a Nix secret key"
        finally:
            del value
        caches = index.caches.get(secret.name, ())
        declared = [c.public_key for c in caches if c.public_key is not None]
        if declared and public not in declared:
            return "damaged", MISMATCH
        if any(c.public_key is None for c in caches):
            # Declared with key_name= alone: the declaration waits for it.
            return "copy", copy_detail(public)
    elif secret.kind == "age-identity":
        from piceli.hosts.age import AgeError, check_identity

        try:
            check_identity(value)
        except (AgeError, ValueError):
            return "damaged", "not an age identity"
        finally:
            del value
    return "present", None


def _applied(name: str, index: _Index) -> bool:
    """Whether the infrastructure creating key pair ``name`` records it as
    applied (``True`` when no declared infrastructure tells)."""
    from piceli.infra.machines.keys import KEYS_RECORD
    from piceli.infra.machines.model import InfraError
    from piceli.infra.machines.state import read_record, state_dir

    infra = index.key_infra.get(name)
    if infra is None:
        return True
    try:
        directory = state_dir(infra.name, infra.state_dir, create=False)
    except InfraError:
        return True
    record = read_record(directory, KEYS_RECORD) if directory.is_dir() else {}
    return any(
        isinstance(item, dict) and item.get("name") == name for item in record.values()
    )


def _state(secret: NeededSecret, index: _Index) -> tuple[str, str | None]:
    from piceli.infra.machines.credential_parts import credential_report

    if secret.kind == "profile":
        return _profile_state(secret.name)
    body, why = _read(secret.name)
    if why is not None:
        return "damaged", why
    if body is None:
        if secret.created_by == "infra-apply" and not _applied(secret.name, index):
            return "pending", PENDING
        if secret.kind == "user-data" and secret.name in index.user_data:
            infra, server = index.user_data[secret.name]
            if _created(infra, server):
                return "not-needed", (
                    f"{server.name} exists: a plan reads it only to create "
                    f"{server.name} again"
                )
        return "missing", None
    stored = body.get("kind")
    if stored != secret.kind:
        held = f"a {stored}" if isinstance(stored, str) else "no known kind"
        return "damaged", f"holds {held}, not a {secret.kind}"
    if secret.kind in ("provider-token", "s3-keys"):
        found = credential_report(
            secret.name, secret.provider if secret.kind == "provider-token" else None
        )
        if found["complete"]:
            return "present", None
        if found["storage"] == "damaged":
            return "damaged", "unreadable"
        return "incomplete", str(found["message"])
    return _value_state(secret, body, index)


def _creator(secret: NeededSecret) -> str:
    if secret.created_by == "infra-apply":
        return "infra-apply"
    return "piceli" if secret.kind in GENERATED else "owner"


def _setup(secret: NeededSecret) -> str:
    from piceli.infra.machines.credential_parts import fix_command, shape_of

    if secret.kind == "provider-token" and secret.provider is not None:
        return fix_command(secret.name, shape_of(secret.provider))
    if secret.kind == "user-data":
        return f"piceli secrets user-data {secret.name} --prompt"
    return secret.setup


def _next(
    secret: NeededSecret, state: str, detail: str | None, index: _Index
) -> tuple[str | None, str | None]:
    """The exact next command, and the detail it comes with."""
    if state in ("present", "not-needed"):
        return None, detail
    if secret.created_by == "infra-apply":
        ref = index.infra_refs.get(secret.name)
        plan = f"piceli infra plan {ref}" if ref else secret.setup
        if state == "pending":
            return plan, detail
        if state == "missing":
            if secret.name in index.key_infra:
                return plan, (
                    f"applied, but not in this store: {plan} stores it again "
                    "from the state"
                )
            return plan, detail or "created and stored by piceli infra apply"
        return (
            f"piceli secrets delete {secret.name} --kind s3 --yes",
            f"{detail}; then {plan} stores it again from the state",
        )
    if state == "copy":
        return f"piceli secrets nix-cache {secret.name}", detail
    command = _setup(secret)
    if detail == MISMATCH:
        # The key is fine; the declaration names another one.
        return f"piceli secrets nix-cache {secret.name}", detail
    if state == "damaged" and secret.kind in _REPLACE:
        command += " --replace"
        detail = (
            f"{detail}; restore it from a backup (piceli secrets restore PATH) "
            "rather than replace a key something already trusts"
        )
    return command, detail


def transfer_command(names: Iterable[str], host: str) -> str:
    """The backup that carries ``names`` to build host ``host`` alone."""
    only = " ".join(f"--only {name}" for name in names)
    return f"piceli secrets backup {only} --to PATH --recipient AGE_RECIPIENT_OF_{host}"


def _items(secrets: list[NeededSecret], index: _Index) -> list[Item]:
    items: list[Item] = []
    for secret in secrets:
        state, detail = _state(secret, index)
        command, detail = _next(secret, state, detail, index)
        items.append(Item(secret, state, _creator(secret), command, detail))
    # Present here and needed on a release's build host: move them there.
    hosts: dict[str, list[str]] = {}
    for item in items:
        if item.state == "present" and item.secret.build_host is not None:
            hosts.setdefault(item.secret.build_host, []).append(item.secret.name)
    for at, item in enumerate(items):
        host = item.secret.build_host
        if item.state == "present" and host is not None:
            items[at] = Item(
                item.secret, item.state, item.created_by,
                transfer_command(sorted(hosts[host]), host),
                f"needed on {host} too, unless it is there already; then on "
                f"{host}: piceli secrets restore PATH --identity KEY, and "
                "delete PATH on both machines",
            )  # fmt: skip
    return items


# ------------------------------------------------------------- backup


def backup_state() -> dict[str, Any]:
    """The backup's state: ``empty`` (nothing stored), ``none``, ``stale``,
    ``single-recipient`` or ``current``, with the next command."""
    from piceli.infra.machines.credentials import credentials_dir
    from piceli.infra.machines.secrets_backup import load_config, staleness

    found = staleness()
    recipients = len(load_config().get("recipients") or ())
    directory = credentials_dir()
    stored = directory.is_dir() and any(
        p.is_file() and not p.name.startswith(".") for p in directory.glob("*.json")
    )
    if not stored:
        state = "empty"
    elif found["last_backup"] is None:
        state = "none"
    elif found["stale"]:
        state = "stale"
    elif found["single_recipient"]:
        state = "single-recipient"
    else:
        state = "current"
    command = None
    if state in ("none", "stale", "single-recipient"):
        command = (
            "piceli secrets backup-key --out PATH --add"
            if recipients < 2
            else "piceli secrets backup --to PATH"
        )
    return {**found, "state": state, "recipients": recipients, "next": command}


# ------------------------------------------------------------- warnings


def _warnings(decls: list[Any], items: list[Item]) -> list[dict[str, str]]:
    from piceli.infra import Infrastructure
    from piceli.infra.machines.model import Bucket

    found: list[dict[str, str]] = []
    buckets: dict[int, Any] = {}
    for value in decls:
        if isinstance(value, Bucket):
            buckets.setdefault(id(value), value)
        elif isinstance(value, Infrastructure):
            for bucket in value.buckets:
                buckets.setdefault(id(bucket), bucket)
        cache = getattr(getattr(value, "host", value), "cache", value)
        bucket = getattr(cache, "bucket", None)
        if isinstance(bucket, Bucket):
            buckets.setdefault(id(bucket), bucket)
    for bucket in buckets.values():
        for key in bucket.keys:
            if key.access != "read":
                continue
            limits = bucket.provider.s3_key_enforcement(key)
            if not limits.get("read_only_enforced"):
                found.append({
                    "kind": "s3-read-not-enforced",
                    "name": key.name,
                    "message": f"S3 key {key.name} (bucket {bucket.name}) is not "
                    f"read-only at {bucket.provider.name}: hosts only read with "
                    "it, but the cloud lets it write",
                    "how": str(limits.get("how") or ""),
                })  # fmt: skip
    for item in items:
        if item.secret.kind == "user-data" and item.state == "present":
            found.append({
                "kind": "user-data-spent",
                "name": item.secret.name,
                "message": f"{item.secret.name}: once the server exists, delete "
                f"the spent key: piceli secrets delete {item.secret.name} "
                "--kind user-data",
                "how": "",
            })  # fmt: skip
    return found


# ------------------------------------------------------------- report


def report(
    module: str, namespace: Mapping[str, Any], attr: str | None = None
) -> dict[str, Any]:
    """The ``piceli.secrets-status.v1`` document of a module's declarations
    (``attr``: only that one). Raises ``LookupError`` when ``attr`` is
    missing and ``ValueError`` when nothing is declared."""
    names: dict[int, str] = {}
    for key, value in namespace.items():
        if not key.startswith("_"):
            names.setdefault(id(value), key)
    if attr is not None:
        if attr not in namespace:
            raise LookupError(attr)
        values: list[Any] = [namespace[attr]]
    else:
        values = [v for k, v in namespace.items() if not k.startswith("_")]
    decls = declarations(values)
    if not decls:
        raise ValueError("no declaration")
    # The whole module tells which infrastructure creates a selected pair.
    every = declarations(v for k, v in namespace.items() if not k.startswith("_"))
    index = _index(every, module, names)
    from piceli.infra import Cluster

    secrets = needed_secrets([d for d in decls if not isinstance(d, Cluster)])
    secrets += _profiles(decls, index)
    items = ordered(_items(secrets, index))
    counts = {state: sum(i.state == state for i in items) for state in STATES}
    return {
        "schema": STATUS_SCHEMA,
        "module": module,
        "attr": attr,
        "declarations": [_describe(d, names) for d in decls],
        "secrets": [i.to_dict() for i in items],
        "counts": counts,
        "complete": not any(i.blocking for i in items),
        "backup": backup_state(),
        "warnings": _warnings(decls, items),
    }


def items_of(document: Mapping[str, Any]) -> list[Item]:
    """The report's secrets as :class:`Item` (what ``setup`` walks)."""
    found: list[Item] = []
    for row in document.get("secrets") or ():
        secret = NeededSecret(
            row["name"], row["kind"],
            "infra-apply" if row["created_by"] == "infra-apply" else "operator",
            row["next"] or "", row["purpose"], row["provider"], row["where"],
            row.get("build_host"),
        )  # fmt: skip
        found.append(
            Item(secret, row["state"], row["created_by"], row["next"], row["detail"])
        )
    return found


def ordered(items: Iterable[Item]) -> list[Item]:
    """Items in the order ``setup`` walks them (:data:`GROUPS`)."""
    return sorted(items, key=lambda i: GROUPS.index(i.group))


def table(document: Mapping[str, Any]) -> list[str]:
    """The human view: one line per secret, what each is for, the backup,
    the warnings and a summary."""
    rows = [("NAME", "KIND", "STATE", "BY", "NEXT")]
    for row in document["secrets"]:
        rows.append((row["name"], row["kind"], row["state"], row["created_by"],
                     row["next"] or "-"))  # fmt: skip
    widths = [max(len(r[i]) for r in rows) for i in range(4)]
    head = [
        f"secrets of {document['module']}"
        + (f":{document['attr']}" if document.get("attr") else "")
        + ": "
        + ", ".join(
            f"{d['kind']} {d['name']}" if d.get("name") else d["kind"]
            for d in document["declarations"]
        )
    ]
    lines = head + [
        "  ".join([*(c.ljust(w) for c, w in zip(r[:4], widths, strict=True)), r[4]])
        for r in rows
    ]
    width = (
        max(len(r["name"]) for r in document["secrets"]) if document["secrets"] else 0
    )
    lines.append("")
    lines.append("what each is for:")
    for row in document["secrets"]:
        text = row["purpose"]
        if row.get("where"):
            text += f"; {row['where']}"
        if row.get("detail"):
            text += f" [{row['detail']}]"
        lines.append(f"  {row['name'].ljust(width)}  {text}")
    backup = document["backup"]
    said = {
        "empty": "nothing stored yet",
        "none": "no backup yet",
        "stale": "stale: newer than the last backup: " + ", ".join(backup["newer"]),
        "single-recipient": "the last backup has a single recipient",
        "current": f"current ({backup['last_backup']})",
    }[backup["state"]]
    lines.append("")
    lines.append(
        f"backup: {said}" + (f"; next: {backup['next']}" if backup["next"] else "")
    )
    for warning in document["warnings"]:
        lines.append(f"warning: {warning['message']}")
    counts = document["counts"]
    summary = ", ".join(f"{counts[s]} {s}" for s in STATES if counts[s])
    if document["complete"]:
        lines.append(f"summary: {summary}; every secret is stored")
    else:
        target = document["module"] + (
            f":{document['attr']}" if document.get("attr") else ""
        )
        lines.append(
            f"summary: {summary}; piceli secrets setup {target} walks through them"
        )
    return lines
