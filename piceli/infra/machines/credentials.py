"""Local credentials of ``piceli infra``: provider API tokens and state passphrases.

``piceli secrets provider NAME --prompt`` and ``piceli secrets state-key
NAME --prompt|--generate`` store one value per name in
``$PICELI_CREDENTIALS_DIR`` (default ``~/.config/piceli/credentials``), a
``0700`` directory, as ``NAME.json`` (``0600``). Values are read only when a
command needs them and go to OpenTofu through its environment, never as an
argument; they are never printed, logged or put in a plan. A file another
user can read is refused (``infra-credential-unsafe``).

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
    "generate_passphrase",
    "load_credential",
    "save_credential",
]

CREDENTIALS_ENV = "PICELI_CREDENTIALS_DIR"
_SCHEMA = "piceli.credential.v1"
#: ``provider-token``: an API token; ``state-key``: a state passphrase.
KINDS = ("provider-token", "state-key")
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
        command = (
            f"piceli secrets provider {name} --prompt"
            if kind == "provider-token"
            else f"piceli secrets state-key {name} --generate"
        )
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
