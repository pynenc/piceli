"""Where the OpenTofu state of an :class:`~piceli.infra.Infrastructure` lives.

The state directory (``Infrastructure(state_dir=)``, default
``$XDG_STATE_HOME/piceli/infra/<name>``, else
``~/.local/state/piceli/infra/<name>``) is OpenTofu's working directory:
the rendered configuration, the lock file, the provider plugins and, with
:class:`LocalState`, the state itself. It is ``0700``, refused inside a Git
work tree (``infra-state-in-git``), and every ``piceli infra`` command holds
its lock file (``fcntl.flock``) while it runs: one operator at a time
(``infra-state-locked``).

The state and plan files are encrypted by OpenTofu itself (``pbkdf2`` key
provider, ``aes_gcm``, ``enforced``): :func:`encryption_env` writes the
configuration into ``TF_ENCRYPTION`` of the child process only, with the
passphrase from the local credential ``Infrastructure(state_key=)``. An
unencrypted state is never written or read.

Besides the state, Piceli keeps small JSON records next to it (no secrets):
``inventory.json`` (what the last apply left: addresses, ids, labels; the
ownership ledger destroy checks), ``installs.json`` and
``registrations.json``.

Importing this module is side-effect free.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from piceli.infra.machines.model import InfraError

__all__ = [
    "HttpState",
    "LocalState",
    "StateBackend",
    "encryption_env",
    "locked",
    "read_record",
    "state_dir",
    "write_record",
]

LOCK_FILE = ".piceli-infra.lock"
_URL = re.compile(r"https?://[^\s@]+")


class StateBackend(ABC):
    """Where OpenTofu stores the (encrypted) state."""

    kind: str = ""

    @abstractmethod
    def render(self) -> dict[str, Any] | None:
        """The ``terraform.backend`` block (``None``: OpenTofu's local default)."""

    def env(self) -> dict[str, str]:
        """Environment variables the backend needs (credentials); never in argv."""
        return {}

    def secrets(self) -> list[str]:
        """Values to redact from OpenTofu's output."""
        return []

    def describe(self) -> dict[str, Any]:
        return {"kind": self.kind}


@dataclass(frozen=True)
class LocalState(StateBackend):
    """The state file in the state directory (the default)."""

    kind = "local"

    def render(self) -> dict[str, Any] | None:
        return None


@dataclass(frozen=True)
class HttpState(StateBackend):
    """OpenTofu's ``http`` backend: the encrypted state on a REST endpoint.

    The server only ever receives ciphertext (state encryption runs before
    the upload). ``credentials`` names a local credential (``piceli secrets
    provider NAME --prompt``) sent as the basic-auth password
    (``TF_HTTP_PASSWORD``), with ``username``.
    """

    address: str
    lock_address: str | None = None
    unlock_address: str | None = None
    username: str = "piceli"
    credentials: str | None = None

    kind = "http"

    def __post_init__(self) -> None:
        for name in ("address", "lock_address", "unlock_address"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, str) or not _URL.fullmatch(value)
            ):
                raise InfraError(
                    "infra-invalid",
                    f"HttpState({name}=) is an http(s) URL without credentials",
                )
        if self.credentials is not None:
            from piceli.profiles import ProfileError, check_name

            try:
                check_name(self.credentials)
            except ProfileError:
                raise InfraError(
                    "infra-invalid", "HttpState(credentials=) is a credential name"
                ) from None

    def render(self) -> dict[str, Any]:
        body: dict[str, Any] = {"address": self.address}
        if self.lock_address is not None:
            body["lock_address"] = self.lock_address
            body["unlock_address"] = self.unlock_address or self.lock_address
        return {"http": body}

    def env(self) -> dict[str, str]:
        if self.credentials is None:
            return {}
        from piceli.infra.machines.credentials import load_credential

        return {
            "TF_HTTP_USERNAME": self.username,
            "TF_HTTP_PASSWORD": load_credential(self.credentials, "provider-token"),
        }

    def secrets(self) -> list[str]:
        return [value for key, value in self.env().items() if key == "TF_HTTP_PASSWORD"]

    def describe(self) -> dict[str, Any]:
        return {"kind": self.kind, "address": self.address}


def default_state_root() -> Path:
    root = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(root) / "piceli" / "infra"


def _in_git(path: Path) -> bool:
    return any((parent / ".git").exists() for parent in [path, *path.parents])


def state_dir(name: str, declared: Path | None, *, create: bool = True) -> Path:
    """The state directory of infrastructure ``name`` (created ``0700``)."""
    path = (
        (declared if declared is not None else default_state_root() / name)
        .expanduser()
        .absolute()
    )
    if _in_git(path):
        raise InfraError(
            "infra-state-in-git",
            "the infrastructure state directory is inside a Git work tree; "
            "use Infrastructure(state_dir=) outside it (default ~/.local/state/piceli/infra)",
        )
    if create:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)
    return path


@contextmanager
def locked(directory: Path) -> Iterator[None]:
    """Hold the state directory's lock (refused when another command holds it)."""
    descriptor = os.open(directory / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    handle: IO[str] = os.fdopen(descriptor, "r+")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InfraError(
                "infra-state-locked",
                "another piceli infra command is using this state; wait for it to end",
            ) from None
        yield
    finally:
        handle.close()


def _hcl_string(value: str) -> str:
    """``value`` as an HCL quoted string (no interpolation)."""
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("${", "$${")
        .replace("%{", "%%{")
    )
    return f'"{escaped}"'


def encryption_env(passphrase: str) -> dict[str, str]:
    """``TF_ENCRYPTION``: state and plan files encrypted, unencrypted refused."""
    config = (
        'key_provider "pbkdf2" "piceli" {\n'
        f"  passphrase = {_hcl_string(passphrase)}\n"
        "}\n"
        'method "aes_gcm" "piceli" {\n'
        "  keys = key_provider.pbkdf2.piceli\n"
        "}\n"
        "state {\n  method = method.aes_gcm.piceli\n  enforced = true\n}\n"
        "plan {\n  method = method.aes_gcm.piceli\n  enforced = true\n}\n"
    )
    return {"TF_ENCRYPTION": config}


def read_record(directory: Path, name: str) -> dict[str, Any]:
    """A JSON record of the state directory (``{}`` when absent or damaged)."""
    try:
        value = json.loads((directory / name).read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def write_record(directory: Path, name: str, value: dict[str, Any]) -> None:
    """Write a JSON record atomically (``0600``)."""
    descriptor, staged = tempfile.mkstemp(dir=directory, prefix=".piceli-record-")
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(json.dumps(value, sort_keys=True, indent=2) + "\n")
        os.replace(staged, directory / name)
    finally:
        if os.path.exists(staged):
            os.unlink(staged)
