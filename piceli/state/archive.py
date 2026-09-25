"""Export a deployment state to one file and import it back (``piceli state``).

An export is one JSON document (``piceli.state-export.v1``) with the state's
snapshot in two parts:

* **public** members (run journals, receipts, the release catalog, release
  history and sidecars, check reports) as a base64 archive;
* **private** members (the secret store, stored discovery with live Secret
  bodies, execution journals, pending plans, backups of replaced objects):
  left out by default, or encrypted with AES-256-GCM under a key derived
  (scrypt) from ``--key-file`` when the owner asks for them. The ciphertext
  is bound to the export's release, namespace and public digest.

An import without the private part would make the next release generate new
secret values, so it is refused unless explicitly allowed.

Importing this module is side-effect free.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
from pathlib import Path, PurePosixPath
from typing import Any

from piceli.state.backend import StateScope
from piceli.state.errors import StateError

EXPORT_SCHEMA = "piceli.state-export.v1"
MAX_EXPORT_BYTES = 256 * 1024 * 1024
MIN_KEY_CHARACTERS = 32
_SCRYPT = {"n": 2**15, "r": 8, "p": 1}


def read_key(path: Path) -> bytes:
    """The key in ``path``: owner-only file, at least 32 characters."""
    try:
        mode = path.stat().st_mode
        text = path.read_bytes().strip()
    except OSError:
        raise StateError("state-key-required", "the key file cannot be read") from None
    if stat.S_IMODE(mode) & 0o077:
        raise StateError(
            "state-key-required",
            "the key file is readable by others; make it owner-only (chmod 600)",
        )
    if len(text) < MIN_KEY_CHARACTERS:
        raise StateError(
            "state-key-required",
            f"the key must have at least {MIN_KEY_CHARACTERS} characters",
        )
    return text


def _aead(key: bytes, salt: bytes) -> Any:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise StateError(
            "state-crypto-unavailable",
            "encrypting secret material needs the cryptography package",
        ) from None
    derived = hashlib.scrypt(
        key,
        salt=salt,
        n=_SCRYPT["n"],
        r=_SCRYPT["r"],
        p=_SCRYPT["p"],
        maxmem=64 * 1024 * 1024,
        dklen=32,
    )
    return AESGCM(derived)


def _bound(header: dict[str, Any]) -> bytes:
    """What the ciphertext is bound to (associated data)."""
    return json.dumps(
        {
            key: header[key]
            for key in ("schema", "name", "namespace", "layout", "public_sha256")
        },
        sort_keys=True,
    ).encode()


def export_document(
    scope: StateScope,
    snapshot: bytes,
    *,
    key: bytes | None,
    source: str,
) -> dict[str, Any]:
    """The export of ``snapshot`` (private members encrypted with ``key`` or left out)."""
    import importlib.metadata
    import io
    import tarfile

    from piceli.pipeline.journal import now
    from piceli.state import snapshot as snapshots

    public: list[tuple[PurePosixPath, bytes]] = []
    hidden: list[tuple[PurePosixPath, bytes]] = []
    for name, content in snapshots.entries(snapshot):
        (hidden if snapshots.private(name) else public).append((name, content))

    def archive(items: list[tuple[PurePosixPath, bytes]]) -> bytes:
        import gzip

        buffer = io.BytesIO()
        with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as compressed:
            with tarfile.open(
                fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT
            ) as tar:
                for name, content in items:
                    info = tarfile.TarInfo(str(name))
                    info.size, info.mode, info.mtime = len(content), 0o600, 0
                    tar.addfile(info, io.BytesIO(content))
        return buffer.getvalue()

    public_bytes = archive(public)
    try:
        version = importlib.metadata.version("piceli")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover
        version = "unknown"
    document: dict[str, Any] = {
        "schema": EXPORT_SCHEMA,
        "piceli_version": version,
        "created_at": now(),
        "name": scope.name,
        "namespace": scope.namespace,
        "layout": scope.layout,
        "source": source,
        "files": [str(name) for name, _ in public],
        "public": base64.b64encode(public_bytes).decode(),
        "public_sha256": hashlib.sha256(public_bytes).hexdigest(),
    }
    names = [str(name) for name, _ in hidden]
    if key is None:
        document["private"] = {"state": "excluded", "files": names}
        return document
    salt, nonce = os.urandom(16), os.urandom(12)
    ciphertext = _aead(key, salt).encrypt(nonce, archive(hidden), _bound(document))
    document["private"] = {
        "state": "encrypted",
        "cipher": "AES-256-GCM",
        "kdf": {"name": "scrypt", **_SCRYPT, "salt": base64.b64encode(salt).decode()},
        "nonce": base64.b64encode(nonce).decode(),
        "ciphertext": base64.b64encode(ciphertext).decode(),
        "files": names,
    }
    return document


def load_export(path: Path) -> tuple[dict[str, Any], str]:
    """``(document, file sha256)`` of an export file (validated, not decrypted)."""
    try:
        raw = path.read_bytes()
    except OSError:
        raise StateError("state-export-invalid", "the export cannot be read") from None
    if len(raw) > MAX_EXPORT_BYTES:
        raise StateError("state-export-invalid", "the export is larger than the limit")
    try:
        document = json.loads(raw)
    except ValueError:
        raise StateError("state-export-invalid", "the export is not JSON") from None
    if not isinstance(document, dict) or document.get("schema") != EXPORT_SCHEMA:
        raise StateError("state-export-invalid", f"not a {EXPORT_SCHEMA} document")
    try:
        public = base64.b64decode(document["public"], validate=True)
        private = document["private"]
        assert isinstance(private, dict) and private.get("state") in {
            "excluded",
            "encrypted",
        }
    except (KeyError, ValueError, TypeError, AssertionError):
        raise StateError("state-export-invalid", "the export is malformed") from None
    if hashlib.sha256(public).hexdigest() != document.get("public_sha256"):
        raise StateError("state-export-invalid", "the export's digest does not match")
    return document, hashlib.sha256(raw).hexdigest()


def import_digest(scope: StateScope, file_sha256: str) -> str:
    """The digest ``piceli state import --approve`` needs: this file, this state."""
    return hashlib.sha256(
        json.dumps(
            {
                "schema": "piceli.state-import.v1",
                "file_sha256": file_sha256,
                "name": scope.name,
                "namespace": scope.namespace,
                "layout": scope.layout,
                "backend": scope.settings.backend,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()


def snapshot_of(
    scope: StateScope,
    document: dict[str, Any],
    *,
    key: bytes | None,
    allow_partial: bool,
) -> bytes:
    """The snapshot an export restores (refuses another release or a partial one)."""
    from piceli.state import snapshot as snapshots
    from piceli.tempfiles import temporary_directory

    for field in ("name", "namespace", "layout"):
        if document.get(field) != getattr(scope, field):
            raise StateError(
                "state-export-invalid",
                f"the export is for {field} {document.get(field)!r}, not "
                f"{getattr(scope, field)!r}",
            )
    private = document["private"]
    parts = [base64.b64decode(document["public"])]
    if private["state"] == "excluded":
        if private.get("files") and not allow_partial:
            raise StateError(
                "state-import-partial",
                f"the export leaves out {len(private['files'])} private file(s) "
                "(secret store, stored discovery, journals)",
            )
    else:
        if key is None:
            raise StateError(
                "state-key-required", "the export's secret material is encrypted"
            )
        try:
            salt = base64.b64decode(private["kdf"]["salt"])
            nonce = base64.b64decode(private["nonce"])
            ciphertext = base64.b64decode(private["ciphertext"])
        except (KeyError, ValueError, TypeError):
            raise StateError(
                "state-export-invalid", "the export's private part is malformed"
            ) from None
        try:
            parts.append(_aead(key, salt).decrypt(nonce, ciphertext, _bound(document)))
        except StateError:
            raise
        except Exception:
            raise StateError(
                "state-key-required", "the key does not decrypt this export"
            ) from None
    with temporary_directory("state") as folder:
        directory = Path(folder) / "state"
        for part in parts:
            snapshots.unpack(part, directory, replace=False)
        data, _names = snapshots.pack(directory)
    return data
