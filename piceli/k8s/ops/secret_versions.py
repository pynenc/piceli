"""Private, immutable secret versions. References are random, never value digests."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from piceli.k8s.ops.bounds import positive, strict_json
from piceli.k8s.ops.discovery import PlanTarget

PRIVATE_VALUE = "<private>"


def private_directory(path: Path) -> None:
    """Refuse symlinks and permissive existing storage; do not chmod user files."""
    if path.is_symlink():
        raise ValueError("private storage cannot be a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    mode = path.stat()
    if mode.st_uid != os.getuid() or stat.S_IMODE(mode.st_mode) & 0o077:
        raise ValueError("private storage requires owner-only directory permissions")


def private_database(path: Path) -> sqlite3.Connection:
    private_directory(path.parent)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        mode = os.fstat(descriptor)
        if (
            not stat.S_ISREG(mode.st_mode)
            or mode.st_uid != os.getuid()
            or stat.S_IMODE(mode.st_mode) & 0o077
        ):
            raise ValueError("private database requires owner-only regular file")
    finally:
        os.close(descriptor)
    connection = sqlite3.connect(path, timeout=1)
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


@dataclass(frozen=True)
class SecretVersionRef:
    """Opaque store/version identities; values and value hashes are not public."""

    store_id: str
    version: str

    def __post_init__(self) -> None:
        for value in (self.store_id, self.version):
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
                raise ValueError("invalid private version reference")


@dataclass(frozen=True)
class SecretBinding:
    json_pointer: str
    reference: SecretVersionRef

    def __post_init__(self) -> None:
        pointer_parts(self.json_pointer)
        if not isinstance(self.reference, SecretVersionRef):
            raise ValueError("invalid private reference")


def pointer_parts(pointer: str) -> list[str]:
    if (
        not isinstance(pointer, str)
        or not pointer.startswith("/")
        or re.search(r"~(?![01])", pointer)
    ):
        raise ValueError("invalid private JSON pointer")
    parts = [
        part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")
    ]
    if (
        parts[0] in {"apiVersion", "kind", "status"}
        or parts[:2]
        in [
            ["metadata", "name"],
            ["metadata", "namespace"],
            ["metadata", "uid"],
            ["metadata", "resourceVersion"],
        ]
        or parts == ["metadata"]
    ):
        raise ValueError("private binding cannot replace resource identity")
    return parts


def replace_pointer(manifest: dict[str, Any], pointer: str, value: Any) -> None:
    parts = pointer_parts(pointer)
    current: Any = manifest
    try:
        for part in parts[:-1]:
            current = current[int(part)] if isinstance(current, list) else current[part]
        key: Any = int(parts[-1]) if isinstance(current, list) else parts[-1]
        current[key]  # A binding must refer to an explicitly present value.
        current[key] = value
    except (KeyError, IndexError, TypeError, ValueError):
        raise ValueError("private pointer does not identify a manifest value") from None


class SecretVersionStore:
    """Local plaintext secret store, protected by 0700 directory and 0600 file.

    The caller owns encryption-at-rest and backup policy. No mutable latest alias
    exists: rotation always creates a new random version, scoped to one target.
    """

    def __init__(self, path: Path, *, max_bytes: int = 64_000_000) -> None:
        self.path = path
        self.max_bytes = positive(max_bytes, "private storage bytes", 1_000_000_000)
        self.connection = private_database(path)
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS store (id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS versions (
                version TEXT PRIMARY KEY, cluster_id TEXT NOT NULL,
                namespace TEXT NOT NULL, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS session_bindings (
                session_id TEXT NOT NULL, input_name TEXT NOT NULL,
                version TEXT NOT NULL REFERENCES versions(version),
                PRIMARY KEY(session_id, input_name));
        """
        )
        with self.connection:
            if self.connection.execute("SELECT id FROM store").fetchone() is None:
                self.connection.execute(
                    "INSERT INTO store VALUES (?)", (uuid.uuid4().hex,)
                )
        self.store_id = self.connection.execute("SELECT id FROM store").fetchone()[0]

    def put(self, target: PlanTarget, value: Any) -> SecretVersionRef:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        strict_json(encoded, 1_000_000)
        if self.path.stat().st_size + len(encoded.encode()) + 32768 > self.max_bytes:
            raise ValueError("private store byte budget exhausted")
        reference = SecretVersionRef(self.store_id, uuid.uuid4().hex)
        with self.connection:
            self.connection.execute(
                "INSERT INTO versions VALUES (?, ?, ?, ?)",
                (reference.version, target.cluster_id, target.namespace, encoded),
            )
        return reference

    def put_once(
        self, target: PlanTarget, session_id: str, input_name: str, value: Any
    ) -> SecretVersionRef:
        """Materialize one caller input once, without storing a value fingerprint.

        A retry with the same session/input pair returns the original opaque
        reference.  It deliberately does not compare ``value``: doing so would
        require retaining a secret-derived hash and would make private material
        observable from otherwise public session state.
        """
        if not re.fullmatch(r"[0-9a-f]{32}", session_id):
            raise ValueError("invalid deployment session identity")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", input_name):
            raise ValueError("invalid private input name")
        row = self.connection.execute(
            "SELECT version FROM session_bindings WHERE session_id=? AND input_name=?",
            (session_id, input_name),
        ).fetchone()
        if row is not None:
            return SecretVersionRef(self.store_id, row[0])
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        strict_json(encoded, 1_000_000)
        if self.path.stat().st_size + len(encoded.encode()) + 32768 > self.max_bytes:
            raise ValueError("private store byte budget exhausted")
        reference = SecretVersionRef(self.store_id, uuid.uuid4().hex)
        with self.connection:
            current = self.connection.execute(
                "SELECT version FROM session_bindings WHERE session_id=? AND input_name=?",
                (session_id, input_name),
            ).fetchone()
            if current is not None:
                return SecretVersionRef(self.store_id, current[0])
            self.connection.execute(
                "INSERT INTO versions VALUES (?, ?, ?, ?)",
                (reference.version, target.cluster_id, target.namespace, encoded),
            )
            self.connection.execute(
                "INSERT INTO session_bindings VALUES (?, ?, ?)",
                (session_id, input_name, reference.version),
            )
        return reference

    def session_reference(
        self, target: PlanTarget, session_id: str, input_name: str
    ) -> SecretVersionRef | None:
        """Return an existing session binding without resolving its value."""
        row = self.connection.execute(
            "SELECT version FROM session_bindings WHERE session_id=? AND input_name=?",
            (session_id, input_name),
        ).fetchone()
        if row is None:
            return None
        reference = SecretVersionRef(self.store_id, row[0])
        return reference if self.contains(target, reference) else None

    def resolve(self, target: PlanTarget, reference: SecretVersionRef) -> Any:
        if reference.store_id != self.store_id:
            raise ValueError("private version belongs to another store")
        row = self.connection.execute(
            "SELECT value FROM versions WHERE version=? AND cluster_id=? AND namespace=?",
            (reference.version, target.cluster_id, target.namespace),
        ).fetchone()
        if row is None:
            raise ValueError("private version missing or outside target")
        return strict_json(row[0], 1_000_000)

    def contains(self, target: PlanTarget, reference: SecretVersionRef) -> bool:
        """Check an opaque version binding without loading its private value."""
        if reference.store_id != self.store_id:
            return False
        return (
            self.connection.execute(
                "SELECT 1 FROM versions WHERE version=? AND cluster_id=? AND namespace=?",
                (reference.version, target.cluster_id, target.namespace),
            ).fetchone()
            is not None
        )

    def close(self) -> None:
        self.connection.close()
