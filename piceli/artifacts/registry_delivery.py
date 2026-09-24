"""Digest-approved image delivery to an OCI registry (the default delivery mode).

A registry moves only the blobs it does not already have, and workloads pull
an immutable ``<registry>/<repository>@sha256:<manifest digest>``. The flow:

1. Check the grant (exact ``oci://`` target, unexpired) and the tool pins.
2. Read the source once (``docker image save <image ID>`` or the archive) and
   build the push plan (:mod:`piceli.artifacts.image_manifest`). The config
   digest recomputed from the streamed bytes must equal the approval; on a
   mismatch nothing is sent to the registry.
3. Optionally start a supervised, loopback-only ``kubectl port-forward`` to the
   registry Service or Pod, so the registry is never exposed.
4. ``HEAD`` the manifest, the tag and every blob. When the manifest, the tag
   and all blobs are present the result is ``already-present``.
5. Upload only the missing blobs (the config from memory, layers by reading
   the source a second time), then ``PUT`` the manifest.
6. Read the manifest back by digest and check its bytes and its config digest;
   check that the tag resolves to it.

The receipt (``piceli.registry-delivery.v1``) records the manifest and config
digests, which blobs were uploaded or skipped, and the node-side ``pull_ref``.
"""

from __future__ import annotations

import hashlib
import io
import re
import tarfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import urlsplit

from piceli.artifacts.delivery import (
    ArchiveSource,
    DeliveryGrant,
    DockerImageSource,
    _now,
    _RegularFile,
)
from piceli.artifacts.image_manifest import (
    BlobRef,
    PushPlan,
    manifest_config_digest,
    member_name,
    scan_image_stream,
)
from piceli.artifacts.node_transport import Runner, SubprocessRunner
from piceli.artifacts.plan import validate_digest
from piceli.artifacts.process import ProcessLimits, ToolPin
from piceli.artifacts.registry import (
    RegistryCredentials,
    RegistryEndpoint,
    RegistryError,
    RegistryTarget,
    StreamedOciRegistryClient,
    host_port,
    is_loopback,
    validate_host,
)

SCHEMA = "piceli.registry-delivery.v1"
FORWARD_NAME = "piceli-registry-delivery"
_NAME = re.compile(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?")
_FORWARD_TARGET = re.compile(r"(?:service|pod|deployment)/[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?")
_CONTEXT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@:/-]{0,252}")
_QUERY = ProcessLimits(60, 4 * 1024 * 1024)


class _Failure(Exception):
    def __init__(self, result: str, reason: str) -> None:
        super().__init__(reason)
        self.result = result
        self.reason = reason


def node_registry_address(value: str) -> str:
    """Validate ``host[:port]`` as the registry address nodes pull from."""
    if not isinstance(value, str) or len(value) > 260 or not value.isprintable():
        raise ValueError("invalid node registry")
    parts = urlsplit("//" + value)
    if (
        not parts.hostname
        or parts.path
        or parts.query
        or parts.fragment
        or parts.username is not None
    ):
        raise ValueError("node registry must be host[:port]")
    try:
        port = parts.port
    except ValueError as error:
        raise ValueError("invalid node registry port") from error
    validate_host(parts.hostname)
    return host_port(parts.hostname, port)


@dataclass(frozen=True)
class RegistryForward:
    """A supervised ``kubectl port-forward`` that reaches the registry for one push.

    The local end is the ``oci://`` target's loopback port; ``remote_port`` is
    the registry port on the Service or Pod. ``kubeconfig`` is always explicit
    and passed to kubectl with ``--kubeconfig``.
    """

    namespace: str
    target: str
    remote_port: int
    kubeconfig: Path = field(repr=False)
    kubectl: ToolPin
    context: str | None = None
    startup_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not isinstance(self.namespace, str) or not _NAME.fullmatch(self.namespace):
            raise ValueError("invalid forward namespace")
        if not isinstance(self.target, str) or not _FORWARD_TARGET.fullmatch(
            self.target
        ):
            raise ValueError("forward target must be service/NAME, deployment/NAME or pod/NAME")
        if (
            isinstance(self.remote_port, bool)
            or not isinstance(self.remote_port, int)
            or not 0 < self.remote_port < 65536
        ):
            raise ValueError("invalid forward remote port")
        if not isinstance(self.kubeconfig, Path) or not self.kubeconfig.is_absolute():
            raise ValueError("an explicit absolute kubeconfig path is required")
        if self.context is not None and (
            not isinstance(self.context, str) or not _CONTEXT.fullmatch(self.context)
        ):
            raise ValueError("invalid kube context")
        if not 0 < self.startup_seconds <= 300:
            raise ValueError("invalid forward startup timeout")

    def public(self, local_port: int) -> dict[str, Any]:
        """Receipt view: never the kubeconfig path."""
        return {
            "namespace": self.namespace,
            "target": self.target,
            "local_port": local_port,
            "remote_port": self.remote_port,
            "context": self.context,
        }


