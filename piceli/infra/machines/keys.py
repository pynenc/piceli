"""S3 key pairs ``piceli infra apply`` creates, and every secret a declaration needs.

A bucket's :class:`~piceli.infra.machines.model.S3Key` pairs are OpenTofu
resources (an EC2 credential at OpenStack). Their secrets end up in the
state, so a plan that holds key pairs is refused unless the state and plan
files are encrypted and unencrypted ones refused (``infra-state-unencrypted``;
Piceli always configures that, the check keeps it so). After an apply
(also a partial one) :func:`store_keys` reads each pair out of the state and
stores it as the local credential of the key's name (kind ``s3-keys``,
by part: ``access_key_id`` and ``secret_access_key``, ``0600``), where ``NixCache`` and ``piceli host
secrets`` load it; a pair whose resource is gone is deleted from the store,
if it still holds what Piceli wrote. ``s3-keys.json`` in the state directory
records, per key, its credential name and a short digest of what was stored
(never the value). Values are added to OpenTofu's redaction list as soon as
they are known and are never printed.

:func:`needed_secrets` lists every secret a declaration needs (name, kind,
who creates it, how): what ``piceli secrets status`` and ``setup`` read.

Importing this module is side-effect free.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from piceli.infra.machines.model import InfraError, Infrastructure

if TYPE_CHECKING:
    from pathlib import Path

    from piceli.infra.machines.plan import Change, Workspace
    from piceli.infra.machines.tofu import TofuResult

__all__ = [
    "KEYS_RECORD",
    "NeededSecret",
    "known_values",
    "needed_secrets",
    "plan_entries",
    "require_encrypted",
    "store_keys",
]

KEYS_RECORD = "s3-keys.json"
KIND = "s3-keys"


def value_digest(value: str) -> str:
    """A short digest of a stored pair: compares, never reveals."""
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()[:16]


def require_encrypted(env: Mapping[str, str]) -> None:
    """Refuse (``infra-state-unencrypted``) unless OpenTofu encrypts and
    enforces both the state and the plan files: key pairs are in them."""
    config = env.get("TF_ENCRYPTION") or ""
    blocks = [part for part in config.split("\n}") if part.strip()]
    enforced = {
        name
        for name in ("state", "plan")
        for block in blocks
        if block.strip().startswith(name + " {") and "enforced = true" in block
    }
    if enforced != {"state", "plan"}:
        raise InfraError(
            "infra-state-unencrypted",
            "this declaration has S3 keys, whose secrets go into the state; "
            "Piceli creates them only into an encrypted, enforced state",
        )


def _record(directory: Path) -> dict[str, Any]:
    from piceli.infra.machines.state import read_record

    return read_record(directory, KEYS_RECORD)


def known_values(infra: Infrastructure, directory: Path) -> list[str]:
    """The stored values of this infrastructure's key pairs, to redact
    (the pair, the access key id and the secret)."""
    from piceli.infra.machines.credentials import credential_present, load_credential

    names = {key.name for key, _ in infra.s3_keys()} | {
        str(item.get("name"))
        for item in _record(directory).values()
        if isinstance(item, dict) and item.get("name")
    }
    found: list[str] = []
    for name in sorted(names):
        try:
            if not credential_present(name):
                continue
            value = load_credential(name, KIND)
        except InfraError:
            continue
        found += [value, *value.split(":", 1)]
    return found


def plan_entries(
    ws: Workspace, changes: Sequence[Change]
) -> tuple[dict[str, Any], ...]:
    """The plan's key pairs: name, bucket, access, action and how the
    provider limits them (never a value). A pair the plan creates under a
    credential name that holds something Piceli did not store is refused
    (``infra-credential-exists``)."""
    from piceli.infra.machines.credentials import credential_present

    record = _record(ws.directory)
    actions = {c.address: c.action for c in changes}
    entries: list[dict[str, Any]] = []
    for key, bucket in ws.infra.s3_keys():
        address = next(
            a for a, (name, _, _) in ws.rendered.s3_keys.items() if name == key.name
        )
        action = actions.get(address, "keep")
        stored = credential_present(key.name)
        if "create" in action or action == "replace":
            if stored and address not in record:
                raise InfraError(
                    "infra-credential-exists",
                    f"credential {key.name!r} exists and was not stored by "
                    f"piceli infra apply; S3Key {key.name} would replace it "
                    f"(piceli secrets delete {key.name}, or another name)",
                )
        entries.append(
            {
                "name": key.name,
                "bucket": bucket.name,
                "access": key.access,
                "action": action,
                "credential": key.name,
                "stored": stored,
                **bucket.provider.s3_key_enforcement(key),
            }
        )
    for change in changes:
        if change.kind == "unknown" and change.address in record:
            item = record[change.address]
            entries.append(
                {
                    "name": item.get("name"),
                    "bucket": item.get("bucket"),
                    "access": item.get("access"),
                    "action": change.action,
                    "credential": item.get("name"),
                    "stored": credential_present(str(item.get("name"))),
                }
            )
    return tuple(entries)


_S3_PARTS = ("access_key_id", "secret_access_key")


def reads_keys(ws: Workspace) -> bool:
    """Whether :func:`store_keys` reads the state: S3 keys are declared or
    recorded."""
    return bool(ws.rendered.s3_keys or _record(ws.directory))


def store_keys(ws: Workspace, shown: TofuResult | None = None) -> list[str]:
    """Store each key pair the state holds; delete those it no longer
    holds. Returns the new values (for redaction); prints nothing.

    ``shown``: 0.26.2, the unredacted ``tofu show -json`` of the state the
    caller read once for this and its inventory (else read here)."""
    from piceli.infra.machines.credentials import (
        credential_present,
        delete_credential,
        load_credential,
        save_parts,
    )
    from piceli.infra.machines.state import write_record

    record = _record(ws.directory)
    if not ws.rendered.s3_keys and not record:
        return []
    if shown is None:
        shown = ws.run("show", "-json", redacted=False)
    try:
        document = json.loads(shown.stdout) if shown.code == 0 else None
    except ValueError:
        document = None
    del shown
    if not isinstance(document, dict):
        raise InfraError("infra-tofu-failed", "tofu show -json did not print the state")
    resources = {
        str(item.get("address")): item
        for item in ((document.get("values") or {}).get("root_module") or {}).get(
            "resources"
        )
        or ()
        if isinstance(item, dict) and item.get("mode") == "managed"
    }
    del document
    fresh: list[str] = []
    kept: dict[str, Any] = {}
    for address, (name, bucket, access) in sorted(ws.rendered.s3_keys.items()):
        item = resources.get(address)
        if item is None:
            continue
        provider = next(b.provider for b in ws.infra.buckets if b.name == bucket)
        pair = provider.s3_key_values(item.get("values") or {})
        if pair is None:
            raise InfraError(
                "infra-tofu-failed", f"the state holds no key pair for S3Key {name}"
            )
        value = ":".join(pair)
        digest = value_digest(value)
        current = load_credential(name, KIND) if credential_present(name) else None
        if current != value:
            save_parts(name, "s3", dict(zip(_S3_PARTS, pair, strict=True)))
            fresh += [value, *pair]
        kept[address] = {"name": name, "bucket": bucket, "access": access,
                         "digest": digest}  # fmt: skip
        del value, pair, current
    for address, item in record.items():
        if address in kept or address in resources or not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        try:
            if (
                name
                and credential_present(name)
                and value_digest(load_credential(name, KIND)) == item.get("digest")
            ):
                delete_credential(name, KIND)
        except InfraError:
            pass
    for address, item in record.items():
        # Still in the state though no longer declared: a failed delete.
        if address not in kept and address in resources:
            kept[address] = item
    write_record(ws.directory, KEYS_RECORD, kept)
    ws.tofu.secrets.extend(fresh)
    return fresh


# ------------------------------------------------------------ needed secrets


@dataclass(frozen=True)
class NeededSecret:
    """One secret a declaration needs: where it is stored, who creates it.

    ``created_by`` is ``"infra-apply"`` (``piceli infra apply`` creates and
    stores it), ``"operator"`` (someone runs ``setup``) or ``"owner"``
    (0.25.3: an S3 pair the owner makes by hand, outside Piceli, and
    stores with ``setup``). ``provider`` is the provider a
    ``provider-token`` is for (its ``Provider.name``); ``where`` says where
    else the secret must live or when it is needed (the host, a build
    host, "read once at creation"), when that matters; ``build_host``
    names the ``NixRelease`` builder whose own store needs it too;
    ``hint`` says how the owner makes it (``openstack ec2 credentials
    create``).
    """

    name: str
    kind: str
    created_by: str
    setup: str
    purpose: str
    provider: str | None = None
    where: str | None = None
    build_host: str | None = None
    hint: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "name": self.name,
            "kind": self.kind,
            "created_by": self.created_by,
            "setup": self.setup,
            "purpose": self.purpose,
            "provider": self.provider,
            "where": self.where,
            "build_host": self.build_host,
            "hint": self.hint,
        }


def _operator(
    name: str,
    kind: str,
    purpose: str,
    *,
    provider: str | None = None,
    where: str | None = None,
) -> NeededSecret:
    from piceli.infra.machines.credentials import _SET_UP

    return NeededSecret(
        name, kind, "operator", _SET_UP[kind].format(name), purpose, provider, where
    )


def _of_infra(infra: Infrastructure) -> Iterable[NeededSecret]:
    assert infra.state_key is not None
    yield _operator(
        infra.state_key, "state-key", f"state of infrastructure {infra.name}",
        where="only in this store: back it up, the state cannot be read without it",
    )  # fmt: skip
    for provider in infra.providers():
        if provider.credentials is not None:
            yield _operator(
                provider.credentials, "provider-token", f"{provider.name} API",
                provider=provider.name,
            )  # fmt: skip
    backend = infra.backend
    credentials = getattr(backend, "credentials", None)
    if isinstance(credentials, str):
        yield _operator(credentials, "provider-token", "state backend")
    for server in infra.servers:
        if server.user_data is not None:
            yield _operator(
                server.user_data.secret, "user-data", f"user data of {server.name}",
                where=f"read once, when infra apply creates {server.name}: store "
                "a short-lived key right before that apply",
            )  # fmt: skip
    for bucket in infra.buckets:
        yield from _of_bucket(bucket)


def _of_bucket(bucket: Any) -> Iterable[NeededSecret]:
    for key in bucket.keys:
        yield NeededSecret(
            key.name, KIND, "infra-apply",
            "piceli infra apply (the infrastructure declaring the bucket)",
            f"{key.access} S3 keys of bucket {bucket.name}",
        )  # fmt: skip


def _by_hand(cache: Any) -> str:
    """How the owner makes an S3 pair of ``cache`` (the bucket's provider's
    command when it is known)."""
    provider = getattr(cache.bucket, "provider", None)
    command = getattr(provider, "s3_keys_by_hand", None)
    if command:
        return f"made by hand, signed in as the project's user: {command}"
    return "made by hand at the object storage (its console or API)"


def _owner(
    cache: Any,
    name: str,
    purpose: str,
    where: str | None,
    builder: str | None = None,
) -> NeededSecret:
    """An S3 pair of ``cache`` that the owner makes and stores (0.25.3)."""
    from piceli.infra.machines.credentials import _SET_UP

    return NeededSecret(
        name, KIND, "owner", _SET_UP[KIND].format(name), purpose, where=where,
        build_host=builder, hint=_by_hand(cache),
    )  # fmt: skip


def _of_cache(
    cache: Any, builder: str | None = None, host: Any = None
) -> Iterable[NeededSecret]:
    there = (
        f"also in the Piceli secrets of build host {builder}: piceli builder "
        f"serve --host {builder} --publish signs and pushes there; move it "
        "with piceli secrets backup --only and restore"
        if builder is not None
        else None
    )
    yield replace(
        _operator(
            cache.signing_credential, "nix-signing-key", f"signs {cache.store_url}",
            where=there,
        ),
        build_host=builder,
    )  # fmt: skip
    if cache.bucket is not None:
        for secret in _of_bucket(cache.bucket):
            if there is not None and secret.name == cache.write_credentials:
                secret = replace(secret, where=there, build_host=builder)
            yield secret
    if cache.write_credentials and not cache.creates(cache.write_credentials):
        yield _owner(
            cache, cache.write_credentials, f"write S3 keys of {cache.store_url}",
            there, builder,
        )  # fmt: skip
    read = cache.read_credentials
    if read and not cache.creates(read):
        # A cache alone names no host: the host that uses it says where.
        where: str | None = None
        if host is not None and host.cache_credentials is not None:
            where = (
                f"on host {host.name} too: piceli host secrets {host.name} "
                f"writes it to {host.read_credentials_file}"
            )
        elif host is not None:
            where = (
                f"on host {host.name} too, in its own file {host.read_credentials_file}"
            )
        yield _owner(cache, read, f"read S3 keys of {cache.store_url}", where)


def _of_host(host: Any, builder: str | None = None) -> Iterable[NeededSecret]:
    yield from _of_cache(host.cache, builder, host)
    read = host.cache.read_credentials
    if read is not None and host.cache.creates(read):
        yield NeededSecret(
            read, KIND, "infra-apply",
            "piceli infra apply (the infrastructure declaring the bucket)",
            f"read S3 keys of bucket {host.cache.bucket_name}",
            where=f"on host {host.name} too: piceli host secrets {host.name} "
            "writes it to /var/lib/piceli-host/cache.env",
        )  # fmt: skip
    if host.age_identity is not None:
        yield _operator(
            host.age_identity, "age-identity", f"sops-nix on {host.name}",
            where=f"on host {host.name} too: piceli host secrets {host.name} puts "
            "it there; its age1 public key goes in .sops.yaml",
        )  # fmt: skip


#: The composition's Git Secret (``piceli secrets git``), in the cluster.
CLUSTER_GIT = "piceli-build-git"


def _of_inputs(release: Any) -> Iterable[NeededSecret]:
    """0.26.0: the Git credentials of a NixRelease's private flake inputs:
    with ``credentials="host"`` each name ``inputs=`` declares (a
    ``git-token`` or a ``git-ssh-key``), made by the owner and needed in the
    build host's own store; with ``credentials="controller"`` the
    composition's Git Secret in the cluster."""
    from piceli.infra.machines.credentials import _SET_UP

    builder = release.builder
    if release.credentials == "controller":
        yield NeededSecret(
            CLUSTER_GIT, "cluster-git-secret", "owner",
            "piceli secrets git --cluster MODULE:ATTR --prompt",
            f"Git token NixRelease {release.name} fetches its private flake "
            "inputs with (credentials=\"controller\": sealed per build)",
            where="in the cluster, not this store: piceli cluster status shows it",
        )  # fmt: skip
        return
    names: dict[str, list[str]] = {}
    for name, credential in release.inputs:
        names.setdefault(credential, []).append(name)
    for credential, inputs in names.items():
        yield NeededSecret(
            credential, "git-token", "owner",
            _SET_UP["git-token"].format(credential),
            f"Git credential (git-token or git-ssh-key) of flake input "
            f"{', '.join(inputs)} of NixRelease {release.name}",
            where=f"in the Piceli secrets of build host {builder}: it fetches the "
            "input before each build; move it with piceli secrets backup --only "
            "and restore",
            build_host=builder,
        )  # fmt: skip


