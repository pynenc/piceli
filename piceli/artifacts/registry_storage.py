"""Read-only looks inside the in-cluster registry's storage, over ``pods/exec``.

The distribution registry has no API that lists untagged manifests, and the
kubelet's volume statistics of a claim on a node-path volume (k3s
``local-path``) are the node's filesystem, not the claim. Both are read from
inside the running registry pod, with fixed, read-only commands:

- :data:`LIST_MANIFESTS`: ``find`` the ``link`` file of every manifest
  revision (tagged or not) under the registry's storage;
- :data:`DISK_USAGE`: ``du -sk`` of the registry's storage directory.

No shell runs (the command is passed as arguments) and nothing is written.
The caller needs ``list`` on ``pods`` and ``get``/``create`` on
``pods/exec`` in the registry's namespace.

Importing this module is side-effect free.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

#: The registry's storage (``cluster_registry.STORAGE_DIR``; not imported, so
#: this module stays light for the ``artifacts`` CLI).
STORAGE_DIR = "/var/lib/registry"
#: Where the filesystem driver keeps repositories.
REPOSITORIES = f"{STORAGE_DIR}/docker/registry/v2/repositories"
#: Every manifest revision link, tagged or not (read-only).
LIST_MANIFESTS: tuple[str, ...] = (
    "find",
    REPOSITORIES,
    "-path",
    "*/_manifests/revisions/sha256/*/link",
    "-type",
    "f",
)
#: The registry's storage use in KiB (read-only).
DISK_USAGE: tuple[str, ...] = ("du", "-sk", STORAGE_DIR)
#: Most output an exec may return (a registry of 20 000 manifests lists ~3 MB).
OUTPUT_LIMIT = 16 * 1024 * 1024
_LINK = re.compile(
    r"(?P<repo>[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
    r"(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*)"
    r"/_manifests/revisions/sha256/(?P<hex>[0-9a-f]{64})/link"
)


class StorageReadError(RuntimeError):
    """The registry's storage could not be read (a fixed ``code``, no detail)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def parse_manifest_links(text: str) -> set[tuple[str, str]]:
    """``(repository, digest)`` of every revision link ``find`` printed.

    Lines that are not a revision link under :data:`REPOSITORIES` (or name an
    invalid repository) are ignored.
    """
    found: set[tuple[str, str]] = set()
    prefix = REPOSITORIES.rstrip("/") + "/"
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith(prefix):
            continue
        match = _LINK.fullmatch(line[len(prefix) :])
        if match:
            found.add((match.group("repo"), "sha256:" + match.group("hex")))
    return found


def parse_disk_usage(text: str) -> int | None:
    """Bytes from ``du -sk`` output (``<KiB>\\t<path>``), else ``None``."""
    first = (text.strip().splitlines() or [""])[0].split()
    if first and first[0].isdigit():
        return int(first[0]) * 1024
    return None


def registry_pod(pods: Sequence[Mapping[str, Any]], name: str) -> str | None:
    """The running, ready registry pod of the registry ``name``, if any."""
    for pod in pods:
        meta = pod.get("metadata") or {}
        labels = meta.get("labels") or {}
        if (
            labels.get("app.kubernetes.io/instance") == name
            and labels.get("app.kubernetes.io/component") == "registry"
            and (pod.get("status") or {}).get("phase") == "Running"
            and not meta.get("deletionTimestamp")
        ):
            return str(meta.get("name"))
    return None


def exec_in_pod(
    client: Any,
    namespace: str,
    pod: str,
    command: Sequence[str],
    *,
    container: str = "registry",
    timeout: float = 60.0,
) -> bytes:
    """Run ``command`` in ``pod`` over ``pods/exec``; its stdout (exit 0 only).

    stderr is never kept. :raises StorageReadError:
    ``registry-storage-unreadable`` when the exec is refused, fails or prints
    more than :data:`OUTPUT_LIMIT`.
    """
    from piceli.checks.context import open_exec_socket
    from piceli.restore.cluster import stream_exec

    chunks: list[bytes] = []
    size = 0

    def keep(chunk: bytes) -> None:
        nonlocal size
        size += len(chunk)
        if size <= OUTPUT_LIMIT:
            chunks.append(chunk)

    try:
        socket_ = open_exec_socket(
            client, namespace, pod, list(command), container, timeout
        )
        code = stream_exec(socket_, stdout=keep, timeout=timeout)
    except Exception:
        raise StorageReadError("registry-storage-unreadable") from None
    if code != 0 or size > OUTPUT_LIMIT:
        raise StorageReadError("registry-storage-unreadable")
    return b"".join(chunks)


def _pods(client: Any, namespace: str, name: str) -> list[dict[str, Any]]:
    from urllib.parse import quote

    selector = f"app.kubernetes.io/instance={name},app.kubernetes.io/component=registry"
    response = client.call_api(
        f"/api/v1/namespaces/{quote(namespace, safe='')}/pods",
        "GET",
        query_params=[("labelSelector", selector)],
        header_params={"Accept": "application/json"},
        auth_settings=["BearerToken"],
        _preload_content=False,
        _request_timeout=30,
    )
    body = response[0] if isinstance(response, tuple) else response
    return list(json.loads(body.data).get("items") or ())


def read_stored_manifests(
    client: Any, namespace: str, name: str, *, timeout: float = 120.0
) -> set[tuple[str, str]]:
    """Every manifest the registry ``name`` stores, tagged or not.

    :raises StorageReadError: ``registry-storage-unreadable`` (no running
        registry pod, exec refused or failed).
    """
    try:
        pod = registry_pod(_pods(client, namespace, name), name)
    except Exception:
        raise StorageReadError("registry-storage-unreadable") from None
    if pod is None:
        raise StorageReadError("registry-storage-unreadable")
    out = exec_in_pod(client, namespace, pod, LIST_MANIFESTS, timeout=timeout)
    return parse_manifest_links(out.decode("utf-8", "replace"))


def read_disk_usage(
    client: Any, namespace: str, pod: str, *, timeout: float = 60.0
) -> int | None:
    """The registry storage's bytes (``du``), ``None`` when it cannot be read."""
    try:
        out = exec_in_pod(client, namespace, pod, DISK_USAGE, timeout=timeout)
    except StorageReadError:
        return None
    return parse_disk_usage(out.decode("utf-8", "replace"))
