"""Side effects of the build and deliver stages behind one replaceable seam.

:class:`Backend` runs the pinned ``docker`` for builds and image queries, the
registry and node delivery modules, and creates release runners. Tests pass
a fake backend; the stages themselves never spawn a process directly.

Importing this module is side-effect free; tools are discovered on first use.
"""

from __future__ import annotations

import socket
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from piceli.artifacts.build_spec import BuildGrant, BuildSpec, DockerTool
    from piceli.artifacts.process import ToolPin
    from piceli.artifacts.source_identity import InputsLock, InputsSpec
    from piceli.k8s.release_runner import ReleaseRunner
    from piceli.k8s.release_spec import ReleaseSpec
    from piceli.pipeline.checks import CheckRunner
    from piceli.pipeline.model import Target

GRANT_SECONDS = 3600.0


@dataclass(frozen=True)
class RegistryRoute:
    """How images reach a registry and how nodes pull them.

    ``push`` is ``host[:port]`` of the push endpoint (``None`` with a
    ``forward``: a free loopback port is chosen per connection);
    ``node_registry`` is what workloads pull from. ``forward`` names the
    registry workload (``deployment/NAME``) reached through a supervised
    ``kubectl port-forward`` to ``remote_port``.
    """

    push: str | None
    node_registry: str | None
    tls: bool | None = None
    forward: str | None = None
    namespace: str | None = None
    remote_port: int | None = None
    kubeconfig: Path | None = None
    context: str | None = None
    credentials: Path | None = None
    ca_file: Path | None = None

    def url(self, repository: str, port: int | None = None) -> str:
        host = self.push if self.push is not None else f"127.0.0.1:{port}"
        tls = "" if self.tls is None else f"?tls={'true' if self.tls else 'false'}"
        return f"oci://{host}/{repository}{tls}"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class Backend:
    """The default, real implementation of every stage side effect."""

    def __init__(self) -> None:
        self._docker: tuple[ToolPin, Path] | None = None
        self._build_tool: DockerTool | None = None

    # ------------------------------------------------------------ tools
    def docker(self) -> tuple[ToolPin, Path]:
        """The pinned docker CLI and its unix socket."""
        if self._docker is None:
            from piceli.artifacts.delivery_inputs import (
                discover_docker_socket,
                discover_tool,
            )

            tool = discover_tool("docker")
            self._docker = (tool, discover_docker_socket(tool))
        return self._docker

    def build_tool(self) -> DockerTool:
        if self._build_tool is None:
            from piceli.artifacts.build_spec import DockerTool

            self._build_tool = DockerTool.discover()
        return self._build_tool

    # ----------------------------------------------------------- inputs
    def record_sources(
        self, inputs: InputsSpec, lock: InputsLock | None
    ) -> dict[str, Any]:
        from piceli.artifacts.source_identity import open_sources

        return open_sources(inputs, lock).to_dict()

    # ------------------------------------------------------------ build
    def image_present(self, image_id: str) -> bool:
        """Whether the local engine still holds the image with this config digest."""
        from piceli.artifacts.node_transport import SubprocessRunner
        from piceli.artifacts.process import ProcessLimits

        tool, sock = self.docker()
        result = SubprocessRunner().capture(
            [
                str(tool.path),
                "--host",
                f"unix://{sock}",
                "image",
                "inspect",
                "--format",
                "{{.Id}}",
                image_id,
            ],
            ProcessLimits(30, 65536),
            {},
        )
        return (
            result.state == "succeeded"
            and result.stdout.decode(errors="replace").strip() == image_id
        )

    def build(
        self,
        spec: BuildSpec,
        grant: BuildGrant,
        output_dir: Path,
        *,
        inputs: InputsSpec | None,
        lock: InputsLock | None,
        log: Path,
        progress: Callable[[str], None],
    ) -> dict[str, Any]:
        receipt = spec.run(
            grant,
            output_dir,
            inputs=inputs,
            lock=lock,
            docker=self.build_tool(),
            log=log,
            progress=progress,
        )
        return receipt.to_dict()

    # --------------------------------------------------------- registry
    def _forward(self, route: RegistryRoute, port: int) -> AbstractContextManager[None]:
        if route.forward is None:
            return nullcontext()
        from piceli.artifacts.registry_delivery import supervised_forward

        return supervised_forward(self._registry_forward(route), port)

    def _registry_forward(self, route: RegistryRoute) -> Any:
        from piceli.artifacts.delivery_inputs import discover_tool
        from piceli.artifacts.registry_delivery import RegistryForward

        assert route.forward and route.namespace and route.kubeconfig
        return RegistryForward(
            namespace=route.namespace,
            target=route.forward,
            remote_port=int(route.remote_port or 5000),
            kubeconfig=route.kubeconfig.absolute(),
            kubectl=discover_tool("kubectl"),
            context=route.context,
        )

    def _credentials(self, route: RegistryRoute) -> Any:
        if route.credentials is None:
            return None
        from piceli.artifacts.registry import RegistryCredentials

        return RegistryCredentials.load(route.credentials.absolute())

    def registry_present(
        self, route: RegistryRoute, manifests: Sequence[tuple[str, str]]
    ) -> list[bool]:
        """For each ``(repository, manifest digest)``: does the registry serve it?"""
        from piceli.artifacts.registry import (
            RegistryError,
            RegistryTarget,
            StreamedOciRegistryClient,
        )

        if not manifests:
            return []
        port = free_port()
        found: list[bool] = []
        try:
            with self._forward(route, port):
                credentials = self._credentials(route)
                for repository, manifest in manifests:
                    target = RegistryTarget.parse(route.url(repository, port))
                    client = StreamedOciRegistryClient(
                        target.endpoint(
                            credentials=credentials,
                            ca_file=route.ca_file,
                            timeout=30,
                        )
                    )
                    try:
                        client.authenticate(repository)
                        found.append(
                            client.manifest_digest(repository, manifest) == manifest
                        )
                    except RegistryError:
                        found.append(False)
        except Exception:
            return [False] * len(manifests)
        return found

    def registry_deliver(
        self, route: RegistryRoute, image_id: str, repository: str
    ) -> dict[str, Any]:
        from piceli.artifacts.delivery import DeliveryGrant, DockerImageSource
        from piceli.artifacts.registry import RegistryTarget
        from piceli.artifacts.registry_delivery import RegistryDelivery

        tool, sock = self.docker()
        port = free_port()
        url = route.url(repository, port)
        delivery = RegistryDelivery(
            docker=tool,
            docker_socket=sock,
            credentials=self._credentials(route),
            ca_file=route.ca_file.absolute() if route.ca_file else None,
            forward=self._registry_forward(route) if route.forward else None,
        )
        return delivery.deliver(
            DockerImageSource(image_id),
            RegistryTarget.parse(url),
            DeliveryGrant(image_id, url, time.time() + GRANT_SECONDS),
            node_registry=route.node_registry,
        )

    def mirror_deliver(
        self,
        route: RegistryRoute,
        reference: str,
        repository: str,
        *,
        platform: str | None,
        credentials: Path | None,
    ) -> dict[str, Any]:
        """Copy one digest-pinned image into ``repository``; returns the receipt.

        The source is pulled on this machine over OCI Distribution (anonymous,
        or with the private ``credentials`` file); the target is reached like a
        built image's push (``route``: port-forward, credentials, CA).
        """
        from piceli.artifacts.mirror import (
            MirrorSource,
            Platform,
            mirror_image,
        )
        from piceli.artifacts.registry import (
            RegistryCredentials,
            RegistryTarget,
            StreamedOciRegistryClient,
        )

        source = MirrorSource.parse(reference)
        source_target = RegistryTarget.parse(source.url)
        source_client = StreamedOciRegistryClient(
            source_target.endpoint(
                credentials=RegistryCredentials.load(credentials.absolute())
                if credentials is not None
                else None,
                timeout=120,
            ),
            actions="pull",
        )
        port = free_port()
        with self._forward(route, port):
            target = RegistryTarget.parse(route.url(repository, port))
            target_client = StreamedOciRegistryClient(
                target.endpoint(
                    credentials=self._credentials(route),
                    ca_file=route.ca_file.absolute() if route.ca_file else None,
                    timeout=120,
                )
            )
            return mirror_image(
                source,
                source_client,
                target_client,
                repository,
                platform=Platform.parse(platform) if platform else None,
                target_registry=route.push or route.forward or "node-loopback",
                node_registry=route.node_registry or target.registry,
            )

    # ---------------------------------------------------------- cluster
    def _api(self, target: Target) -> Any:
        from piceli.k8s.ops.provider_factory import api_client_from_kubeconfig

        return api_client_from_kubeconfig(
            target.kubeconfig.absolute(),
            target.context,
            transport=target.transport,  # type: ignore[arg-type]
            exec_policy=target.exec_policy(),
        )

    def list_deployments(self, target: Target) -> list[dict[str, Any]]:
        """Every Deployment of the target namespace, as plain manifests (read-only)."""
        import json

        from kubernetes.client import AppsV1Api

        client = self._api(target)
        try:
            response = AppsV1Api(client).list_namespaced_deployment(
                target.namespace,
                _preload_content=False,
                _request_timeout=target.request_seconds,
            )
            items = json.loads(response.data).get("items") or []
        finally:
            client.close()
        return [item for item in items if isinstance(item, dict)]

    def node_platform(self, target: Target, node: str) -> str:
        """``os/architecture`` the node reports (``status.nodeInfo``)."""
        import json

        from kubernetes.client import CoreV1Api

        client = self._api(target)
        try:
            response = CoreV1Api(client).read_node(
                node, _preload_content=False, _request_timeout=target.request_seconds
            )
            info = (json.loads(response.data).get("status") or {}).get("nodeInfo") or {}
        finally:
            client.close()
        return f"{info.get('operatingSystem') or 'linux'}/{info.get('architecture')}"

    # ------------------------------------------------------------- node
    def _node_delivery(self, url: str) -> tuple[Any, Any]:
        from piceli.artifacts.delivery import NodeDelivery
        from piceli.artifacts.delivery_inputs import discover_tool
        from piceli.artifacts.node_transport import NodeTarget

        target = NodeTarget.parse(url)
        tool, sock = self.docker()
        ssh = discover_tool("ssh") if target.transport == "ssh" else None
        return NodeDelivery(docker=tool, docker_socket=sock, ssh=ssh), target

    def node_present(self, url: str, reference: str, image_id: str) -> bool:
        delivery, target = self._node_delivery(url)
        try:
            image = delivery.inspect_node(target, reference)
        except Exception:
            return False
        return image is not None and image_id in image.config_digests

    def node_deliver(self, url: str, image_id: str, reference: str) -> dict[str, Any]:
        from piceli.artifacts.delivery import DeliveryGrant, DockerImageSource

        delivery, target = self._node_delivery(url)
        return dict(
            delivery.deliver(
                DockerImageSource(image_id),
                target,
                DeliveryGrant(image_id, url, time.time() + GRANT_SECONDS),
                reference=reference,
            )
        )

    # ---------------------------------------------------------- release
    def release_runner(self, spec: ReleaseSpec) -> ReleaseRunner:
        from piceli.k8s.release_runner import ReleaseRunner

        return ReleaseRunner(spec)

    # ----------------------------------------------------------- checks
    def check_runner(self) -> CheckRunner:
        from piceli.pipeline.checks import default_runner

        return default_runner()


def public(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """The small, path-free view of a delivery receipt used in events."""
    image = receipt.get("image") or {}
    return {
        key: value
        for key, value in {
            "result": receipt.get("result"),
            "state": receipt.get("state"),
            "reason": receipt.get("reason"),
            "config_digest": image.get("config_digest"),
            "manifest_digest": image.get("manifest_digest"),
            "reference": receipt.get("pull_ref") or image.get("reference"),
            "seconds": receipt.get("seconds"),
        }.items()
        if value is not None
    }
