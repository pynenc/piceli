"""State storage, single-instance concurrency control, and backup for Piceli Operator.

Files remain the default state store with atomic/locked updates and safe backup/restore.
Local preference profiles and remote-authenticated users/standing policies are kept strictly distinct.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import socket
import tarfile
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

_NAME = re.compile(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?")
_TOKEN_CHARS = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


class ConcurrentWriterError(RuntimeError):
    """Raised when another Piceli instance already holds the exclusive state lock."""


@dataclass(frozen=True)
class StandingPolicy:
    """Explicit authorization scope for autonomous or assisted operations.

    Standing policies authorize ONLY their explicit scope. Scope expansion requires a new grant.
    """

    name: str
    allowed_namespaces: tuple[str, ...] = ()
    allowed_branches: tuple[str, ...] = ()
    allowed_registries: tuple[str, ...] = ()
    allowed_operations: tuple[str, ...] = ("preview", "promote", "rollback")
    require_pr_approval: bool = True

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.name):
            raise ValueError(f"invalid policy name: {self.name}")

    def authorize(
        self,
        action: str,
        *,
        namespace: str | None = None,
        branch: str | None = None,
        registry: str | None = None,
        is_pr: bool = False,
        approved: bool = False,
    ) -> bool:
        """Check whether an operation is permitted under this standing policy."""
        if action not in self.allowed_operations:
            return False
        if namespace is not None and self.allowed_namespaces:
            if not any(
                allowed == "*"
                or allowed == namespace
                or (allowed.endswith("*") and namespace.startswith(allowed[:-1]))
                for allowed in self.allowed_namespaces
            ):
                return False
        if branch is not None and self.allowed_branches:
            if not any(
                allowed == "*"
                or allowed == branch
                or (allowed.endswith("*") and branch.startswith(allowed[:-1]))
                for allowed in self.allowed_branches
            ):
                return False
        if registry is not None and self.allowed_registries:
            if not any(
                allowed == "*"
                or allowed == registry
                or (allowed.endswith("*") and registry.startswith(allowed[:-1]))
                for allowed in self.allowed_registries
            ):
                return False
        if is_pr and self.require_pr_approval and not approved:
            if action in {"apply", "deploy", "merge", "promote"}:
                return False
        return True


@dataclass(frozen=True)
class OperatorUser:
    """Authenticated operator identity; distinct from local loopback preference profiles."""

    username: str
    role: str  # admin, operator, viewer
    token_hash: str
    salt: str
    created_at: float = field(default_factory=time.time)
    policies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.username):
            raise ValueError(f"invalid username: {self.username}")
        if self.role not in {"admin", "operator", "viewer"}:
            raise ValueError(f"unrecognized operator role: {self.role}")

    def verify_token(self, token: str) -> bool:
        computed = hashlib.pbkdf2_hmac(
            "sha256", token.encode(), self.salt.encode(), 100_000
        ).hex()
        return secrets.compare_digest(computed, self.token_hash)


class StateStorageAdapter(Protocol):
    """Interface for state persistence and single-instance coordination."""

    def acquire_lock(self) -> None:
        """Acquire single-instance exclusive lock or raise ConcurrentWriterError."""

    def release_lock(self) -> None:
        """Release instance lock."""

    def load_data(self, key: str) -> dict[str, Any]:
        """Load structured state by key."""

    def save_data(self, key: str, data: Mapping[str, Any]) -> None:
        """Atomically persist structured state by key."""

    def create_backup(self, target_archive: Path) -> Path:
        """Create an atomic, mode-restricted backup archive."""

    def restore_backup(self, backup_archive: Path, destination: Path) -> None:
        """Verify and restore state backup into empty/owned destination."""


class InstanceLock:
    """Process-level and file-descriptor exclusive lock for single-instance operations."""

    def __init__(self, lock_file: Path) -> None:
        self.lock_file = lock_file
        self._fd: int | None = None

    def acquire(self) -> None:
        """Acquire exclusive non-blocking lock on the instance lock file."""
        self.lock_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self._fd = os.open(
                str(self.lock_file),
                os.O_CREAT | os.O_RDWR,
                0o600,
            )
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as error:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            raise ConcurrentWriterError(
                f"exclusive operator instance lock already held at {self.lock_file}"
            ) from error

        meta = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "acquired_at": time.time(),
        }
        os.ftruncate(self._fd, 0)
        os.lseek(self._fd, 0, os.SEEK_SET)
        payload = (json.dumps(meta) + chr(10)).encode()
        os.write(self._fd, payload)

    def release(self) -> None:
        """Release lock and close file descriptor."""
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
            try:
                if self.lock_file.exists():
                    self.lock_file.unlink()
            except OSError:
                pass

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.release()


class FileStateStore:
    """File-backed operator state store with atomic replacement, locking, and backup."""

    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = (base_dir or Path.home() / ".config" / "piceli").resolve()
        self.lock = InstanceLock(self.base_dir / ".instance.lock")

    def ensure_directories(self) -> None:
        self.base_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.base_dir, 0o700)

    def _file_path(self, key: str) -> Path:
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", key):
            raise ValueError(f"invalid state key: {key}")
        return self.base_dir / f"{key}.json"

    def load_data(self, key: str) -> dict[str, Any]:
        """Load JSON state file or return empty dict if missing."""
        path = self._file_path(key)
        if not path.exists():
            return {}
        content = path.read_text(encoding="utf-8")
        if not content.strip():
            return {}
        value = json.loads(content)
        if not isinstance(value, dict):
            raise ValueError(f"state data for {key} must be a JSON object")
        return value

    def save_data(self, key: str, data: Mapping[str, Any]) -> None:
        """Atomically save JSON state with owner-only (0o600) permissions."""
        self.ensure_directories()
        path = self._file_path(key)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{key}-", dir=self.base_dir)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(data, handle, sort_keys=True, separators=(",", ":"))
                handle.write(chr(10))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            dir_fd = os.open(str(self.base_dir), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            if os.path.exists(temporary):
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def create_backup(self, target_archive: Path) -> Path:
        """Create an atomic .tar.gz backup of all state files."""
        target_archive = target_archive.resolve()
        target_archive.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

        descriptor, temporary = tempfile.mkstemp(
            prefix=".backup-", suffix=".tar.gz", dir=target_archive.parent
        )
        os.close(descriptor)
        os.chmod(temporary, 0o600)

        try:
            with tarfile.open(temporary, "w:gz") as tar:
                if self.base_dir.exists():
                    for entry in sorted(self.base_dir.iterdir()):
                        if (
                            entry.is_file()
                            and entry.suffix == ".json"
                            and not entry.name.startswith(".")
                        ):
                            tar.add(entry, arcname=entry.name)
            os.replace(temporary, target_archive)
        finally:
            if os.path.exists(temporary):
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
        return target_archive

    def restore_backup(
        self, backup_archive: Path, destination: Path | None = None
    ) -> None:
        """Restore state from backup archive into empty or owned directory."""
        dest = (destination or self.base_dir).resolve()
        if dest.exists() and any(dest.iterdir()):
            non_state = [
                p
                for p in dest.iterdir()
                if p.suffix != ".json" and not p.name.startswith(".")
            ]
            if non_state:
                raise ValueError(
                    f"destination {dest} contains non-state files; aborting restore"
                )

        dest.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(dest, 0o700)

        with tarfile.open(backup_archive, "r:gz") as tar:
            for member in tar.getmembers():
                if (
                    not member.isfile()
                    or "/" in member.name
                    or ".." in member.name
                    or not member.name.endswith(".json")
                ):
                    raise ValueError(f"unsafe member in state archive: {member.name}")
                target_file = dest / member.name
                f = tar.extractfile(member)
                if f is not None:
                    data = f.read()
                    target_file.write_bytes(data)
                    os.chmod(target_file, 0o600)


class UserStore:
    """Persists authenticated operator users and credentials in the state store."""

    def __init__(self, store: FileStateStore) -> None:
        self.store = store

    def load_users(self) -> dict[str, OperatorUser]:
        raw = self.store.load_data("users")
        users: dict[str, OperatorUser] = {}
        for item in raw.get("users", []):
            user = OperatorUser(
                username=item["username"],
                role=item["role"],
                token_hash=item["token_hash"],
                salt=item["salt"],
                created_at=item.get("created_at", 0.0),
                policies=tuple(item.get("policies", ())),
            )
            users[user.username] = user
        return users

    def save_users(self, users: Mapping[str, OperatorUser]) -> None:
        data = {
            "schema_version": 1,
            "users": [
                {
                    "username": u.username,
                    "role": u.role,
                    "token_hash": u.token_hash,
                    "salt": u.salt,
                    "created_at": u.created_at,
                    "policies": list(u.policies),
                }
                for u in sorted(users.values(), key=lambda x: x.username)
            ],
        }
        self.store.save_data("users", data)

    def create_user(
        self, username: str, role: str, token: str, policies: tuple[str, ...] = ()
    ) -> tuple[OperatorUser, str]:
        """Create user and return OperatorUser and plaintext token."""
        salt = secrets.token_hex(16)
        token_hash = hashlib.pbkdf2_hmac(
            "sha256", token.encode(), salt.encode(), 100_000
        ).hex()
        user = OperatorUser(
            username=username,
            role=role,
            token_hash=token_hash,
            salt=salt,
            created_at=time.time(),
            policies=policies,
        )
        users = self.load_users()
        users[username] = user
        self.save_users(users)
        return user, token

    def authenticate(self, token: str) -> OperatorUser | None:
        """Authenticate a bearer or API token against stored users."""
        if not token:
            return None
        users = self.load_users()
        for user in users.values():
            if user.verify_token(token):
                return user
        return None


class PolicyStore:
    """Persists standing policies in the operator state store."""

    def __init__(self, store: FileStateStore) -> None:
        self.store = store

    def load_policies(self) -> dict[str, StandingPolicy]:
        raw = self.store.load_data("policies")
        policies: dict[str, StandingPolicy] = {}
        for item in raw.get("policies", []):
            policy = StandingPolicy(
                name=item["name"],
                allowed_namespaces=tuple(item.get("allowed_namespaces", ())),
                allowed_branches=tuple(item.get("allowed_branches", ())),
                allowed_registries=tuple(item.get("allowed_registries", ())),
                allowed_operations=tuple(item.get("allowed_operations", ())),
                require_pr_approval=item.get("require_pr_approval", True),
            )
            policies[policy.name] = policy
        return policies

    def save_policies(self, policies: Mapping[str, StandingPolicy]) -> None:
        data = {
            "schema_version": 1,
            "policies": [
                {
                    "name": p.name,
                    "allowed_namespaces": list(p.allowed_namespaces),
                    "allowed_branches": list(p.allowed_branches),
                    "allowed_registries": list(p.allowed_registries),
                    "allowed_operations": list(p.allowed_operations),
                    "require_pr_approval": p.require_pr_approval,
                }
                for p in sorted(policies.values(), key=lambda x: x.name)
            ],
        }
        self.store.save_data("policies", data)
