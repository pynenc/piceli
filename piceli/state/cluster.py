"""Shared deployment state in the release namespace: a Lease lock and Secrets.

One release (namespace + release name) owns three kinds of objects, all
labelled ``piceli.io/state=<release>`` and excluded from every discovery
Piceli runs (so a plan never sees, adopts or prunes them):

``Lease piceli-lock-<release>`` (``coordination.k8s.io/v1``)
    The release lock. ``holderIdentity`` names the runner process holding
    it, ``leaseTransitions`` counts acquisitions (the **fence**), and the
    holder renews ``renewTime`` every third of ``leaseDurationSeconds``. A
    lease whose ``renewTime + leaseDurationSeconds`` passed is **stale**: the
    next deployer takes it over (compare-and-swap on ``resourceVersion``) and
    increments ``leaseTransitions``, so the previous holder's next write is
    refused (``state-lock-lost``).
``Secret piceli-state-<release>`` (type ``piceli.io/state``)
    The head: a manifest naming the generation, the chunk Secrets, the
    snapshot's SHA-256 and the fence of the writer.
``Secret piceli-state-<release>-g<generation>-<n>``
    The snapshot (:mod:`piceli.state.snapshot`) in chunks of at most 512 KiB.

A write (:meth:`ClusterStore.push`) creates the new generation's chunks,
renews the lease (fencing: holder and transitions must still match), then
replaces the head with a compare-and-swap on its ``resourceVersion``; only
then are the previous generation's chunks deleted. A writer that dies at any
point leaves the previous generation intact and readable.

Secrets hold the whole snapshot, including the secret store, so secret
material is never stored in a ConfigMap; protect them like any other Secret
(RBAC, encryption at rest). The kubeconfig needs ``get``, ``create``,
``patch`` and ``delete`` on ``secrets`` and ``leases`` in the namespace.

Importing this module is side-effect free.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlencode

from piceli.state.errors import StateError

LOCK_PREFIX = "piceli-lock-"
STATE_PREFIX = "piceli-state-"
#: Label on every state object; discovery lists with ``!piceli.io/state``.
STATE_LABEL = "piceli.io/state"
STATE_TYPE = "piceli.io/state"
MANIFEST_SCHEMA = "piceli.state.v1"
FIELD_MANAGER = "piceli-state"
CHUNK_BYTES = 512 * 1024
MAX_CHUNKS = 64
MAX_RESPONSE_BYTES = 2_000_000
LEASE_API = "/apis/coordination.k8s.io/v1/namespaces/{namespace}/leases"
SECRET_API = "/api/v1/namespaces/{namespace}/secrets"
_LABELS = {"app.kubernetes.io/managed-by": "piceli"}


def default_holder() -> str:
    """This process's holder identity: ``<host>/<pid>/<random>`` (no command line)."""
    host = (socket.gethostname() or "runner").split(".")[0][:40] or "runner"
    return f"{host}/{os.getpid()}/{uuid.uuid4().hex[:8]}"


