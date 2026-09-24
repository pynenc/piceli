"""Restorable backups written before a ``replace`` deletes a live object.

A backup is the live object as ``kubectl create -f <file>`` accepts it: the
server-populated fields that a create rejects or ignores (``status``,
``metadata.uid``, ``resourceVersion``, ``managedFields``,
``creationTimestamp``, ``generation``, ``selfLink``, deletion fields) are
removed; everything else, including labels, annotations and the
``kubectl.kubernetes.io/last-applied-configuration`` annotation, is kept.

Files are written atomically with owner-only permissions under a private
directory (``<state_dir>/backups/<execution>/``) and fsynced before the delete
is journaled. The journal records the path and the file's SHA-256, never the
content.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from piceli.k8s.ops.secret_versions import private_directory

_SERVER_METADATA = (
    "creationTimestamp",
    "deletionGracePeriodSeconds",
    "deletionTimestamp",
    "generation",
    "managedFields",
    "resourceVersion",
    "selfLink",
    "uid",
)
_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def restorable_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """The live object cleaned for ``kubectl create -f``."""
    value = json.loads(json.dumps(manifest))
    value.pop("status", None)
    metadata = value.get("metadata")
    if not isinstance(metadata, dict) or not metadata.get("name"):
        raise ValueError("backup requires object metadata")
    for key in _SERVER_METADATA:
        metadata.pop(key, None)
    return dict(value)


def write_backup(
    directory: Path, *, execution: str, ordinal: int, manifest: Mapping[str, Any]
) -> tuple[Path, str]:
    """Write one backup atomically (mode 0600); return its path and SHA-256."""
    content = restorable_manifest(manifest)
    encoded = (json.dumps(content, sort_keys=True, indent=2) + "\n").encode()
    private_directory(directory)
    folder = directory / _SAFE.sub("_", execution)
    private_directory(folder)
    name = f"{ordinal:04d}-{content['kind']}-{content['metadata']['name']}.json"
    path = folder / _SAFE.sub("_", name)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=folder)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    handle = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)
    return path, hashlib.sha256(encoded).hexdigest()
