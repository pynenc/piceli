"""Typed building blocks of an :class:`~piceli.app.App`.

Every class here is a frozen pydantic model, so a wrong value fails when it is
declared, with a pydantic ``ValidationError`` that names the field. Nothing in
this module contacts a cluster, and importing it does not import the
``kubernetes`` client.

The models render to plain manifest fragments; :class:`~piceli.app.App` turns
them into ``ResourceIntent`` objects.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    InstanceOf,
    NonNegativeInt,
    PositiveInt,
    field_validator,
    model_validator,
)

from piceli.app.access import Forward
from piceli.k8s.ops.secret_versions import SecretVersionRef

# ----------------------------------------------------------------- field types

_LABEL_NAME = re.compile(r"[A-Za-z0-9]([-_.A-Za-z0-9]{0,61}[A-Za-z0-9])?")
_DNS_SUBDOMAIN = re.compile(
    r"[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*"
)


def _port_name(value: str) -> str:
    if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", value) or len(value) > 15:
        raise ValueError(
            "port names are 1-15 lowercase letters, digits and '-' (IANA service name)"
        )
    if not re.search(r"[a-z]", value) or "--" in value:
        raise ValueError(
            "port names need at least one letter and no adjacent '-' characters"
        )
    return value


def _labels(value: dict[str, str]) -> dict[str, str]:
    for key, item in value.items():
        prefix, _, name = key.rpartition("/")
        if prefix and (len(prefix) > 253 or not _DNS_SUBDOMAIN.fullmatch(prefix)):
            raise ValueError(f"label key {key!r}: prefix must be a DNS subdomain")
        if not _LABEL_NAME.fullmatch(name):
            raise ValueError(
                f"label key {key!r}: name must be 1-63 alphanumerics, '-', '_' or '.'"
            )
        if item and not _LABEL_NAME.fullmatch(item):
            raise ValueError(
                f"label {key!r}: value {item!r} must be empty or 1-63 "
                "alphanumerics, '-', '_' or '.'"
            )
    return value


#: A DNS label (RFC 1123): Deployment, Service, container and volume names.
Name = Annotated[str, Field(pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", max_length=63)]
#: A DNS subdomain: ConfigMap, Secret and claim names.
ObjectName = Annotated[
    str,
    Field(
        pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$",
        max_length=253,
    ),
]
#: A Kubernetes quantity such as ``100m``, ``128Mi`` or ``1.5``.
Quantity = Annotated[
    str,
    Field(
        pattern=r"^[+]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+|Ki|Mi|Gi|Ti|Pi|Ei|n|u|m|k|M|G|T|P|E)?$"
    ),
]
EnvName = Annotated[str, Field(pattern=r"^[-._a-zA-Z][-._a-zA-Z0-9]*$")]
DataKey = Annotated[str, Field(pattern=r"^[-._a-zA-Z0-9]+$", max_length=253)]
AbsolutePath = Annotated[str, Field(pattern=r"^/")]
PortNumber = Annotated[int, Field(ge=1, le=65535)]
PortName = Annotated[str, AfterValidator(_port_name)]
Labels = Annotated[dict[str, str], AfterValidator(_labels)]
Protocol = Literal["TCP", "UDP", "SCTP"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _compact(**values: Any) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


# ---------------------------------------------------------------------- probes


class Probe(_Model):
    """A readiness, liveness or startup check of a container.

    Build probes with :meth:`http`, :meth:`tcp` or :meth:`exec` (also reachable
    as ``app.probe.http(...)``). Timing fields left as ``None`` keep the
    Kubernetes defaults and are not rendered.

    Invariants: an ``http`` probe has a ``path`` starting with ``/`` and a
    ``port``; a ``tcp`` probe has only a ``port``; an ``exec`` probe has only a
    non-empty ``command``.

    Example::

        Probe.http("/healthz", 8080, period_seconds=5)
    """

    action: Literal["http", "tcp", "exec"]
    port: PortNumber | PortName | None = None
    path: str | None = None
    scheme: Literal["HTTP", "HTTPS"] | None = None
    command: tuple[str, ...] | None = None
    initial_delay_seconds: NonNegativeInt | None = None
    period_seconds: PositiveInt | None = None
    timeout_seconds: PositiveInt | None = None
    failure_threshold: PositiveInt | None = None
    success_threshold: PositiveInt | None = None

    @model_validator(mode="after")
    def _shape(self) -> Probe:
        if self.action == "http":
            if self.port is None or not (self.path or "").startswith("/"):
                raise ValueError("an http probe needs a port and a path starting '/'")
            if self.command is not None:
                raise ValueError("an http probe has no command")
        elif self.action == "tcp":
            if self.port is None:
                raise ValueError("a tcp probe needs a port")
            if self.path is not None or self.command is not None or self.scheme:
                raise ValueError("a tcp probe has only a port")
        else:
            if not self.command:
                raise ValueError("an exec probe needs a non-empty command")
            if self.port is not None or self.path is not None or self.scheme:
                raise ValueError("an exec probe has only a command")
        return self

    @classmethod
    def http(
        cls,
        path: str,
        port: int | str,
        *,
        scheme: Literal["HTTP", "HTTPS"] | None = None,
        **timing: Any,
    ) -> Probe:
        """``GET path`` on ``port`` must answer 200-399."""
        return cls(action="http", path=path, port=port, scheme=scheme, **timing)

    @classmethod
    def tcp(cls, port: int | str, **timing: Any) -> Probe:
        """A TCP connection to ``port`` must succeed."""
        return cls(action="tcp", port=port, **timing)

    @classmethod
    def exec(cls, command: Sequence[str], **timing: Any) -> Probe:
        """``command`` run in the container must exit 0."""
        return cls(action="exec", command=tuple(command), **timing)

    def manifest(self) -> dict[str, Any]:
        if self.action == "http":
            action: dict[str, Any] = {
                "httpGet": _compact(path=self.path, port=self.port, scheme=self.scheme)
            }
        elif self.action == "tcp":
            action = {"tcpSocket": {"port": self.port}}
        else:
            action = {"exec": {"command": list(self.command or ())}}
        return {
            **action,
            **_compact(
                initialDelaySeconds=self.initial_delay_seconds,
                periodSeconds=self.period_seconds,
                timeoutSeconds=self.timeout_seconds,
                failureThreshold=self.failure_threshold,
                successThreshold=self.success_threshold,
            ),
        }


# ------------------------------------------------------------------- resources


class Resources(_Model):
    """CPU, memory and ephemeral-storage requests and limits of a container.

    Unset values are not rendered. Example::

        Resources(cpu="100m", memory="128Mi", memory_limit="256Mi")
    """

    cpu: Quantity | None = None
    memory: Quantity | None = None
    ephemeral_storage: Quantity | None = None
    cpu_limit: Quantity | None = None
    memory_limit: Quantity | None = None
    ephemeral_storage_limit: Quantity | None = None

    def manifest(self) -> dict[str, Any]:
        requests = _compact(
            cpu=self.cpu,
            memory=self.memory,
            **{"ephemeral-storage": self.ephemeral_storage},
        )
        limits = _compact(
            cpu=self.cpu_limit,
            memory=self.memory_limit,
            **{"ephemeral-storage": self.ephemeral_storage_limit},
        )
        return _compact(requests=requests or None, limits=limits or None)


# ------------------------------------------------------------------------- env


class SecretKey(_Model):
    """An environment value read from one key of a Secret (``secretKeyRef``).

    Usually obtained from ``app.secret(...).key("name")``, which checks that
    the key exists. The value stays in the Secret; the Deployment only names it.
    """

    secret: ObjectName
    key: DataKey
    optional: bool | None = None

    def manifest(self) -> dict[str, Any]:
        return {
            "secretKeyRef": _compact(
                name=self.secret, key=self.key, optional=self.optional
            )
        }


class ConfigKey(_Model):
    """An environment value read from one key of a ConfigMap (``configMapKeyRef``)."""

    config: ObjectName
    key: DataKey
    optional: bool | None = None

    def manifest(self) -> dict[str, Any]:
        return {
            "configMapKeyRef": _compact(
                name=self.config, key=self.key, optional=self.optional
            )
        }


class FieldRef(_Model):
    """An environment value from the downward API, such as ``status.podIP``."""

    field_path: str = Field(min_length=1)

    def manifest(self) -> dict[str, Any]:
        return {"fieldRef": {"fieldPath": self.field_path}}


EnvValue = str | SecretKey | ConfigKey | FieldRef


# --------------------------------------------------------- configs and secrets


def _object_name(value: Any) -> Any:
    return value.name if isinstance(value, Config | Secret) else value


class Config(_Model):
    """A ConfigMap declared with ``app.config(name, data)``.

    :param name: ConfigMap name.
    :param data: Public string values.
    :param component: Deployment component; defaults to ``name``.
    """

    name: ObjectName
    data: dict[DataKey, str]
    component: Name | None = None

    @property
    def component_name(self) -> str:
        return self.component or self.name

    def key(self, key: str) -> ConfigKey:
        """An env value from ``key``; the key must be declared in ``data``."""
        if key not in self.data:
            raise ValueError(
                f"config {self.name!r} has no key {key!r}; keys: {sorted(self.data)}"
            )
        return ConfigKey(config=self.name, key=key)


class Secret(_Model):
    """A Secret whose values are opaque secret versions.

    Values are :class:`~piceli.k8s.ops.secret_versions.SecretVersionRef` objects
    from ``ctx.secret(...)``; they are bound at apply time and never appear in
    the composition, the plan or the rendered manifests.

    :param name: Secret name.
    :param data: Key to secret version.
    :param type: Secret type, such as ``kubernetes.io/tls``.
    :param component: Deployment component; defaults to ``name``.
    """

    name: ObjectName
    data: dict[DataKey, InstanceOf[SecretVersionRef]] = Field(min_length=1)
    type: str = Field(default="Opaque", min_length=1)
    component: Name | None = None

    @field_validator("data", mode="before")
    @classmethod
    def _opaque_only(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if not isinstance(item, SecretVersionRef):
                    raise ValueError(
                        f"secret value {key!r} must be a secret version from "
                        "ctx.secret(...), not a plain value; put public values "
                        "in app.config(...)"
                    )
        return value

    @property
    def component_name(self) -> str:
        return self.component or self.name

    def key(self, key: str) -> SecretKey:
        """An env value from ``key``; the key must be declared in ``data``."""
        if key not in self.data:
            raise ValueError(
                f"secret {self.name!r} has no key {key!r}; keys: {sorted(self.data)}"
            )
        return SecretKey(secret=self.name, key=key)


# --------------------------------------------------------------------- volumes


class _Volume(_Model):
    name: Name | None = None

    def volume_name(self, mount_path: str) -> str:
        return self.name or self._default_name(mount_path)

    def _default_name(self, mount_path: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", mount_path.lower()).strip("-")[:63] or "root"

    def source(self) -> dict[str, Any]:
        raise NotImplementedError


class ConfigVolume(_Volume):
    """Mount a ConfigMap as files.

    :param config: A :class:`Config` from ``app.config`` or a ConfigMap name.
    :param name: Volume name; defaults to the ConfigMap name.
    :param items: Only these keys, as ``{key: relative path}``.
    :param default_mode: File mode, such as ``0o444``.
    """

    kind: Literal["config"] = "config"
    config: ObjectName
    items: dict[DataKey, str] | None = None
    default_mode: int | None = Field(default=None, ge=0, le=0o777)

    def __init__(self, config: Config | str, /, **data: Any) -> None:
        super().__init__(**{"config": _object_name(config), **data})

    def _default_name(self, mount_path: str) -> str:
        return self.config.replace(".", "-")[:63]

    def source(self) -> dict[str, Any]:
        return {
            "configMap": _compact(
                name=self.config,
                items=_items(self.items),
                defaultMode=self.default_mode,
            )
        }


class SecretVolume(_Volume):
    """Mount a Secret as files.

    :param secret: A :class:`Secret` from ``app.secret`` or a Secret name.
    :param name: Volume name; defaults to the Secret name.
    :param items: Only these keys, as ``{key: relative path}``.
    :param default_mode: File mode, such as ``0o400``.
    """

    kind: Literal["secret"] = "secret"
    secret: ObjectName
    items: dict[DataKey, str] | None = None
    default_mode: int | None = Field(default=None, ge=0, le=0o777)

    def __init__(self, secret: Secret | str, /, **data: Any) -> None:
        super().__init__(**{"secret": _object_name(secret), **data})

    def _default_name(self, mount_path: str) -> str:
        return self.secret.replace(".", "-")[:63]

    def source(self) -> dict[str, Any]:
        return {
            "secret": _compact(
                secretName=self.secret,
                items=_items(self.items),
                defaultMode=self.default_mode,
            )
        }


class MemoryVolume(_Volume):
    """A RAM-backed scratch directory (``emptyDir`` with ``medium: Memory``).

    Its content is lost when the pod stops and counts against the memory limit.

    :param name: Volume name; defaults to one derived from the mount path.
    :param size_limit: Maximum size, such as ``64Mi``.
    """

    kind: Literal["memory"] = "memory"
    size_limit: Quantity | None = None

    def source(self) -> dict[str, Any]:
        return {"emptyDir": _compact(medium="Memory", sizeLimit=self.size_limit)}


class ExistingClaim(_Volume):
    """Mount a PersistentVolumeClaim that the release never creates, changes or deletes.

    Data safety as a type: an ``ExistingClaim`` produces **no** resource intent,
    so no plan can create, patch, adopt or prune the claim. The claim must
    already exist (made by an administrator, another tool, or an earlier
    release); a pod that mounts a missing claim stays ``Pending``. Rendering
    refuses any composition that also manages a claim with this name.

    :param claim: Name of the existing PersistentVolumeClaim.
    :param name: Volume name; defaults to the claim name.
    :param read_only: Mount read-only.

    Example::

        volumes={"/data": ExistingClaim("cache-state")}
    """

    kind: Literal["existing-claim"] = "existing-claim"
    claim: ObjectName
    read_only: bool = False

    def __init__(self, claim: str, /, **data: Any) -> None:
        super().__init__(**{"claim": claim, **data})

    def _default_name(self, mount_path: str) -> str:
        return self.claim.replace(".", "-")[:63]

    def source(self) -> dict[str, Any]:
        return {
            "persistentVolumeClaim": _compact(
                claimName=self.claim, readOnly=True if self.read_only else None
            )
        }


Volume = ConfigVolume | SecretVolume | MemoryVolume | ExistingClaim


def _items(items: Mapping[str, str] | None) -> list[dict[str, str]] | None:
    if items is None:
        return None
    return [{"key": key, "path": path} for key, path in items.items()]


class Mount(_Model):
    """A volume mount with options; use it where a plain volume is not enough.

    :param volume: The volume.
    :param read_only: Mount read-only.
    :param sub_path: Mount only this path inside the volume.
    """

    volume: Volume
    read_only: bool = False
    sub_path: str | None = Field(default=None, min_length=1)

    def __init__(self, volume: Volume, /, **data: Any) -> None:
        super().__init__(**{"volume": volume, **data})


# ------------------------------------------------------------------ containers


class ContainerPort(_Model):
    """A named or non-TCP container port. A plain ``int`` is enough otherwise."""

    port: PortNumber
    name: PortName | None = None
    protocol: Protocol | None = None

    def manifest(self) -> dict[str, Any]:
        return _compact(containerPort=self.port, name=self.name, protocol=self.protocol)


class Container(_Model):
    """One container: image, command, env, ports, probes, resources and mounts.

    ``app.deployment(...)`` builds the main container from its own keyword
    arguments; pass ``Container`` objects as ``sidecars`` or ``init``.

    :param name: Container name, unique in the pod.
    :param image: Image reference; prefer ``ctx.image(...)`` (pinned by digest).
    :param command: Entrypoint override.
    :param args: Arguments.
    :param working_dir: Working directory.
    :param env: ``{NAME: value}`` where a value is a ``str``, a
        :class:`SecretKey`, a :class:`ConfigKey` or a :class:`FieldRef`.
    :param ports: Container ports, as ``int`` or :class:`ContainerPort`.
    :param ready: Readiness probe.
    :param live: Liveness probe.
    :param startup: Startup probe.
    :param resources: Requests and limits.
    :param volumes: ``{mount path: volume}``; a :class:`Mount` adds options.
    :param pull_policy: ``imagePullPolicy``.

    Invariants: env values never hold secret versions (use a :class:`Secret`
    and ``secret.key(...)``); mount paths are absolute.
    """

    name: Name
    image: str = Field(min_length=1, pattern=r"^\S+$")
    command: tuple[str, ...] | None = None
    args: tuple[str, ...] | None = None
    working_dir: AbsolutePath | None = None
    env: dict[EnvName, EnvValue] = Field(default_factory=dict)
    ports: tuple[PortNumber | ContainerPort, ...] = ()
    ready: Probe | None = None
    live: Probe | None = None
    startup: Probe | None = None
    resources: Resources | None = None
    volumes: dict[AbsolutePath, Volume | Mount] = Field(default_factory=dict)
    pull_policy: Literal["Always", "IfNotPresent", "Never"] | None = None

    @field_validator("env", mode="before")
    @classmethod
    def _no_plain_secret(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if isinstance(item, SecretVersionRef):
                    raise ValueError(
                        f"env {key!r}: a secret version cannot be an env value; "
                        "declare app.secret(name, {key: ctx.secret(...)}) and use "
                        "secret.key(key)"
                    )
        return value

    @property
    def has_probes(self) -> bool:
        return any(probe is not None for probe in (self.ready, self.live, self.startup))

    def mounts(self) -> list[tuple[str, Volume, dict[str, Any]]]:
        """``(volume name, volume, volumeMount)`` in declaration order."""
        result = []
        for path, item in self.volumes.items():
            mount = item if isinstance(item, Mount) else None
            volume = item.volume if isinstance(item, Mount) else item
            name = volume.volume_name(path)
            read_only = (isinstance(volume, ExistingClaim) and volume.read_only) or (
                mount is not None and mount.read_only
            )
            result.append(
                (
                    name,
                    volume,
                    _compact(
                        name=name,
                        mountPath=path,
                        readOnly=True if read_only else None,
                        subPath=mount.sub_path if mount else None,
                    ),
                )
            )
        return result

    def references(self) -> set[tuple[str, str]]:
        """``("ConfigMap"|"Secret", name)`` pairs this container reads."""
        refs: set[tuple[str, str]] = set()
        for value in self.env.values():
            if isinstance(value, SecretKey):
                refs.add(("Secret", value.secret))
            elif isinstance(value, ConfigKey):
                refs.add(("ConfigMap", value.config))
        for _, volume, _ in self.mounts():
            if isinstance(volume, SecretVolume):
                refs.add(("Secret", volume.secret))
            elif isinstance(volume, ConfigVolume):
                refs.add(("ConfigMap", volume.config))
        return refs

    def manifest(self) -> dict[str, Any]:
        env = [
            {"name": key, "value": value}
            if isinstance(value, str)
            else {"name": key, "valueFrom": value.manifest()}
            for key, value in self.env.items()
        ]
        ports = [
            {"containerPort": port} if isinstance(port, int) else port.manifest()
            for port in self.ports
        ]
        resources = self.resources.manifest() if self.resources else None
        return _compact(
            name=self.name,
            image=self.image,
            imagePullPolicy=self.pull_policy,
            command=list(self.command) if self.command is not None else None,
            args=list(self.args) if self.args is not None else None,
            workingDir=self.working_dir,
            ports=ports or None,
            env=env or None,
            resources=resources or None,
            readinessProbe=self.ready.manifest() if self.ready else None,
            livenessProbe=self.live.manifest() if self.live else None,
            startupProbe=self.startup.manifest() if self.startup else None,
            volumeMounts=[mount for _, _, mount in self.mounts()] or None,
        )


# ------------------------------------------------------------------- workloads


class Deployment(_Model):
    """A Deployment declared with ``app.deployment(...)``.

    :param name: Deployment name; also the default selector.
    :param containers: The main container first, then sidecars.
    :param init_containers: Run to completion, in order, before the others.
    :param replicas: Desired pods.
    :param selector: Explicit selector labels (see the selector rule below).
    :param labels: Extra labels on the Deployment and its pods.
    :param node: Alias of a verified target node (``[target.nodes.<alias>]``)
        to pin the pods to, resolved when the app is rendered.
    :param share_process_namespace: Containers see each other's processes.
    :param strategy: ``RollingUpdate`` or ``Recreate``.
    :param service_account: ``serviceAccountName``.
    :param component: Deployment component; defaults to ``name``.
    :param access: A loopback forward to the pods (``app.access.forward``);
        never rendered into the manifest.

    Selector rule: the selector is ``{"app.kubernetes.io/name": <name>}`` unless
    ``selector`` is given. It never depends on the app name, the component or
    other labels, so renaming those cannot change the selector of an existing
    Deployment (which the API server would reject as immutable). Changing it
    means creating a Deployment with a new name.
    """

    name: Name
    containers: tuple[Container, ...] = Field(min_length=1)
    init_containers: tuple[Container, ...] = ()
    replicas: NonNegativeInt = 1
    selector: Labels | None = Field(default=None, min_length=1)
    labels: Labels = Field(default_factory=dict)
    node: str | None = Field(default=None, min_length=1)
    share_process_namespace: bool = False
    strategy: Literal["RollingUpdate", "Recreate"] | None = None
    service_account: Name | None = None
    component: Name | None = None
    access: Forward | None = None

    @model_validator(mode="after")
    def _pod(self) -> Deployment:
        names = [item.name for item in (*self.init_containers, *self.containers)]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"container names must be unique in a pod: {duplicates}")
        for item in self.init_containers:
            if item.has_probes:
                raise ValueError(
                    f"init container {item.name!r} cannot have probes; it runs "
                    "to completion before the other containers start"
                )
        for key, value in self.selector_labels.items():
            if key in self.labels and self.labels[key] != value:
                raise ValueError(
                    f"label {key!r} conflicts with the selector label {value!r}"
                )
        self.volumes()  # conflicting volume definitions fail at declaration
        if self.access is not None:
            self.access_port()
        return self

    def access_port(self) -> int:
        """The container port the ``access`` forward reaches (main container).

        :raises ValueError: when there is no access declaration, the main
            container has no ports, or the named port does not exist.
        """
        if self.access is None:
            raise ValueError(f"deployment {self.name!r} declares no access")
        ports = [
            item if isinstance(item, ContainerPort) else ContainerPort(port=item)
            for item in self.containers[0].ports
        ]
        return _access_port(
            f"deployment {self.name!r}",
            self.access.port,
            [(item.port, item.name) for item in ports],
        )

    @property
    def selector_labels(self) -> dict[str, str]:
        """The immutable pod selector (see the selector rule)."""
        return dict(self.selector or {"app.kubernetes.io/name": self.name})

    @property
    def component_name(self) -> str:
        return self.component or self.name

    @property
    def all_containers(self) -> tuple[Container, ...]:
        return (*self.init_containers, *self.containers)

    def volumes(self) -> dict[str, Volume]:
        """Pod volumes by name, shared by every container that mounts them."""
        result: dict[str, Volume] = {}
        for container in self.all_containers:
            for name, volume, _ in container.mounts():
                existing = result.setdefault(name, volume)
                if existing.source() != volume.source():
                    raise ValueError(
                        f"volume name {name!r} is used for two different volumes; "
                        "give one of them another name="
                    )
        return result

    def existing_claims(self) -> set[str]:
        return {
            volume.claim
            for volume in self.volumes().values()
            if isinstance(volume, ExistingClaim)
        }

    def references(self) -> set[tuple[str, str]]:
        return {ref for item in self.all_containers for ref in item.references()}

    def manifest(
        self,
        namespace: str,
        app_labels: Mapping[str, str],
        node_name: str | None,
    ) -> dict[str, Any]:
        labels = {**app_labels, **self.labels, **self.selector_labels}
        pod = _compact(
            shareProcessNamespace=True if self.share_process_namespace else None,
            serviceAccountName=self.service_account,
            nodeSelector={"kubernetes.io/hostname": node_name} if node_name else None,
            initContainers=[item.manifest() for item in self.init_containers] or None,
            containers=[item.manifest() for item in self.containers],
            volumes=[
                {"name": name, **volume.source()}
                for name, volume in self.volumes().items()
            ]
            or None,
        )
        spec = _compact(
            replicas=self.replicas,
            strategy={"type": self.strategy} if self.strategy else None,
            selector={"matchLabels": self.selector_labels},
            template={"metadata": {"labels": labels}, "spec": pod},
        )
        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": self.name, "namespace": namespace, "labels": labels},
            "spec": spec,
        }


def _access_port(
    what: str, wanted: int | str | None, ports: Sequence[tuple[int, str | None]]
) -> int:
    if not ports:
        raise ValueError(f"{what} has no port to forward to; declare ports=")
    if wanted is None:
        return ports[0][0]
    for number, name in ports:
        if wanted in (number, name):
            return number
    raise ValueError(
        f"{what}: access port {wanted!r} is not one of its ports "
        f"{[name or number for number, name in ports]}"
    )


class ServicePort(_Model):
    """One Service port. ``target_port`` defaults to ``port``."""

    port: PortNumber
    target_port: PortNumber | PortName | None = None
    name: PortName | None = None
    protocol: Protocol = "TCP"

    def manifest(self) -> dict[str, Any]:
        return _compact(
            name=self.name,
            port=self.port,
            targetPort=self.target_port if self.target_port is not None else self.port,
            protocol=self.protocol,
        )


class Service(_Model):
    """A Service in front of a Deployment, declared with ``app.service(...)``.

    It selects the Deployment's immutable selector labels. Several ports need
    names. ``access`` (``app.access.forward``) declares how to reach it from a
    laptop; it is never rendered into the manifest.
    """

    name: Name
    selector: Labels = Field(min_length=1)
    ports: tuple[ServicePort, ...] = Field(min_length=1)
    type: Literal["ClusterIP", "NodePort", "LoadBalancer"] | None = None
    component: Name
    access: Forward | None = None

    @model_validator(mode="after")
    def _names(self) -> Service:
        if len(self.ports) > 1 and any(port.name is None for port in self.ports):
            raise ValueError("a Service with several ports needs a name on each")
        if self.access is not None:
            self.access_port()
        return self

    def access_port(self) -> int:
        """The Service port the ``access`` forward reaches.

        :raises ValueError: when there is no access declaration or the port
            does not exist on the Service.
        """
        if self.access is None:
            raise ValueError(f"service {self.name!r} declares no access")
        return _access_port(
            f"service {self.name!r}",
            self.access.port,
            [(item.port, item.name) for item in self.ports],
        )

    @property
    def component_name(self) -> str:
        return self.component

    def manifest(self, namespace: str, labels: Mapping[str, str]) -> dict[str, Any]:
        return {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": _compact(
                name=self.name, namespace=namespace, labels=dict(labels) or None
            ),
            "spec": _compact(
                type=self.type,
                selector=self.selector,
                ports=[port.manifest() for port in self.ports],
            ),
        }


class NetworkPolicy(_Model):
    """Ingress rules for a Deployment's pods, declared with ``app.network_policy``.

    With no ``allow_from`` and no ``ports`` all ingress is denied. With
    ``allow_from``, only those Deployments' pods may connect (on ``ports``, or
    any port). With only ``ports``, any source may connect on those ports.
    """

    name: Name
    pod_selector: Labels = Field(min_length=1)
    allow_from: tuple[Labels, ...] = ()
    ports: tuple[PortNumber, ...] = ()
    component: Name

    @property
    def component_name(self) -> str:
        return self.component

    def manifest(self, namespace: str, labels: Mapping[str, str]) -> dict[str, Any]:
        rules: list[dict[str, Any]] = []
        if self.allow_from or self.ports:
            rules.append(
                _compact(
                    **{
                        "from": [
                            {"podSelector": {"matchLabels": peer}}
                            for peer in self.allow_from
                        ]
                        or None
                    },
                    ports=[{"port": port, "protocol": "TCP"} for port in self.ports]
                    or None,
                )
            )
        return {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": _compact(
                name=self.name, namespace=namespace, labels=dict(labels) or None
            ),
            "spec": {
                "podSelector": {"matchLabels": self.pod_selector},
                "policyTypes": ["Ingress"],
                "ingress": rules,
            },
        }