def micro_time(value: float) -> str:
    """``MicroTime`` as the API server writes it."""
    return (
        datetime.fromtimestamp(value, UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def parse_time(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


@dataclass(frozen=True)
class Fence:
    """What a write proves: the lease holder and its acquisition count."""

    holder: str
    transitions: int

    def to_dict(self) -> dict[str, Any]:
        return {"holder": self.holder, "transitions": self.transitions}


class Api:
    """Minimal JSON requests over a caller-owned ``ApiClient``.

    No retries or redirects; error bodies are never read (they may carry
    server detail); responses are bounded. Independent of the release
    engine's call slots, so the lease heartbeat never competes with an apply.
    """

    def __init__(self, client: Any, request_seconds: float) -> None:
        self.client = client
        self.request_seconds = request_seconds
        self.host = client.configuration.host.rstrip("/")

    def call(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        query: dict[str, str] | None = None,
        merge: bool = False,
    ) -> tuple[int, dict[str, Any] | None]:
        from urllib3 import Timeout

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/merge-patch+json"
            if merge
            else "application/json",
        }
        self.client.update_params_for_auth(headers, [], ["BearerToken"])
        url = self.host + path + ("?" + urlencode(query) if query else "")
        response = None
        try:
            response = self.client.rest_client.pool_manager.request(
                method,
                url,
                body=None if body is None else json.dumps(body).encode(),
                headers=headers,
                preload_content=False,
                retries=False,
                redirect=False,
                timeout=Timeout(total=self.request_seconds),
            )
            if response.status in (401, 403):
                raise StateError(
                    "state-access-denied",
                    f"the kubeconfig may not {method} {path.rsplit('/', 2)[-2]} "
                    "in the namespace (shared state needs get, create, patch and "
                    "delete on secrets and leases)",
                )
            if not 200 <= response.status < 300:
                return response.status, None
            raw = response.read(MAX_RESPONSE_BYTES + 1, decode_content=True)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise StateError("state-corrupt", "a state object is over the limit")
            value = json.loads(raw or b"{}")
            return response.status, value if isinstance(value, dict) else None
        except StateError:
            raise
        except Exception as error:  # transport detail may carry credentials
            raise StateError(
                "state-unavailable",
                f"the shared state could not be reached ({type(error).__name__})",
            ) from None
        finally:
            if response is not None:
                response.close()

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if close is not None:
            close()


class ClusterStore:
    """The lock and snapshot of one release, in its namespace.

    :param api: Requests to the cluster (:class:`Api`).
    :param namespace: The release namespace.
    :param name: The release name (lock and state object names derive from it).
    :param layout: ``pipeline`` or ``release``: what kind of state directory
        the snapshot is; a mismatch is refused (``state-layout-mismatch``).
    :param lease_seconds: Lease duration; a holder that stops renewing for
        this long can be taken over.
    :param lock_code: The error code of a held lock (``pipeline-locked`` or
        ``release-locked``).
    """

    def __init__(
        self,
        api: Api,
        *,
        namespace: str,
        name: str,
        layout: str,
        lease_seconds: int = 60,
        lock_code: str = "pipeline-locked",
        identity: dict[str, str] | None = None,
        holder: str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.api = api
        self.namespace = namespace
        self.name = name
        self.layout = layout
        self.lease_seconds = int(lease_seconds)
        self.lock_code = lock_code
        self.identity = dict(identity or {})
        self.holder = holder or default_holder()
        self.clock = clock
        self.fence: Fence | None = None
        #: The previous holder when :meth:`acquire` took a stale lease over.
        self.took_over: dict[str, Any] | None = None
        self.lost: StateError | None = None
        self._mutex = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._renewed = 0.0

    # ------------------------------------------------------------ names
    @property
    def lease_name(self) -> str:
        return LOCK_PREFIX + self.name

    @property
    def head_name(self) -> str:
        return STATE_PREFIX + self.name

    def _path(self, api: str, name: str | None = None) -> str:
        root = api.format(namespace=quote(self.namespace, safe=""))
        return root + ("/" + quote(name, safe="") if name else "")

    def _metadata(self, name: str, **labels: str) -> dict[str, Any]:
        return {
            "name": name,
            "namespace": self.namespace,
            "labels": {**_LABELS, STATE_LABEL: self.name, **labels},
        }

    def _get(self, api: str, name: str) -> dict[str, Any] | None:
        status, value = self.api.call("GET", self._path(api, name))
        if status == 404:
            return None
        if value is None:
            raise StateError(
                "state-unavailable", f"reading {name} failed with HTTP {status}"
            )
        return value

    def _write(
        self, method: str, api: str, name: str | None, body: dict[str, Any]
    ) -> tuple[int, dict[str, Any] | None]:
        return self.api.call(
            method,
            self._path(api, name),
            body=body,
            query={"fieldManager": FIELD_MANAGER},
            merge=method == "PATCH",
        )

    def _delete(self, api: str, name: str) -> None:
        current = self._get(api, name)
        if current is None:
            return
        metadata = current.get("metadata") or {}
        self.api.call(
            "DELETE",
            self._path(api, name),
            body={
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "preconditions": {
                    "uid": metadata.get("uid"),
                    "resourceVersion": metadata.get("resourceVersion"),
                },
                "propagationPolicy": "Background",
            },
        )

    # ------------------------------------------------------------ lease
    def lease(self) -> dict[str, Any] | None:
        """The lock's Lease object, if it exists."""
        return self._get(LEASE_API, self.lease_name)

    def _expired(self, spec: dict[str, Any], now: float) -> bool:
        renewed = parse_time(spec.get("renewTime")) or parse_time(
            spec.get("acquireTime")
        )
        duration = spec.get("leaseDurationSeconds")
        if renewed is None or not isinstance(duration, int):
            return True
        return renewed + duration <= now

    def describe_lock(self) -> dict[str, Any] | None:
        """``{"holder", "expires_in", "transitions"}`` of a held lock, else ``None``."""
        lease = self.lease()
        spec = (lease or {}).get("spec") or {}
        holder = spec.get("holderIdentity")
        now = self.clock()
        if not holder or self._expired(spec, now):
            return None
        renewed = parse_time(spec.get("renewTime")) or now
        return {
            "holder": holder,
            "expires_in": max(0, round(renewed + spec["leaseDurationSeconds"] - now)),
            "transitions": int(spec.get("leaseTransitions") or 0),
        }

    def acquire(self) -> Fence:
        """Take the lock, or take a stale one over; refuse a held one."""
        with self._mutex:
            for _ in range(5):
                now = self.clock()
                lease = self.lease()
                spec_now = {
                    "holderIdentity": self.holder,
                    "leaseDurationSeconds": self.lease_seconds,
                    "acquireTime": micro_time(now),
                    "renewTime": micro_time(now),
                }
                if lease is None:
                    status, _ = self._write(
                        "POST",
                        LEASE_API,
                        None,
                        {
                            "apiVersion": "coordination.k8s.io/v1",
                            "kind": "Lease",
                            "metadata": self._metadata(self.lease_name),
                            "spec": {**spec_now, "leaseTransitions": 1},
                        },
                    )
                    if status == 409:
                        continue
                    self._accepted(status, "creating the lock")
                    return self._held(Fence(self.holder, 1), now)
                spec = lease.get("spec") or {}
                holder = spec.get("holderIdentity")
                if holder and not self._expired(spec, now):
                    renewed = parse_time(spec.get("renewTime")) or now
                    expires = max(
                        0, round(renewed + spec["leaseDurationSeconds"] - now)
                    )
                    raise StateError(
                        self.lock_code,
                        f"release {self.name!r} in namespace {self.namespace!r} is "
                        f"locked by {holder} (lease expires in {expires}s unless "
                        "renewed)",
                        details={"holder": holder, "expires_in": expires},
                    )
                transitions = int(spec.get("leaseTransitions") or 0) + 1
                metadata = lease.get("metadata") or {}
                status, _ = self._write(
                    "PATCH",
                    LEASE_API,
                    self.lease_name,
                    {
                        "metadata": {
                            "uid": metadata.get("uid"),
                            "resourceVersion": metadata.get("resourceVersion"),
                        },
                        "spec": {**spec_now, "leaseTransitions": transitions},
                    },
                )
                if status == 409:
                    continue
                self._accepted(status, "taking the lock")
                if holder:
                    self.took_over = {"holder": holder}
                return self._held(Fence(self.holder, transitions), now)
            raise StateError(
                self.lock_code,
                f"release {self.name!r}: the lock changed hands while acquiring it",
            )

    def _accepted(self, status: int, what: str) -> None:
        if not 200 <= status < 300:
            raise StateError("state-unavailable", f"{what} failed with HTTP {status}")

    def _held(self, fence: Fence, now: float) -> Fence:
        self.fence, self.lost, self._renewed = fence, None, now
        return fence

    def renew(self) -> None:
        """Prove the lock is still ours and extend it (fencing before a write)."""
        with self._mutex:
            if self.lost is not None:
                raise self.lost
            if self.fence is None:
                raise StateError("state-lock-lost", "the release lock is not held")
            for _ in range(3):
                lease = self.lease()
                spec = (lease or {}).get("spec") or {}
                if (
                    lease is None
                    or spec.get("holderIdentity") != self.fence.holder
                    or int(spec.get("leaseTransitions") or 0) != self.fence.transitions
                ):
                    holder = spec.get("holderIdentity") or "nobody"
                    self.lost = StateError(
                        "state-lock-lost",
                        f"the lock of release {self.name!r} is now held by {holder}; "
                        "this run stopped writing",
                        details={"holder": holder},
                    )
                    raise self.lost
                now = self.clock()
                metadata = lease.get("metadata") or {}
                status, _ = self._write(
                    "PATCH",
                    LEASE_API,
                    self.lease_name,
                    {
                        "metadata": {
                            "uid": metadata.get("uid"),
                            "resourceVersion": metadata.get("resourceVersion"),
                        },
                        "spec": {"renewTime": micro_time(now)},
                    },
                )
                if status == 409:
                    continue
                self._accepted(status, "renewing the lock")
                self._renewed = now
                return
            raise StateError("state-unavailable", "the lock kept changing; not renewed")

    def check(self) -> None:
        """Raise the loss the heartbeat recorded (no request)."""
        if self.lost is not None:
            raise self.lost

    def start_heartbeat(self) -> None:
        """Renew the lease every third of its duration until :meth:`release`."""
        if self._thread is not None:
            return
        interval = max(0.2, self.lease_seconds / 3)
        self._stop.clear()

        def beat() -> None:
            while not self._stop.wait(interval):
                try:
                    self.renew()
                except StateError as error:
                    if error.code == "state-lock-lost":
                        return
                    if self.clock() - self._renewed > self.lease_seconds:
                        self.lost = StateError(
                            "state-lock-lost",
                            f"the lock of release {self.name!r} could not be renewed "
                            f"for {self.lease_seconds}s; this run stopped writing",
                        )
                        return

        self._thread = threading.Thread(target=beat, name="piceli-lease", daemon=True)
        self._thread.start()

    def release(self) -> None:
        """Stop renewing and free the lock when it is still ours (best effort)."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=self.api.request_seconds + 1)
        with self._mutex:
            fence, self.fence = self.fence, None
            if fence is None or self.lost is not None:
                return
            try:
                lease = self.lease()
                spec = (lease or {}).get("spec") or {}
                if lease is None or spec.get("holderIdentity") != fence.holder:
                    return
                metadata = lease.get("metadata") or {}
                self._write(
                    "PATCH",
                    LEASE_API,
                    self.lease_name,
                    {
                        "metadata": {
                            "uid": metadata.get("uid"),
                            "resourceVersion": metadata.get("resourceVersion"),
                        },
                        "spec": {"holderIdentity": None},
                    },
                )
            except StateError:
                pass

    # ------------------------------------------------------------ state
    def head(self) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """``(head Secret, manifest)``; both ``None`` when no state was pushed."""
        value = self._get(SECRET_API, self.head_name)
        if value is None:
            return None, None
        try:
            manifest = json.loads(
                base64.b64decode((value.get("data") or {})["manifest"])
            )
        except (KeyError, ValueError, TypeError):
            raise StateError(
                "state-corrupt", f"Secret {self.head_name} is not a state head"
            ) from None
        if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA:
            raise StateError(
                "state-corrupt", f"Secret {self.head_name} has an unknown schema"
            )
        if manifest.get("layout") != self.layout:
            raise StateError(
                "state-layout-mismatch",
                f"the shared state of {self.name!r} belongs to a "
                f"{manifest.get('layout')} spec, not to a {self.layout} spec",
            )
        return value, manifest

    def pull(self) -> tuple[bytes | None, dict[str, Any] | None]:
        """``(snapshot, manifest)``, or ``(None, None)`` when there is none yet."""
        for _ in range(3):
            _, manifest = self.head()
            if manifest is None:
                return None, None
            parts: list[bytes] = []
            for name in manifest.get("chunks", []):
                value = self._get(SECRET_API, str(name))
                if value is None:
                    break  # a newer generation replaced it meanwhile: read again
                try:
                    parts.append(base64.b64decode((value.get("data") or {})["chunk"]))
                except (KeyError, ValueError, TypeError):
                    raise StateError(
                        "state-corrupt", f"Secret {name} is not a state chunk"
                    ) from None
            else:
                data = b"".join(parts)
                if hashlib.sha256(data).hexdigest() != manifest.get("sha256"):
                    raise StateError(
                        "state-corrupt",
                        f"the shared state of {self.name!r} does not match its digest",
                    )
                return data, manifest
        raise StateError(
            "state-corrupt", f"the shared state of {self.name!r} kept changing"
        )

    def push(self, data: bytes, *, files: int = 0) -> dict[str, Any]:
        """Write ``data`` as the next generation (fenced); returns the manifest."""
        with self._mutex:
            self.renew()
            assert self.fence is not None
            digest = hashlib.sha256(data).hexdigest()
            head, manifest = self.head()
            if manifest is not None:
                recorded = (manifest.get("fence") or {}).get("transitions", 0)
                if int(recorded) > self.fence.transitions:
                    self.lost = StateError(
                        "state-lock-lost",
                        f"a newer lock holder wrote the state of {self.name!r}",
                    )
                    raise self.lost
                if manifest.get("sha256") == digest:
                    return manifest
            generation = int((manifest or {}).get("generation", 0)) + 1
            chunks = [
                data[index : index + CHUNK_BYTES]
                for index in range(0, len(data), CHUNK_BYTES)
            ] or [b""]
            if len(chunks) > MAX_CHUNKS:
                raise StateError(
                    "state-too-large",
                    f"the state of {self.name!r} is {len(data)} bytes compressed; "
                    f"the limit is {MAX_CHUNKS * CHUNK_BYTES}",
                )
            names = [f"{self.head_name}-g{generation}-{n}" for n in range(len(chunks))]
            for name, chunk in zip(names, chunks, strict=True):
                self._create_chunk(name, chunk, generation)
            self.renew()  # the fence, right before the commit point
            new = {
                "schema": MANIFEST_SCHEMA,
                "name": self.name,
                "namespace": self.namespace,
                "layout": self.layout,
                "generation": generation,
                "chunks": names,
                "sha256": digest,
                "bytes": len(data),
                "files": files,
                "fence": self.fence.to_dict(),
                "updated_at": micro_time(self.clock()),
            }
            encoded = base64.b64encode(
                json.dumps(new, sort_keys=True).encode()
            ).decode()
            if head is None:
                status, _ = self._write(
                    "POST",
                    SECRET_API,
                    None,
                    {
                        "apiVersion": "v1",
                        "kind": "Secret",
                        "metadata": self._metadata(self.head_name),
                        "type": STATE_TYPE,
                        "data": {"manifest": encoded},
                    },
                )
            else:
                metadata = head.get("metadata") or {}
                status, _ = self._write(
                    "PATCH",
                    SECRET_API,
                    self.head_name,
                    {
                        "metadata": {
                            "uid": metadata.get("uid"),
                            "resourceVersion": metadata.get("resourceVersion"),
                        },
                        "data": {"manifest": encoded},
                    },
                )
            if status == 409:
                self.lost = StateError(
                    "state-lock-lost",
                    f"the state of {self.name!r} changed under this run's lock",
                )
                raise self.lost
            self._accepted(status, "writing the state head")
            for name in (manifest or {}).get("chunks", []):
                self._forget(str(name))
            for index in range(len(names), len(names) + MAX_CHUNKS):
                # Leftovers of a writer that died before its head update.
                stray = f"{self.head_name}-g{generation}-{index}"
                if self._get(SECRET_API, stray) is None:
                    break
                self._forget(stray)
            return new

    def _create_chunk(self, name: str, chunk: bytes, generation: int) -> None:
        body = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": self._metadata(
                name, **{"piceli.io/state-generation": str(generation)}
            ),
            "type": STATE_TYPE,
            "data": {"chunk": base64.b64encode(chunk).decode()},
        }
        status, _ = self._write("POST", SECRET_API, None, body)
        if status == 409:
            # A writer died after creating it and before its head update:
            # unreferenced by any head, so it is garbage.
            self._delete(SECRET_API, name)
            status, _ = self._write("POST", SECRET_API, None, body)
        self._accepted(status, f"writing {name}")

    def _forget(self, name: str) -> None:
        try:
            self._delete(SECRET_API, name)
        except StateError:
            pass  # an unreferenced chunk; the next push deletes it again

    def close(self) -> None:
        self.release()
        self.api.close()


def open_store(
    target: Any,
    *,
    name: str,
    layout: str,
    lease_seconds: int = 60,
    lock_code: str = "pipeline-locked",
) -> ClusterStore:
    """A :class:`ClusterStore` for a verified ``KubeconfigTarget``.

    The kubeconfig and context are explicit (never the current context);
    the kube-system and namespace UIDs are read and checked against the
    target's pins before anything else.
    """
    from piceli.k8s.ops.provider_factory import (
        api_client_from_kubeconfig,
        read_cluster_identity,
    )

    client = api_client_from_kubeconfig(
        target.kubeconfig,
        target.context,
        transport=target.transport,
        exec_policy=target.exec_policy,
    )
    try:
        identity = read_cluster_identity(client, target)
    except BaseException:
        client.close()
        raise
    return ClusterStore(
        Api(client, target.request_seconds),
        namespace=target.namespace,
        name=name,
        layout=layout,
        lease_seconds=lease_seconds,
        lock_code=lock_code,
        identity={
            "cluster_uid": identity.cluster_uid,
            "namespace_uid": identity.namespace_uid,
        },
    )
