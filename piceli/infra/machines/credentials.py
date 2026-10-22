"""Local credentials of ``piceli infra``: provider API tokens and state passphrases.

``piceli secrets provider NAME --prompt`` and ``piceli secrets state-key
NAME --prompt|--generate`` (and the other ``piceli secrets`` commands) store
one value per name in
``$PICELI_CREDENTIALS_DIR`` (default ``~/.config/piceli/credentials``), a
``0700`` directory, as ``NAME.json`` (``0600``). Values are read only when a
command needs them and go to OpenTofu through its environment, never as an
argument; they are never printed, logged or put in a plan. A file another
user can read is refused (``infra-credential-unsafe``). ``piceli secrets
delete NAME`` removes one (:func:`delete_credential`), without reading its
value out.

Importing this module is side-effect free.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
import tempfile
from pathlib import Path

from piceli.infra.machines.model import InfraError

__all__ = [
    "KINDS",
    "credentials_dir",
    "delete_credential",
    "generate_passphrase",
    "load_credential",
    "save_credential",
]

CREDENTIALS_ENV = "PICELI_CREDENTIALS_DIR"
_SCHEMA = "piceli.credential.v1"
#: ``provider-token``: an API token; ``state-key``: a state passphrase;
#: 0.24: ``nix-signing-key``: a Nix binary cache's secret key
#: (``piceli secrets nix-cache``); ``s3-keys``: ``ACCESS_KEY_ID:SECRET``
#: of a bucket's S3 API (``piceli secrets s3``); ``age-identity``: a host's
#: age X25519 identity for sops-nix (``piceli secrets age``); ``user-data``:
#: a server's user data at creation (``piceli secrets user-data``).
KINDS = (
    "provider-token",
    "state-key",
    "nix-signing-key",
    "s3-keys",
    "age-identity",
    "user-data",
)
_SET_UP = {
    "provider-token": "piceli secrets provider {} --prompt",
    "state-key": "piceli secrets state-key {} --generate",
    "nix-signing-key": "piceli secrets nix-cache {} --generate",
    "s3-keys": "piceli secrets s3 {} --prompt",
    "age-identity": "piceli secrets age {} --generate",
    "user-data": "piceli secrets user-data {} --file PATH",
}
#: ``piceli secrets delete --kind``: the ``piceli secrets`` command's name of
#: each kind (a stored kind's own name is accepted too).
KIND_OPTIONS = {
    "provider": "provider-token",
    "state-key": "state-key",
    "nix-cache": "nix-signing-key",
    "s3": "s3-keys",
    "age": "age-identity",
    "user-data": "user-data",
}
#: Nova takes at most 65535 bytes of base64: 49149 bytes of user data.
MAX_USER_DATA = 49149
#: OpenTofu's pbkdf2 key provider needs at least 16 characters.
MIN_PASSPHRASE = 16


def credentials_dir() -> Path:
    """``$PICELI_CREDENTIALS_DIR`` or ``~/.config/piceli/credentials`` (read on each call)."""
    value = os.environ.get(CREDENTIALS_ENV)
    if value:
        return Path(value).expanduser().absolute()
    return Path.home() / ".config" / "piceli" / "credentials"


def _path(name: str) -> Path:
    from piceli.profiles import ProfileError, check_name

    try:
        check_name(name)
    except ProfileError:
        raise InfraError(
            "infra-credential-invalid", "a credential name is like a profile name"
        ) from None
    return credentials_dir() / f"{name}.json"


def check_value(kind: str, value: str) -> None:
    """Refuse an empty value, control characters, or a short passphrase."""
    if kind not in KINDS:
        raise InfraError(
            "infra-credential-invalid", f"unknown credential kind {kind!r}"
        )
    if kind == "user-data":
        # A whole cloud-config or script: lines and tabs are fine.
        if (
            not value.strip()
            or len(value.encode()) > MAX_USER_DATA
            or any((ord(c) < 32 and c not in "\n\t\r") or ord(c) == 127 for c in value)
        ):
            raise InfraError(
                "infra-credential-invalid",
                f"user data is text of 1..{MAX_USER_DATA} bytes without control characters",
            )
        return
    if (
        not value
        or len(value) > 4096
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise InfraError(
            "infra-credential-invalid", "the value is empty or has control characters"
        )
    if kind == "state-key" and len(value) < MIN_PASSPHRASE:
        raise InfraError(
            "infra-credential-invalid",
            f"a state passphrase has at least {MIN_PASSPHRASE} characters",
        )


def generate_passphrase() -> str:
    """A random passphrase (256 bits, URL-safe text)."""
    return secrets.token_urlsafe(32)


def save_credential(name: str, kind: str, value: str) -> Path:
    """Store ``value`` under ``name`` (atomic, ``0600`` in a ``0700`` directory)."""
    check_value(kind, value)
    path = _path(name)
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    body = json.dumps({"schema": _SCHEMA, "kind": kind, "value": value}, sort_keys=True)
    descriptor, staged = tempfile.mkstemp(dir=directory, prefix=".piceli-credential-")
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(body + "\n")
        os.replace(staged, path)
    finally:
        if os.path.exists(staged):
            os.unlink(staged)
    return path


def credential_present(name: str) -> bool:
    return _path(name).is_file()


def load_credential(name: str, kind: str) -> str:
    """The value stored under ``name`` (a registered refusal when missing or unsafe)."""
    path = _path(name)
    try:
        info = path.stat()
    except FileNotFoundError:
        command = _SET_UP.get(kind, _SET_UP["provider-token"]).format(name)
        raise InfraError(
            "infra-credential-missing", f"no credential named {name!r}; run `{command}`"
        ) from None
    if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise InfraError(
            "infra-credential-unsafe",
            f"credential {name!r} is readable by other users; chmod 600 it",
        )
    try:
        body = json.loads(path.read_text())
    except (OSError, ValueError):
        raise InfraError(
            "infra-credential-invalid", f"credential {name!r} is unreadable"
        ) from None
    if (
        not isinstance(body, dict)
        or body.get("kind") != kind
        or not isinstance(body.get("value"), str)
    ):
        raise InfraError(
            "infra-credential-invalid", f"credential {name!r} is not a {kind}"
        )
    return str(body["value"])


def kind_of(option: str) -> str:
    """The stored kind that ``--kind OPTION`` names (``infra-credential-invalid``)."""
    kind = KIND_OPTIONS.get(option, option)
    if kind not in KINDS:
        raise InfraError(
            "infra-credential-invalid",
            "--kind is one of " + ", ".join(KIND_OPTIONS),
        )
    return kind


def stored_kind(name: str) -> tuple[Path, str | None]:
    """The file of ``name`` and the kind it holds (``None``: damaged); the
    value is not returned. ``infra-credential-missing`` when there is none."""
    path = _path(name)
    if not path.is_file():
        raise InfraError(
            "infra-credential-missing", f"no credential named {name!r} to delete"
        )
    try:
        body = json.loads(path.read_text())
    except (OSError, ValueError, UnicodeDecodeError):
        body = None
    kind = body.get("kind") if isinstance(body, dict) else None
    return path, kind if kind in KINDS else None


def delete_credential(name: str, kind: str | None = None) -> tuple[Path, str | None]:
    """Remove the credential ``name`` (only if it holds ``kind``, when given):
    its path and the kind it held. The value is never read out."""
    path, found = stored_kind(name)
    require_kind(name, found, kind)
    path.unlink()
    return path, found


def require_kind(name: str, found: str | None, kind: str | None) -> None:
    """Refuse (``infra-credential-invalid``) when ``kind`` is given and the
    credential holds another one."""
    if kind is not None and found != kind:
        raise InfraError(
            "infra-credential-invalid",
            f"credential {name!r} is {'a ' + found if found else 'damaged'}, "
            f"not a {kind}; nothing was deleted",
        )