def needed_secrets(*declarations: Any) -> list[NeededSecret]:
    """Every secret the declarations need, once per name (first purpose
    wins; an ``infra-apply`` one over an ``operator`` one; a ``where`` that
    a later one names is kept).

    Takes :class:`~piceli.infra.Infrastructure`, ``Bucket``, ``NixCache``,
    ``NixHost``, ``NixRelease`` (its host's secrets; the cache's signing and
    write keys also on its build host), ``DevBuilds`` (none of its own),
    ``SigningKey`` (0.32.0: a release signing key), or lists of them. Nothing is read: this is the declaration.
    """
    from piceli.artifacts.file_signing import SigningKey
    from piceli.dev.model import DevBuilds
    from piceli.dev.nix import NixCache
    from piceli.hosts.model import NixHost
    from piceli.infra.machines.model import Bucket
    from piceli.infra.nix_releases import NixRelease

    found: dict[str, NeededSecret] = {}

    def walk(item: Any) -> Iterable[NeededSecret]:
        if isinstance(item, Infrastructure):
            yield from _of_infra(item)
        elif isinstance(item, Bucket):
            yield from _of_bucket(item)
        elif isinstance(item, NixCache):
            yield from _of_cache(item)
        elif isinstance(item, NixHost):
            yield from _of_host(item)
        elif isinstance(item, NixRelease):
            yield from _of_host(item.host, item.builder)
            yield from _of_inputs(item)
        elif isinstance(item, DevBuilds):
            return
        elif isinstance(item, SigningKey):
            yield _operator(
                item.name, "signing-key", item.purpose,
                where="only in this store (or an SSH agent): back it up; "
                "verifiers trust its public key in allowed_signers",
            )  # fmt: skip
        elif isinstance(item, (list, tuple, set, frozenset)):
            for each in item:
                yield from walk(each)
        else:
            raise TypeError(f"needed_secrets() takes declarations, not {item!r}")

    for secret in walk(declarations):
        seen = found.get(secret.name)
        if seen is None:
            found[secret.name] = secret
        elif seen.created_by == "operator" and secret.created_by == "infra-apply":
            found[secret.name] = replace(
                secret,
                where=secret.where or seen.where,
                build_host=secret.build_host or seen.build_host,
            )
        elif seen.where is None and secret.where is not None:
            found[secret.name] = replace(
                seen, where=secret.where, build_host=secret.build_host
            )
    return list(found.values())
