"""Owner-controlled OCI registry that one node pulls from without registry configuration.

The registry runs with the host network and listens only on the node loopback
(``127.0.0.1:<port>``). containerd treats ``localhost``/``127.0.0.1`` registries
as plain-HTTP capable, so the node's kubelet pulls
``127.0.0.1:<port>/<repo>@sha256:…`` with no ``registries.yaml`` or
``certs.d`` entry, and nothing listens on a LAN address. Pushes go through a
``kubectl port-forward`` to the registry pod.

This is the single-node (pull-local) mode. Pulling from other nodes needs a
reachable address, TLS, authentication and a node-side registry configuration;
see ``docs/node_local_registry.md``.
"""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING, Any, ClassVar

import yaml
from kubernetes import client
from pydantic import Field, NonNegativeInt, field_validator, model_validator

from piceli.k8s.templates.auxiliary import names, quantity
from piceli.k8s.templates.auxiliary.labels import Labels
from piceli.k8s.templates.deployable import base

if TYPE_CHECKING:
    from piceli.k8s.ops.plan import DeploymentComponent, ResourceIntent

#: distribution/distribution 3.1.1 (``docker.io/library/registry:3.1.1``), multi-arch index.
DEFAULT_REGISTRY_IMAGE = (
    "docker.io/library/registry:3.1.1"
    "@sha256:325b4b29b041e82803abeb703e201655e4e23ab83264ec1a7c9ddb0a5b14a6e0"
)
RETAIN_ANNOTATION = "piceli.io/retained"
CONFIG_DIGEST_ANNOTATION = "piceli.io/registry-config-sha256"
CONFIG_DIR = "/etc/distribution"
CONFIG_FILE = f"{CONFIG_DIR}/config.yml"
STORAGE_DIR = "/var/lib/registry"

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_PINNED_IMAGE_RE = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")
# distribution reference grammar: path components separated by "/"
_REPOSITORY_RE = re.compile(
    r"^[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*$"
)
# the longest suffix appended to ``name`` for derived objects ("-storage", "-config", "-gc")
_MAX_NAME = 63 - len("-storage")