Forwarder = Callable[[RegistryForward, int], AbstractContextManager[None]]


@contextmanager
def supervised_forward(forward: RegistryForward, local_port: int) -> Iterator[None]:
    """Run one loopback port-forward under ``ForwardSupervisor`` for a block.

    The forward is probed with ``GET /v2/`` (any status below 500 is healthy,
    so an auth challenge counts) and restarted by the supervisor if the probe
    keeps failing. It is stopped when the block exits, whatever happens.
    """
    # The Kubernetes layer is only loaded when a forward is actually used.
    from piceli.k8s.observe import ForwardSupervisor, PortForward

    supervisor = ForwardSupervisor(
        kubeconfig=forward.kubeconfig,
        context=forward.context,
        kubectl=str(forward.kubectl.path),
    )
    try:
        supervisor.add_or_update(
            PortForward(
                name=FORWARD_NAME,
                namespace=forward.namespace,
                target=forward.target,
                local_port=local_port,
                remote_port=forward.remote_port,
                health_path="/v2/",
            ),
            persist=False,
        )
        supervisor.start(FORWARD_NAME)
        deadline = time.monotonic() + forward.startup_seconds
        while True:
            (status,) = supervisor.statuses()
            if status.health == "healthy":
                break
            if status.health == "conflict":
                raise _Failure("failed", "forward-port-in-use")
            if status.state == "failed" or time.monotonic() > deadline:
                raise _Failure("failed", "forward-unavailable")
            time.sleep(0.1)
        yield
    finally:
        supervisor.close()