class NodeLocalRegistry(base.Deployable):
    """
    A single-node OCI registry bound to the node loopback.

    Produces a ConfigMap (the complete registry configuration), a retained
    PersistentVolumeClaim (unless ``host_path`` is set) and a one-replica
    Deployment pinned to ``node_name`` with ``hostNetwork: true``.

    :param node_name: ``kubernetes.io/hostname`` of the node that runs the
        registry and pulls from it. Storage and pulls are node-local.
    :param name: Base name of the generated objects.
    :param port: Loopback port. Workloads pull ``127.0.0.1:<port>/…``.
    :param image: Registry image, pinned by digest (``…@sha256:…``).
    :param storage: Size of the retained PVC.
    :param storage_class: StorageClass of the PVC; ``None`` uses the default.
    :param host_path: Use this node directory instead of a PVC.
    :param existing_claim: Use this existing PersistentVolumeClaim (never
        created, changed or deleted) instead of creating ``<name>-storage``.
    :param selector: Deployment selector labels. Kubernetes never changes a
        Deployment's selector, so a registry taken over from another owner
        keeps its own; default ``app.kubernetes.io/name``/``instance`` labels.
    :param rolling_update: Use ``RollingUpdate`` with ``maxSurge: 0`` and
        ``maxUnavailable: 1`` instead of ``Recreate``: with one replica the old
        pod still stops (and frees the port) before the new one starts. For a
        registry taken over from a Deployment whose strategy is
        ``RollingUpdate``: Kubernetes refuses to switch it to ``Recreate``
        while another manager owns its ``rollingUpdate`` settings.
    :param index_platforms: Accept image indexes that hold only these
        platforms' manifests (``os/arch``), as a mirror of a multi-arch image
        copies only the node's platform. ``None`` (default) keeps the
        registry's rule: every platform of an index must be present.
    :param read_only: Maintenance mode: pulls work, pushes and deletes are refused.
    :param stopped: Scale to zero replicas (maintenance, e.g. garbage collection).
    :param run_as_user: Non-root UID/GID/fsGroup; ``None`` keeps the image user (root).
    :param cpu_request: CPU request.
    :param memory_request: Memory request.
    :param memory_limit: Memory limit; ``None`` for no limit.
    :param upload_purge_age: Purge incomplete uploads older than this (Go duration).
    :param labels: Extra labels for every generated object.
    """

    KIND: ClassVar[str] = "NodeLocalRegistry"

    node_name: str = Field(min_length=1, max_length=253)
    name: names.Name = "registry"
    port: int = Field(default=5000, ge=1024, le=65535)
    image: str = DEFAULT_REGISTRY_IMAGE
    storage: quantity.Quantity = "10Gi"
    storage_class: str | None = None
    host_path: str | None = None
    existing_claim: names.Name | None = None
    selector: dict[str, str] | None = None
    index_platforms: list[str] | None = None
    rolling_update: bool = False
    read_only: bool = False
    stopped: bool = False
    run_as_user: int | None = Field(default=65532, ge=1)
    cpu_request: quantity.Quantity = "10m"
    memory_request: quantity.Quantity = "32Mi"
    memory_limit: quantity.Quantity | None = "256Mi"
    upload_purge_age: str = "168h"
    labels: Labels | None = None

    @field_validator("image")
    @classmethod
    def _image_pinned(cls, value: str) -> str:
        if not _PINNED_IMAGE_RE.fullmatch(value):
            raise ValueError(
                f"registry image must be pinned by digest (<ref>@sha256:<64 hex>): {value!r}"
            )
        return value

    @field_validator("host_path")
    @classmethod
    def _absolute_host_path(cls, value: str | None) -> str | None:
        if value is not None and (not value.startswith("/") or value == "/"):
            raise ValueError(f"host_path must be an absolute directory: {value!r}")
        return value

    @field_validator("index_platforms")
    @classmethod
    def _platforms(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if not value:
            raise ValueError("index_platforms needs at least one os/arch platform")
        for item in value:
            if not re.fullmatch(r"[a-z0-9_.-]{1,32}/[a-z0-9_.-]{1,32}", item):
                raise ValueError(f"index platform must be os/arch: {item!r}")
        return sorted(set(value))

    @field_validator("selector")
    @classmethod
    def _selector(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is not None and not value:
            raise ValueError("selector needs at least one label")
        return value

    @model_validator(mode="after")
    def _one_storage(self) -> NodeLocalRegistry:
        if self.host_path and self.existing_claim:
            raise ValueError("give host_path or existing_claim, not both")
        return self

    @model_validator(mode="after")
    def _name_fits_suffixes(self) -> NodeLocalRegistry:
        if len(self.name) > _MAX_NAME:
            raise ValueError(
                f"name {self.name!r} is too long; derived object names need <= {_MAX_NAME} chars"
            )
        return self

    # -- names -------------------------------------------------------------------------

    @property
    def config_map_name(self) -> str:
        return f"{self.name}-config"

    @property
    def claim_name(self) -> str:
        return self.existing_claim or f"{self.name}-storage"

    @property
    def default_selector_labels(self) -> dict[str, str]:
        return {
            "app.kubernetes.io/name": "node-local-registry",
            "app.kubernetes.io/instance": self.name,
        }

    @property
    def selector_labels(self) -> dict[str, str]:
        return dict(self.selector) if self.selector else self.default_selector_labels

    @property
    def object_labels(self) -> dict[str, str]:
        return {
            **(self.labels or {}),
            **self.default_selector_labels,
            **self.selector_labels,
        }

    # -- pull references ---------------------------------------------------------------

    @property
    def address(self) -> str:
        """The node-side registry host, ``127.0.0.1:<port>``."""
        return f"127.0.0.1:{self.port}"

    def pull_reference(self, repository: str, digest: str) -> str:
        """The image reference a workload on ``node_name`` uses: ``127.0.0.1:<port>/<repo>@sha256:…``."""
        return pull_reference(repository, digest, port=self.port)

    # -- registry configuration ----------------------------------------------------------

    def registry_config(self) -> dict[str, Any]:
        """The complete registry configuration (no image defaults are relied on).

        The image's default configuration also starts a debug/metrics listener on
        ``:5001`` (all interfaces), which with ``hostNetwork`` would be reachable
        from the LAN; this configuration has no ``http.debug`` section.
        """
        return {
            "version": 0.1,
            "log": {"level": "info", "fields": {"service": "registry"}},
            "storage": {
                "filesystem": {"rootdirectory": STORAGE_DIR},
                "delete": {"enabled": True},
                "maintenance": {
                    "uploadpurging": {
                        "enabled": True,
                        "age": self.upload_purge_age,
                        "interval": "24h",
                        "dryrun": False,
                    },
                    "readonly": {"enabled": self.read_only},
                },
            },
            "http": {
                "addr": self.address,
                "headers": {"X-Content-Type-Options": ["nosniff"]},
            },
            "health": {
                "storagedriver": {"enabled": True, "interval": "10s", "threshold": 3}
            },
            **(
                {
                    "validation": {
                        "manifests": {
                            "indexes": {
                                "platforms": "list",
                                "platformlist": [
                                    {
                                        "os": item.split("/")[0],
                                        "architecture": item.split("/")[1],
                                    }
                                    for item in self.index_platforms
                                ],
                            }
                        }
                    }
                }
                if self.index_platforms
                else {}
            ),
        }

    def config_yaml(self) -> str:
        return yaml.safe_dump(self.registry_config(), sort_keys=True)

    # -- pod building blocks -----------------------------------------------------------

    def _volumes(self) -> list[client.V1Volume]:
        storage: client.V1Volume
        if self.host_path:
            storage = client.V1Volume(
                name="storage",
                host_path=client.V1HostPathVolumeSource(
                    path=self.host_path, type="DirectoryOrCreate"
                ),
            )
        else:
            storage = client.V1Volume(
                name="storage",
                persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                    claim_name=self.claim_name
                ),
            )
        config = client.V1Volume(
            name="config",
            config_map=client.V1ConfigMapVolumeSource(
                name=self.config_map_name,
                items=[client.V1KeyToPath(key="config.yml", path="config.yml")],
            ),
        )
        return [storage, config]

    @staticmethod
    def _volume_mounts() -> list[client.V1VolumeMount]:
        return [
            client.V1VolumeMount(name="storage", mount_path=STORAGE_DIR),
            client.V1VolumeMount(name="config", mount_path=CONFIG_DIR, read_only=True),
        ]

    def _pod_security_context(self) -> client.V1PodSecurityContext:
        return client.V1PodSecurityContext(
            fs_group=self.run_as_user,
            seccomp_profile=client.V1SeccompProfile(type="RuntimeDefault"),
        )

    def _container_security_context(self) -> client.V1SecurityContext:
        uid = self.run_as_user
        return client.V1SecurityContext(
            run_as_user=uid,
            run_as_group=uid,
            run_as_non_root=True if uid is not None else None,
            allow_privilege_escalation=False,
            read_only_root_filesystem=True,
            capabilities=client.V1Capabilities(drop=["ALL"]),
        )

    def _storage_init_containers(self) -> list[client.V1Container] | None:
        """Hand a ``host_path`` directory to ``run_as_user``.

        ``fsGroup`` does not apply to hostPath volumes and ``DirectoryOrCreate``
        creates the directory as root, so a non-root registry could not write
        it. This init container runs as root with only ``CAP_CHOWN`` and changes
        the owner of entries that are not already owned by ``run_as_user``.
        """
        if not self.host_path or self.run_as_user is None:
            return None
        uid = self.run_as_user
        script = (
            f"find {STORAGE_DIR} '(' ! -user {uid} -o ! -group {uid} ')' "
            f"-exec chown -h {uid}:{uid} {{}} +"
        )
        return [
            client.V1Container(
                name="storage-owner",
                image=self.image,
                image_pull_policy="IfNotPresent",
                command=["sh", "-c", script],
                volume_mounts=[
                    client.V1VolumeMount(name="storage", mount_path=STORAGE_DIR)
                ],
                resources=self._resources(),
                security_context=client.V1SecurityContext(
                    run_as_user=0,
                    run_as_group=0,
                    run_as_non_root=False,
                    allow_privilege_escalation=False,
                    read_only_root_filesystem=True,
                    capabilities=client.V1Capabilities(drop=["ALL"], add=["CHOWN"]),
                ),
            )
        ]

    def _port_claim(self) -> client.V1ContainerPort:
        # With hostNetwork the API defaults hostPort to containerPort, so the
        # scheduler treats the port as taken on the node: two registries (or a
        # registry and its stopped-mode GC job) never share it.
        return client.V1ContainerPort(
            name="registry", container_port=self.port, protocol="TCP"
        )

    def _probe(self, *, period: int, failure_threshold: int) -> client.V1Probe:
        # kubelet runs in the node network namespace, which is the pod's
        # (hostNetwork), so it reaches the loopback-only listener. Without
        # ``host`` kubelet would probe the node IP, where nothing listens.
        return client.V1Probe(
            http_get=client.V1HTTPGetAction(
                host="127.0.0.1", path="/v2/", port=self.port, scheme="HTTP"
            ),
            period_seconds=period,
            timeout_seconds=2,
            failure_threshold=failure_threshold,
        )

    def _node_pod_spec(
        self, containers: list[client.V1Container], restart_policy: str
    ) -> client.V1PodSpec:
        return client.V1PodSpec(
            host_network=True,
            dns_policy="ClusterFirstWithHostNet",
            node_selector={"kubernetes.io/hostname": self.node_name},
            # automountServiceAccountToken is not set: Piceli's plan redaction
            # treats any "*token" key as sensitive, which would make the
            # manifest unexecutable in a release. The registry makes no API
            # calls; the namespace's default ServiceAccount has no RBAC grants
            # unless the owner adds some.
            enable_service_links=False,
            restart_policy=restart_policy,
            security_context=self._pod_security_context(),
            init_containers=self._storage_init_containers(),
            containers=containers,
            volumes=self._volumes(),
        )

    def _resources(self) -> client.V1ResourceRequirements:
        return client.V1ResourceRequirements(
            requests={"cpu": self.cpu_request, "memory": self.memory_request},
            limits={"memory": self.memory_limit} if self.memory_limit else None,
        )

    # -- objects -----------------------------------------------------------------------

    def get_config_map(self) -> client.V1ConfigMap:
        return client.V1ConfigMap(
            api_version="v1",
            kind="ConfigMap",
            metadata=client.V1ObjectMeta(
                name=self.config_map_name, labels=self.object_labels
            ),
            data={"config.yml": self.config_yaml()},
        )

    def get_claim(self) -> client.V1PersistentVolumeClaim | None:
        if self.host_path or self.existing_claim:
            return None
        return client.V1PersistentVolumeClaim(
            api_version="v1",
            kind="PersistentVolumeClaim",
            metadata=client.V1ObjectMeta(
                name=self.claim_name,
                labels=self.object_labels,
                annotations={RETAIN_ANNOTATION: "true"},
            ),
            spec=client.V1PersistentVolumeClaimSpec(
                access_modes=["ReadWriteOnce"],
                storage_class_name=self.storage_class,
                resources=client.V1VolumeResourceRequirements(
                    requests={"storage": self.storage}
                ),
            ),
        )

    def get_deployment(self) -> client.V1Deployment:
        container = client.V1Container(
            name="registry",
            image=self.image,
            image_pull_policy="IfNotPresent",
            args=[CONFIG_FILE],
            ports=[self._port_claim()],
            volume_mounts=self._volume_mounts(),
            readiness_probe=self._probe(period=5, failure_threshold=3),
            liveness_probe=self._probe(period=20, failure_threshold=6),
            startup_probe=self._probe(period=2, failure_threshold=60),
            resources=self._resources(),
            security_context=self._container_security_context(),
        )
        config_digest = hashlib.sha256(self.config_yaml().encode()).hexdigest()
        return client.V1Deployment(
            api_version="apps/v1",
            kind="Deployment",
            metadata=client.V1ObjectMeta(name=self.name, labels=self.object_labels),
            spec=client.V1DeploymentSpec(
                replicas=0 if self.stopped else 1,
                # Recreate: the old pod releases the loopback port (and the
                # storage) before the new one starts. A one-replica rolling
                # update with no surge does the same.
                strategy=(
                    client.V1DeploymentStrategy(
                        type="RollingUpdate",
                        rolling_update=client.V1RollingUpdateDeployment(
                            max_surge=0, max_unavailable=1
                        ),
                    )
                    if self.rolling_update
                    else client.V1DeploymentStrategy(type="Recreate")
                ),
                selector=client.V1LabelSelector(match_labels=self.selector_labels),
                template=client.V1PodTemplateSpec(
                    metadata=client.V1ObjectMeta(
                        labels=self.object_labels,
                        annotations={CONFIG_DIGEST_ANNOTATION: config_digest},
                    ),
                    spec=self._node_pod_spec([container], "Always"),
                ),
            ),
        )

    def get(self) -> list[Any]:
        claim = self.get_claim()
        return [
            self.get_config_map(),
            *([claim] if claim is not None else []),
            self.get_deployment(),
        ]

    # -- maintenance -------------------------------------------------------------------

    def garbage_collection(self, **kwargs: Any) -> NodeLocalRegistryGarbageCollection:
        """A Job that runs ``registry garbage-collect`` on this registry's storage."""
        return NodeLocalRegistryGarbageCollection(registry=self, **kwargs)

    # -- release compositions ----------------------------------------------------------

    def resource_intents(self, namespace: str) -> tuple[ResourceIntent, ...]:
        """Plain manifests as ``ResourceIntent`` objects for a ``DeploymentComposition``."""
        from piceli.k8s.ops.plan import ResourceIntent

        return tuple(
            ResourceIntent.from_manifest(_namespaced(manifest, namespace))
            for manifest in self.api_data()
        )

    def component(
        self,
        namespace: str,
        *,
        component_name: str = "registry",
        dependencies: tuple[str, ...] = (),
    ) -> DeploymentComponent:
        """A ``DeploymentComponent`` for ``piceli release`` composition functions."""
        from piceli.k8s.ops.plan import DeploymentComponent

        return DeploymentComponent(
            component_name, self.resource_intents(namespace), dependencies
        )


class NodeLocalRegistryGarbageCollection(base.Deployable):
    """
    A Job that runs ``registry garbage-collect`` against a :class:`NodeLocalRegistry`.

    Garbage collection must not run while the registry accepts writes: an upload
    that races the mark phase can lose blobs. The Job enforces that:

    * registry not in ``read_only`` mode (default): the Job claims the registry's
      host port on the node, so the scheduler only starts it while the registry
      is stopped (``stopped=True`` or scaled to zero), and the registry cannot
      start again until the Job's pod has finished;
    * registry in ``read_only`` mode: pulls keep working and the Job runs next to
      it (distribution supports GC against a read-only registry).

    :param registry: The registry whose storage is collected.
    :param name: Job name; defaults to ``<registry name>-gc``.
    :param delete_untagged: Also delete manifests that no tag references
        (``--delete-untagged``). Digest-only pushes are untagged: collect them
        only when no workload pulls them any more.
    :param dry_run: Only report what would be deleted.
    :param cleanup_after_seconds: ``ttlSecondsAfterFinished`` of the Job.
    """

    KIND: ClassVar[str] = "NodeLocalRegistryGarbageCollection"

    registry: NodeLocalRegistry
    name: names.Name | None = None
    delete_untagged: bool = True
    dry_run: bool = False
    cleanup_after_seconds: NonNegativeInt | None = 3600

    @property
    def job_name(self) -> str:
        return self.name or f"{self.registry.name}-gc"

    @property
    def claims_registry_port(self) -> bool:
        return not self.registry.read_only

    def command(self) -> list[str]:
        command = ["registry", "garbage-collect"]
        if self.delete_untagged:
            command.append("--delete-untagged")
        if self.dry_run:
            command.append("--dry-run")
        return [*command, CONFIG_FILE]

    def get_job(self) -> client.V1Job:
        registry = self.registry
        container = client.V1Container(
            name="garbage-collect",
            image=registry.image,
            image_pull_policy="IfNotPresent",
            command=self.command(),
            ports=[registry._port_claim()] if self.claims_registry_port else None,
            volume_mounts=registry._volume_mounts(),
            resources=registry._resources(),
            security_context=registry._container_security_context(),
        )
        pod_spec = registry._node_pod_spec([container], "Never")
        if not self.claims_registry_port:
            pod_spec.host_network = None
            pod_spec.dns_policy = None
        labels = {
            **registry.object_labels,
            "app.kubernetes.io/component": "garbage-collection",
        }
        return client.V1Job(
            api_version="batch/v1",
            kind="Job",
            metadata=client.V1ObjectMeta(name=self.job_name, labels=labels),
            spec=client.V1JobSpec(
                backoff_limit=0,
                ttl_seconds_after_finished=self.cleanup_after_seconds,
                template=client.V1PodTemplateSpec(
                    # not the registry's selector labels: the Deployment must not adopt it
                    metadata=client.V1ObjectMeta(
                        labels={
                            "app.kubernetes.io/name": "node-local-registry-gc",
                            "app.kubernetes.io/instance": registry.name,
                        }
                    ),
                    spec=pod_spec,
                ),
            ),
        )

    def get(self) -> list[client.V1Job]:
        return [self.get_job()]


def pull_reference(repository: str, digest: str, *, port: int = 5000) -> str:
    """``127.0.0.1:<port>/<repository>@<digest>``: a node-local, immutable pull reference."""
    if not _REPOSITORY_RE.fullmatch(repository):
        raise ValueError(f"invalid repository name: {repository!r}")
    if not _DIGEST_RE.fullmatch(digest):
        raise ValueError(f"digest must be sha256:<64 lowercase hex>: {digest!r}")
    if not 1 <= port <= 65535:
        raise ValueError(f"invalid port: {port}")
    return f"127.0.0.1:{port}/{repository}@{digest}"


def _namespaced(manifest: dict[str, Any], namespace: str) -> dict[str, Any]:
    if not namespace:
        raise ValueError("namespace is required")
    return {**manifest, "metadata": {**manifest["metadata"], "namespace": namespace}}