@dataclass(frozen=True)
class RegistryDelivery:
    """Push one approved image to one registry repository.

    ``docker`` (with ``docker_socket``) is required for a
    :class:`~piceli.artifacts.delivery.DockerImageSource`. ``credentials`` and
    ``ca_file`` configure the registry connection; ``forward`` makes it
    reachable through a supervised port-forward. ``runner``, ``forwarder`` and
    ``client_factory`` are seams for tests.
    """

    docker: ToolPin | None = None
    docker_socket: Path | None = field(default=None, repr=False)
    credentials: RegistryCredentials | None = field(default=None, repr=False)
    ca_file: Path | None = field(default=None, repr=False)
    forward: RegistryForward | None = None
    runner: Runner = field(default_factory=SubprocessRunner, repr=False)
    forwarder: Forwarder = field(default=supervised_forward, repr=False)
    client_factory: Callable[[RegistryEndpoint], StreamedOciRegistryClient] = field(
        default=StreamedOciRegistryClient, repr=False
    )

    def __post_init__(self) -> None:
        if self.docker_socket is not None and not self.docker_socket.is_absolute():
            raise ValueError("explicit absolute Docker socket required")
        if self.ca_file is not None and not self.ca_file.is_absolute():
            raise ValueError("explicit absolute CA file required")

    def _docker(self, *arguments: str) -> list[str]:
        if self.docker is None or self.docker_socket is None:
            raise ValueError("a pinned docker tool and socket are required")
        return [
            str(self.docker.path),
            "--host",
            f"unix://{self.docker_socket}",
            *arguments,
        ]

    def preview(
        self,
        target: RegistryTarget,
        approved_digest: str,
        node_registry: str | None = None,
    ) -> dict[str, Any]:
        """What a delivery would contact, without contacting anything."""
        validate_digest(approved_digest)
        return {
            "schema": SCHEMA,
            "target": self._public_target(target),
            "approved_digest": approved_digest,
            "node_registry": self._node_registry(target, node_registry),
            "requires_explicit_grant": True,
        }

    def _public_target(self, target: RegistryTarget) -> dict[str, Any]:
        endpoint_auth = "none" if self.credentials is None else self.credentials.scheme
        return {
            **target.public(),
            "auth": endpoint_auth,
            "forward": (
                self.forward.public(target.effective_port) if self.forward else None
            ),
        }

    def _node_registry(self, target: RegistryTarget, node_registry: str | None) -> str:
        if node_registry is not None:
            return node_registry_address(node_registry)
        if self.forward is not None:
            raise ValueError("a forwarded push needs an explicit node registry")
        return target.registry

    # delivery -----------------------------------------------------------------

    def deliver(
        self,
        source: DockerImageSource | ArchiveSource,
        target: RegistryTarget,
        grant: DeliveryGrant,
        *,
        node_registry: str | None = None,
        limits: ProcessLimits | None = None,
        cancel: threading.Event | None = None,
    ) -> dict[str, Any]:
        """Push, or confirm presence of, exactly the approved image.

        Returns a secret-safe receipt for every operational outcome. Invalid
        grants, tools or inputs raise ``ValueError`` before anything runs.
        """
        if (
            not isinstance(grant.target, str)
            or not grant.target.startswith("oci://")
            or RegistryTarget.parse(grant.target).identity != target.identity
            or grant.expires_at <= time.time()
        ):
            raise ValueError("exact unexpired delivery grant required")
        if self.forward is not None and (
            not is_loopback(target.host) or target.port is None
        ):
            raise ValueError("a forwarded push needs a loopback target with a port")
        pull_host = self._node_registry(target, node_registry)
        tools: dict[str, ToolPin] = {}
        if isinstance(source, DockerImageSource):
            if self.docker is None or self.docker_socket is None:
                raise ValueError("a pinned docker tool and socket are required")
            tools["docker"] = self.docker
        if self.forward is not None:
            tools["kubectl"] = self.forward.kubectl
        for tool in tools.values():
            tool.verify()
        limits = limits or ProcessLimits(600, 1_048_576)
        started_at, started = _now(), time.monotonic()
        deadline = min(grant.expires_at, time.time() + limits.max_seconds)
        receipt: dict[str, Any] = {
            "schema": SCHEMA,
            "approved_digest": grant.approved_digest,
            "target": self._public_target(target),
            "source": {
                "kind": "docker-image"
                if isinstance(source, DockerImageSource)
                else "archive"
            },
            "tools": {key: tool.sha256 for key, tool in sorted(tools.items())},
            "image": {"config_digest": None, "manifest_digest": None},
            "previous": None,
            "blobs": {
                "total": 0,
                "uploaded": 0,
                "skipped": 0,
                "uploaded_bytes": 0,
                "skipped_bytes": 0,
                "items": [],
            },
            "node_registry": pull_host,
            "pull_ref": None,
            "pushed": False,
            "started_at": started_at,
        }
        try:
            result = self._deliver(
                source, target, grant, limits, deadline, cancel, receipt
            )
            receipt["image"]["config_digest"] = grant.approved_digest
            receipt["pull_ref"] = (
                f"{pull_host}/{target.repository}@{receipt['image']['manifest_digest']}"
            )
            receipt.update(
                result=result,
                reason=None,
                state="succeeded",
                pushed=result == "pushed",
            )
        except _Failure as failure:
            receipt.update(
                result=failure.result,
                reason=failure.reason,
                state="rejected" if failure.result == "rejected" else "failed",
            )
        finally:
            for tool in tools.values():
                tool.verify()
        receipt["finished_at"] = _now()
        receipt["seconds"] = round(time.monotonic() - started, 3)
        return receipt

    def _local_image_id(self, source: DockerImageSource) -> str:
        result = self.runner.capture(
            self._docker("image", "inspect", "--format", "{{.Id}}", source.reference),
            _QUERY,
            {},
        )
        if result.state != "succeeded":
            raise _Failure("failed", "source-unavailable")
        return validate_digest(result.stdout.decode().strip())

    def _containerd_image_store(self) -> bool:
        result = self.runner.capture(
            self._docker("info", "--format", "{{json .DriverStatus}}"), _QUERY, {}
        )
        return (
            result.state == "succeeded"
            and b"io.containerd.snapshotter" in result.stdout
        )

    def _read_source(
        self,
        source: DockerImageSource | ArchiveSource,
        save_argv: list[str] | None,
        reader: Callable[[BinaryIO], Any],
        deadline: float,
        cancel: threading.Event | None,
        failure: str,
    ) -> Any:
        """Hand the image tar to ``reader`` once (a fresh read every call)."""
        try:
            if save_argv is None:
                assert isinstance(source, ArchiveSource)
                with _RegularFile(source.path, source.max_bytes) as stream:
                    return reader(stream)
            remaining = max(0.001, min(3600.0, deadline - time.time()))
            saved, value = self.runner.read(
                save_argv, reader, ProcessLimits(remaining), {}, cancel
            )
            if saved.state != "succeeded":
                raise _Failure("failed", "source-unavailable")
            return value
        except (_Failure, RegistryError):
            raise
        except InterruptedError as error:
            raise _Failure("failed", "cancelled") from error
        except (ValueError, OSError, tarfile.TarError, LookupError) as error:
            raise _Failure(
                "rejected" if failure == "invalid-archive" else "failed", failure
            ) from error

    def _deliver(
        self,
        source: DockerImageSource | ArchiveSource,
        target: RegistryTarget,
        grant: DeliveryGrant,
        limits: ProcessLimits,
        deadline: float,
        cancel: threading.Event | None,
        receipt: dict[str, Any],
    ) -> str:
        approved = grant.approved_digest
        save_argv: list[str] | None = None
        # 1. Prove the source locally before any byte leaves the machine.
        if isinstance(source, DockerImageSource):
            local_id = self._local_image_id(source)
            receipt["source"]["local_image_id"] = local_id
            if local_id != approved and not self._containerd_image_store():
                # Classic store: the image ID *is* the config digest.
                raise _Failure("rejected", "digest-mismatch")
            save_argv = self._docker("image", "save", local_id)
        max_bytes = (
            source.max_bytes if isinstance(source, ArchiveSource) else 64 * 1024**3
        )
        plan: PushPlan = self._read_source(
            source,
            save_argv,
            lambda stream: scan_image_stream(
                stream, max_bytes=max_bytes, cancel=cancel
            ),
            deadline,
            cancel,
            "invalid-archive"
            if isinstance(source, ArchiveSource)
            else "invalid-image-stream",
        )
        receipt["source"].update(
            format=plan.format, stream_sha256=plan.stream_sha256, bytes=plan.bytes
        )
        receipt["image"].update(
            manifest_digest=plan.manifest_digest,
            manifest_media_type=plan.manifest_media_type,
            manifest_origin=plan.manifest_origin,
            platform=plan.platform,
            layers=len(plan.layers),
        )
        # 2. The gate: nothing reaches the registry unless the config matches.
        if plan.config_digest != approved:
            raise _Failure("rejected", "digest-mismatch")
        blobs = plan.blobs()
        receipt["blobs"]["total"] = len(blobs)
        forwarded: AbstractContextManager[None] = (
            self.forwarder(self.forward, target.effective_port)
            if self.forward is not None
            else nullcontext()
        )
        with forwarded:
            timeout = max(1.0, min(120.0, deadline - time.time()))
            client = self.client_factory(
                target.endpoint(
                    credentials=self.credentials, ca_file=self.ca_file, timeout=timeout
                )
            )
            try:
                return self._push(
                    client, source, save_argv, plan, target, deadline, cancel, receipt
                )
            except RegistryError as error:
                raise _Failure("failed", error.reason) from error

    def _push(
        self,
        client: StreamedOciRegistryClient,
        source: DockerImageSource | ArchiveSource,
        save_argv: list[str] | None,
        plan: PushPlan,
        target: RegistryTarget,
        deadline: float,
        cancel: threading.Event | None,
        receipt: dict[str, Any],
    ) -> str:
        repository = target.repository
        client.authenticate(repository)
        # 3. What does the registry already have?
        tag_digest = (
            client.manifest_digest(repository, target.tag) if target.tag else None
        )
        manifest_present = (
            client.manifest_digest(repository, plan.manifest_digest)
            == plan.manifest_digest
        )
        missing: list[BlobRef] = []
        items: list[dict[str, Any]] = []
        for blob in plan.blobs():
            if client.blob_size(repository, blob.digest) is None:
                missing.append(blob)
            else:
                items.append(blob.public() | {"action": "skipped"})
        if target.tag and tag_digest not in (None, plan.manifest_digest):
            receipt["previous"] = {"tag_digest": tag_digest}
        tag_ok = target.tag is None or tag_digest == plan.manifest_digest
        if manifest_present and tag_ok and not missing:
            self._verify(client, plan, target, receipt["approved_digest"])
            self._account(receipt, plan, items)
            return "already-present"
        # 4. Upload only what is missing.
        uploads = self._upload(
            client, source, save_argv, plan, repository, missing, deadline, cancel
        )
        items.extend(uploads)
        self._account(receipt, plan, items)
        # Defence in depth: the manifest itself must name the approved config.
        if manifest_config_digest(plan.manifest) != receipt["approved_digest"]:
            raise _Failure("rejected", "digest-mismatch")
        client.push_manifest(
            repository,
            target.tag or plan.manifest_digest,
            plan.manifest,
            plan.manifest_media_type,
        )
        # 5. Read back what the registry now serves.
        self._verify(client, plan, target, receipt["approved_digest"])
        return "pushed"

    @staticmethod
    def _account(
        receipt: dict[str, Any], plan: PushPlan, items: list[dict[str, Any]]
    ) -> None:
        blobs = receipt["blobs"]
        order = {blob.digest: index for index, blob in enumerate(plan.blobs())}
        blobs["items"] = sorted(items, key=lambda item: order[item["digest"]])
        for item in items:
            key = "uploaded" if item["action"] == "uploaded" else "skipped"
            blobs[key] += 1
            blobs[f"{key}_bytes"] += item["size"]

    def _upload(
        self,
        client: StreamedOciRegistryClient,
        source: DockerImageSource | ArchiveSource,
        save_argv: list[str] | None,
        plan: PushPlan,
        repository: str,
        missing: list[BlobRef],
        deadline: float,
        cancel: threading.Event | None,
    ) -> list[dict[str, Any]]:
        done: list[dict[str, Any]] = []

        def check() -> None:
            if cancel is not None and cancel.is_set():
                raise _Failure("failed", "cancelled")
            if time.time() > deadline:
                raise _Failure("failed", "timed-out")

        def upload(blob: BlobRef, stream: BinaryIO) -> None:
            check()
            result = client.push_blob(
                repository, blob.digest, blob.size, stream, cancel=cancel
            )
            done.append(blob.public() | {"action": "uploaded", "mode": result.mode})

        layers = {blob.member: blob for blob in missing if blob != plan.config}
        if plan.config in missing:
            upload(plan.config, io.BytesIO(plan.config_body))
        if not layers:
            return done

        def reader(stream: BinaryIO) -> None:
            mode = "r|" if save_argv is not None else "r:"
            with tarfile.open(fileobj=stream, mode=mode) as archive:  # type: ignore[call-overload]
                for info in archive:
                    if not info.isfile():
                        continue
                    blob = layers.get(member_name(info.name))
                    if blob is None:
                        continue
                    if info.size != blob.size:
                        raise ValueError("layer changed since it was read")
                    member = archive.extractfile(info)
                    assert member is not None
                    upload(blob, member)
                    del layers[blob.member]
                    if not layers:
                        break
            if layers:
                raise ValueError("layer vanished since it was read")

        self._read_source(
            source,
            save_argv,
            reader,
            deadline,
            cancel,
            "source-changed",
        )
        return done

    @staticmethod
    def _verify(
        client: StreamedOciRegistryClient,
        plan: PushPlan,
        target: RegistryTarget,
        approved: str,
    ) -> None:
        body, _ = client.get_manifest(target.repository, plan.manifest_digest)
        if (
            "sha256:" + hashlib.sha256(body).hexdigest() != plan.manifest_digest
            or manifest_config_digest(body) != approved
        ):
            raise _Failure("failed", "verification-failed")
        if target.tag is not None and (
            client.manifest_digest(target.repository, target.tag)
            != plan.manifest_digest
        ):
            raise _Failure("failed", "verification-failed")
