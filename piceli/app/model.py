"""Typed building blocks of an :class:`~piceli.app.App`.

Every class here is a frozen pydantic model, so a wrong value fails when it is
declared, with a pydantic ``ValidationError`` that names the field. Nothing in
this module contacts a cluster, and importing it does not import the
``kubernetes`` client.

The models render to plain manifest fragments; :class:`~piceli.app.App` turns
them into ``ResourceIntent`` objects.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, ClassVar, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
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

    Growing or moving the claim is the one exception, and only with
    ``Pipeline(restore_points=...)``: ``expand_to`` asks the deploy's
    ``backup`` stage to expand the claim in place (its storage class must
    allow volume expansion), and ``migrate_from`` asks it to create this
    claim with ``size`` and copy the data of another claim into it before
    the release mounts it (the old claim is kept). Both are planned, shown and approved with the deploy's combined
    hash; neither renders anything. See ``docs/restore_points.md``.

    :param claim: Name of the existing PersistentVolumeClaim.
    :param name: Volume name; defaults to the claim name.
    :param read_only: Mount read-only.
    :param expand_to: The size the existing claim should grow to, such as
        ``"8Gi"`` (never shrinks it).
    :param size: With ``migrate_from``: the size of the claim Piceli creates.
    :param storage_class: With ``migrate_from``: the new claim's
        ``storageClassName`` (default: the old claim's).
    :param migrate_from: An existing claim whose data moves into ``claim``
        (created by the deploy, with ``size``) before the release switches
        the workload to it.

    Example::

        volumes={"/data": ExistingClaim("cache-state")}
        volumes={"/data": ExistingClaim("cache-state-2", size="8Gi",
                                        migrate_from="cache-state")}
    """

    kind: Literal["existing-claim"] = "existing-claim"
    claim: ObjectName
    read_only: bool = False
    expand_to: Quantity | None = None
    size: Quantity | None = None
    storage_class: ObjectName | None = None
    migrate_from: ObjectName | None = None

    def __init__(self, claim: str, /, **data: Any) -> None:
        super().__init__(**{"claim": claim, **data})

    @model_validator(mode="after")
    def _growth(self) -> ExistingClaim:
        if self.migrate_from is not None:
            if self.size is None:
                raise ValueError("migrate_from needs size= for the new claim")
            if self.migrate_from == self.claim:
                raise ValueError("migrate_from names another claim than claim")
            if self.read_only:
                raise ValueError("a claim migrated into is mounted writably")
            if self.expand_to is not None:
                raise ValueError("give expand_to= or migrate_from=, not both")
        elif self.size is not None:
            raise ValueError(
                "an existing claim has no size= (Piceli does not create it); "
                "use expand_to= to grow it, or migrate_from= with size= to "
                "move its data into a new claim"
            )
        elif self.storage_class is not None:
            raise ValueError(
                "storage_class= only goes with migrate_from= (a claim's class "
                "cannot change in place)"
            )
        return self

    def growth(self) -> dict[str, Any] | None:
        """The declared size or migration (``None`` when neither is set)."""
        size = self.size if self.migrate_from is not None else self.expand_to
        if size is None:
            return None
        return {
            "claim": self.claim,
            "size": str(size),
            **({"storage_class": self.storage_class} if self.storage_class else {}),
            **({"migrate_from": self.migrate_from} if self.migrate_from else {}),
        }

    def _default_name(self, mount_path: str) -> str:
        return self.claim.replace(".", "-")[:63]

    def source(self) -> dict[str, Any]:
        return {
            "persistentVolumeClaim": _compact(
                claimName=self.claim, readOnly=True if self.read_only else None
            )
        }


AccessMode = Literal[
    "ReadWriteOnce", "ReadOnlyMany", "ReadWriteMany", "ReadWriteOncePod"
]


class ClaimTemplate(_Volume):
    """A per-pod PersistentVolumeClaim of a StatefulSet (``volumeClaimTemplates``).

    Mount it like any volume on ``app.stateful_set(...)``; each pod gets its
    own claim, named ``<template>-<stateful set>-<ordinal>`` by Kubernetes.

    Data safety: the claims are created by the StatefulSet controller, not by
    the release, so no plan creates, changes, adopts or prunes them. The
    StatefulSet renders ``persistentVolumeClaimRetentionPolicy`` ``Retain``
    for deletion and scale-down, and a release deletes or replaces a
    StatefulSet with ``Orphan`` propagation: the claims and their data outlive
    it. A template is immutable once the StatefulSet exists (changing it
    needs ``replace=["StatefulSet/<name>"]``, which deletes and recreates the
    StatefulSet object only, pods and claims orphaned and kept). Without
    ``restore_points`` existing claims keep their size; with
    ``Pipeline(restore_points=...)`` a larger ``size`` grows every existing
    claim of the template (when its storage class allows expansion), and
    ``migrate_from`` copies each replica's claim of an older template into
    this template's new claim. See ``docs/restore_points.md``.

    :param name: Template (and volume) name.
    :param size: Requested storage, such as ``"1Gi"``.
    :param storage_class: ``storageClassName``; the cluster default when unset.
    :param access_modes: Access modes; ``ReadWriteOnce`` by default.
    :param read_only: Mount read-only.
    :param migrate_from: The template this one replaces: each existing
        ``<migrate_from>-<stateful set>-<ordinal>`` claim is copied into a new
        ``<name>-<stateful set>-<ordinal>`` claim before the release switches
        the StatefulSet to this template (the old claims are kept).

    Example::

        volumes={"/var/lib/db": ClaimTemplate("data", size="1Gi")}
        volumes={"/var/lib/db": ClaimTemplate("data-2", size="8Gi", migrate_from="data")}
    """

    kind: Literal["claim-template"] = "claim-template"
    name: Name
    size: Quantity
    storage_class: ObjectName | None = None
    access_modes: tuple[AccessMode, ...] = Field(
        default=("ReadWriteOnce",), min_length=1
    )
    read_only: bool = False
    migrate_from: Name | None = None

    def __init__(self, name: str, /, **data: Any) -> None:
        super().__init__(**{"name": name, **data})

    @model_validator(mode="after")
    def _growth(self) -> ClaimTemplate:
        if self.migrate_from is not None:
            if self.migrate_from == self.name:
                raise ValueError("migrate_from names another template than name")
            if self.read_only:
                raise ValueError("a claim template migrated into is mounted writably")
        return self

    def growth(self) -> dict[str, Any] | None:
        """The declared migration (``None`` when there is none)."""
        if self.migrate_from is None:
            return None
        return {"template": self.name, "migrate_from": self.migrate_from}

    def source(self) -> dict[str, Any]:
        raise ValueError(
            f"claim template {self.name!r} is not a pod volume; it renders under "
            "the StatefulSet's volumeClaimTemplates"
        )

    def template(self) -> dict[str, Any]:
        """The ``volumeClaimTemplates`` item."""
        return {
            "metadata": {"name": self.name},
            "spec": _compact(
                accessModes=list(self.access_modes),
                storageClassName=self.storage_class,
                resources={"requests": {"storage": self.size}},
            ),
        }


Volume = ConfigVolume | SecretVolume | MemoryVolume | ExistingClaim | ClaimTemplate


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
            read_only = (
                isinstance(volume, ExistingClaim | ClaimTemplate) and volume.read_only
            ) or (mount is not None and mount.read_only)
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


# -------------------------------------------------------------------- security

#: The node label a ``node=`` pin renders; see :class:`PodDefaults`.
HOSTNAME_LABEL = "kubernetes.io/hostname"


def _capability(value: str) -> str:
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", value):
        raise ValueError(
            f"capability {value!r}: capabilities are upper-case names such as "
            "'ALL' or 'NET_BIND_SERVICE'"
        )
    return value


Capability = Annotated[str, AfterValidator(_capability)]


class Security(_Model):
    """Pod and container security settings of a workload.

    Pod-level fields render to the pod's ``securityContext``; container-level
    fields render to the ``securityContext`` of **every** container of the pod
    (init containers and sidecars included). Unset (``None``) fields are not
    rendered, and when settings are layered (``PodDefaults(security=...)``
    under a workload's ``security=``) each set field of the upper layer wins
    field by field.

    Pod level:

    :param run_as_non_root: Refuse to start a container that would run as UID 0.
    :param run_as_user: UID of every container process.
    :param run_as_group: Primary GID of every container process.
    :param fs_group: GID that owns mounted volumes.
    :param seccomp: Seccomp profile type: ``RuntimeDefault``, ``Unconfined``
        or ``Localhost`` (with ``seccomp_localhost_profile``).
    :param seccomp_localhost_profile: Profile path for ``seccomp="Localhost"``.

    Container level:

    :param allow_privilege_escalation: ``False`` blocks setuid binaries and
        similar privilege gains.
    :param read_only_root_filesystem: Mount the image's root filesystem
        read-only (mount a volume where the process must write).
    :param drop_capabilities: Linux capabilities to drop, such as ``("ALL",)``.
        An empty tuple drops nothing (it clears an inherited default).
    :param add_capabilities: Capabilities to add back, such as
        ``("NET_BIND_SERVICE",)``.

    Invariants: ``run_as_non_root=True`` cannot be combined with
    ``run_as_user=0``; a ``Localhost`` seccomp profile needs its path, and
    only ``Localhost`` takes one.

    Example::

        Security.restricted(user=10001, fs_group=10001)
        Security(run_as_user=2000, read_only_root_filesystem=True)
    """

    run_as_non_root: bool | None = None
    run_as_user: NonNegativeInt | None = None
    run_as_group: NonNegativeInt | None = None
    fs_group: NonNegativeInt | None = None
    seccomp: Literal["RuntimeDefault", "Unconfined", "Localhost"] | None = None
    seccomp_localhost_profile: str | None = Field(default=None, min_length=1)
    allow_privilege_escalation: bool | None = None
    read_only_root_filesystem: bool | None = None
    drop_capabilities: tuple[Capability, ...] | None = None
    add_capabilities: tuple[Capability, ...] | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Security:
        if self.run_as_non_root and self.run_as_user == 0:
            raise ValueError("run_as_non_root=True cannot run as run_as_user=0")
        if self.seccomp == "Localhost" and not self.seccomp_localhost_profile:
            raise ValueError(
                "seccomp='Localhost' needs seccomp_localhost_profile (a profile path)"
            )
        if self.seccomp_localhost_profile and self.seccomp != "Localhost":
            raise ValueError("seccomp_localhost_profile needs seccomp='Localhost'")
        return self

    @classmethod
    def restricted(
        cls,
        *,
        user: int = 10001,
        group: int | None = None,
        fs_group: int | None = None,
        read_only_root_filesystem: bool | None = None,
    ) -> Security:
        """Settings that pass the Kubernetes ``restricted`` Pod Security Standard.

        Non-root ``user`` (and ``group``, defaulting to ``user``), the
        runtime's default seccomp profile, no privilege escalation and every
        capability dropped.
        """
        return cls(
            run_as_non_root=True,
            run_as_user=user,
            run_as_group=user if group is None else group,
            fs_group=fs_group,
            seccomp="RuntimeDefault",
            allow_privilege_escalation=False,
            read_only_root_filesystem=read_only_root_filesystem,
            drop_capabilities=("ALL",),
        )

    @staticmethod
    def layered(base: Security | None, top: Security | None) -> Security | None:
        """``top`` over ``base``, field by field (unset fields inherit)."""
        if base is None or top is None:
            return top if base is None else base
        return Security.model_validate(
            {**base.model_dump(exclude_none=True), **top.model_dump(exclude_none=True)}
        )

    def pod_manifest(self) -> dict[str, Any]:
        seccomp = (
            _compact(type=self.seccomp, localhostProfile=self.seccomp_localhost_profile)
            if self.seccomp
            else None
        )
        return _compact(
            runAsNonRoot=self.run_as_non_root,
            runAsUser=self.run_as_user,
            runAsGroup=self.run_as_group,
            fsGroup=self.fs_group,
            seccompProfile=seccomp,
        )

    def container_manifest(self) -> dict[str, Any]:
        capabilities = _compact(
            add=list(self.add_capabilities) if self.add_capabilities else None,
            drop=list(self.drop_capabilities) if self.drop_capabilities else None,
        )
        return _compact(
            allowPrivilegeEscalation=self.allow_privilege_escalation,
            readOnlyRootFilesystem=self.read_only_root_filesystem,
            capabilities=capabilities or None,
        )


class PodDefaults(_Model):
    """Pod settings an :class:`~piceli.app.App` applies to every workload it declares.

    Pass it as ``App(..., pod_defaults=PodDefaults(...))``. Every workload
    declared on the app (Deployment, StatefulSet, DaemonSet, Job, CronJob)
    gets these settings; a workload's own typed argument
    wins (``security=`` field by field, ``node_selector=`` key by key,
    ``termination_grace_seconds=`` and ``automount_token=`` as a whole), and
    :meth:`App.override <piceli.app.App.override>` still patches the rendered
    manifest afterwards. Components added with ``app.add(...)`` are not
    changed. With no defaults, rendering is unchanged.

    :param security: Pod and container security settings (:class:`Security`).
    :param node_selector: Extra node labels every pod must match, such as
        ``{"kubernetes.io/arch": "amd64"}``. Merged with a workload's
        ``node=`` pin (which renders ``kubernetes.io/hostname``).
    :param termination_grace_seconds: ``terminationGracePeriodSeconds``.
    :param automount_token: ``automountServiceAccountToken`` for pods that
        are not bound to a service account declared with
        ``app.service_account`` (those get a token; see
        :meth:`App.service_account <piceli.app.App.service_account>`).
        ``False`` keeps API credentials out of every other pod.

    Invariants: a ``node_selector`` that sets ``kubernetes.io/hostname`` is
    refused on a workload that also has a ``node=`` pin (the two would
    disagree or repeat each other).

    Example::

        App("shop", pod_defaults=PodDefaults(
            security=Security.restricted(user=10001, fs_group=10001),
            node_selector={"kubernetes.io/arch": "amd64"},
            termination_grace_seconds=30,
        ))
    """

    security: Security | None = None
    node_selector: Labels | None = None
    termination_grace_seconds: NonNegativeInt | None = None
    automount_token: bool | None = None


# ------------------------------------------------------------------- workloads


class Workload(_Model):
    """Pod settings shared by every pod-bearing kind of an :class:`~piceli.app.App`.

    :class:`Deployment` and the kinds in :mod:`piceli.app.kinds`
    (``StatefulSet``, ``DaemonSet``, ``Job``, ``CronJob``) derive from it, so
    ``pod_defaults``, ``service_account=``, node pins, images, secrets and
    volumes behave the same for all of them.

    :param name: Object name; also the default selector.
    :param containers: The main container first, then sidecars.
    :param init_containers: Run to completion, in order, before the others.
    :param selector: Explicit selector labels (see the selector rule below).
    :param labels: Extra labels on the object and its pods.
    :param node: Alias of a verified target node (``[target.nodes.<alias>]``)
        to pin the pods to, resolved when the app is rendered.
    :param share_process_namespace: Containers see each other's processes.
    :param service_account: ``serviceAccountName``.
    :param security: Pod and container security settings; layered field by
        field over the app's ``PodDefaults.security``.
    :param node_selector: Extra node labels the pods must match; merged key by
        key over the app's ``PodDefaults.node_selector`` and with ``node``.
    :param termination_grace_seconds: ``terminationGracePeriodSeconds``.
    :param automount_token: ``automountServiceAccountToken`` of the pods. Unset,
        a pod bound to a service account declared on the app gets ``True``;
        any other pod gets the app's ``PodDefaults.automount_token``.
    :param component: Deployment component; defaults to ``name``.

    Selector rule: the selector is ``{"app.kubernetes.io/name": <name>}`` unless
    ``selector`` is given. It never depends on the app name, the component or
    other labels, so renaming those cannot change the selector of an existing
    workload (which the API server would reject as immutable). Changing it
    means creating a workload with a new name.
    """

    #: The Kubernetes kind this model renders.
    kind: ClassVar[str] = ""
    #: How messages name the kind (``"deployment 'api'"``).
    label: ClassVar[str] = "workload"

    name: Name
    containers: tuple[Container, ...] = Field(min_length=1)
    init_containers: tuple[Container, ...] = ()
    selector: Labels | None = Field(default=None, min_length=1)
    labels: Labels = Field(default_factory=dict)
    node: str | None = Field(default=None, min_length=1)
    share_process_namespace: bool = False
    service_account: ObjectName | None = None
    security: Security | None = None
    node_selector: Labels | None = None
    termination_grace_seconds: NonNegativeInt | None = None
    automount_token: bool | None = None
    component: Name | None = None

    @model_validator(mode="after")
    def _pod(self) -> Workload:
        self.check_defaults(None)
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
        if self.claim_templates() and not self.accepts_claim_templates():
            raise ValueError(
                f"{self.label} {self.name!r}: ClaimTemplate volumes are per-pod "
                "claims of a StatefulSet; declare it with app.stateful_set(...), "
                "or mount an ExistingClaim"
            )
        return self

    @classmethod
    def accepts_claim_templates(cls) -> bool:
        """Whether ``ClaimTemplate`` volumes may be mounted (StatefulSet only)."""
        return False

    def node_labels(self, defaults: PodDefaults | None) -> dict[str, str]:
        """The extra node selector: the app's defaults, then this workload's."""
        return {
            **((defaults.node_selector or {}) if defaults else {}),
            **(self.node_selector or {}),
        }

    def check_defaults(self, defaults: PodDefaults | None) -> None:
        """Refuse settings that conflict once ``defaults`` are applied.

        :raises ValueError: when a ``kubernetes.io/hostname`` node selector
            meets a ``node=`` pin.
        """
        if self.node is not None and HOSTNAME_LABEL in self.node_labels(defaults):
            source = (
                "its node_selector"
                if HOSTNAME_LABEL in (self.node_selector or {})
                else "the app's pod_defaults.node_selector"
            )
            raise ValueError(
                f"{self.label} {self.name!r}: {source} sets {HOSTNAME_LABEL}, which "
                f"conflicts with node={self.node!r}; pin with node= or select the "
                "host by label, not both"
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
        """Pod volumes by name, shared by every container that mounts them.

        ``ClaimTemplate`` mounts are not pod volumes (see :meth:`claim_templates`).
        """
        result: dict[str, Volume] = {}
        for container in self.all_containers:
            for name, volume, _ in container.mounts():
                if isinstance(volume, ClaimTemplate):
                    continue
                existing = result.setdefault(name, volume)
                if existing.source() != volume.source():
                    raise ValueError(
                        f"volume name {name!r} is used for two different volumes; "
                        "give one of them another name="
                    )
        return result

    def claim_templates(self) -> dict[str, ClaimTemplate]:
        """Per-pod claim templates by name, in first-mount order."""
        result: dict[str, ClaimTemplate] = {}
        pod_volumes = {
            name
            for container in self.all_containers
            for name, volume, _ in container.mounts()
            if not isinstance(volume, ClaimTemplate)
        }
        for container in self.all_containers:
            for name, volume, _ in container.mounts():
                if not isinstance(volume, ClaimTemplate):
                    continue
                if name in pod_volumes:
                    raise ValueError(
                        f"volume name {name!r} is used for a ClaimTemplate and "
                        "another volume; give one of them another name"
                    )
                existing = result.setdefault(name, volume)
                if existing != volume:
                    raise ValueError(
                        f"claim template {name!r} is declared twice with different "
                        "settings; mount the same ClaimTemplate object"
                    )
        return result

    def existing_claims(self) -> set[str]:
        return {
            volume.claim
            for volume in self.volumes().values()
            if isinstance(volume, ExistingClaim)
        }

    def references(self) -> set[tuple[str, str]]:
        """``(kind, name)`` of the configs, secrets and service account it uses."""
        refs = {ref for item in self.all_containers for ref in item.references()}
        if self.service_account is not None:
            refs.add(("ServiceAccount", self.service_account))
        return refs

    def pod_labels(self, app_labels: Mapping[str, str]) -> dict[str, str]:
        """Labels of the object and its pods: app, workload, then selector."""
        return {**app_labels, **self.labels, **self.selector_labels}

    def pod_spec(
        self,
        node_name: str | None,
        defaults: PodDefaults | None = None,
        automount_token: bool | None = None,
        *,
        restart_policy: str | None = None,
    ) -> dict[str, Any]:
        """The pod spec, with the app's ``defaults`` under this workload's fields.

        :param automount_token: ``automountServiceAccountToken`` when this
            workload sets none (the app decides it; see ``automount_token``).
        """
        self.check_defaults(defaults)
        security = Security.layered(
            defaults.security if defaults else None, self.security
        )
        container_security = security.container_manifest() if security else {}

        def container(item: Container) -> dict[str, Any]:
            rendered = item.manifest()
            if container_security:
                rendered["securityContext"] = dict(container_security)
            return rendered

        node_selector = self.node_labels(defaults)
        if node_name:
            node_selector[HOSTNAME_LABEL] = node_name
        grace = (
            self.termination_grace_seconds
            if self.termination_grace_seconds is not None
            else defaults.termination_grace_seconds
            if defaults
            else None
        )
        automount = (
            self.automount_token
            if self.automount_token is not None
            else automount_token
        )
        return _compact(
            shareProcessNamespace=True if self.share_process_namespace else None,
            serviceAccountName=self.service_account,
            automountServiceAccountToken=automount,
            securityContext=(security.pod_manifest() or None) if security else None,
            terminationGracePeriodSeconds=grace,
            restartPolicy=restart_policy,
            nodeSelector=node_selector or None,
            initContainers=[container(item) for item in self.init_containers] or None,
            containers=[container(item) for item in self.containers],
            volumes=[
                {"name": name, **volume.source()}
                for name, volume in self.volumes().items()
            ]
            or None,
        )

    def manifest(
        self,
        namespace: str,
        app_labels: Mapping[str, str],
        node_name: str | None,
        defaults: PodDefaults | None = None,
        automount_token: bool | None = None,
        *,
        scaled: int | None = None,
    ) -> dict[str, Any]:
        """The rendered object (see each kind).

        :param scaled: The ``min_replicas`` of the autoscaler that owns
            ``spec.replicas``: rendered as the initial size instead of
            ``replicas`` (see :class:`~piceli.app.kinds.Autoscaler`).
        """
        raise NotImplementedError


class Deployment(Workload):
    """A Deployment declared with ``app.deployment(...)``.

    Pod fields are those of :class:`Workload`; in addition:

    :param replicas: Desired pods. When an autoscaler targets the Deployment
        (``app.autoscaler``) it owns the count: ``replicas=`` is refused and
        the autoscaler's ``min_replicas`` is rendered as the initial size.
    :param strategy: ``RollingUpdate`` or ``Recreate``.
    :param access: A loopback forward to the pods (``app.access.forward``);
        never rendered into the manifest.
    """

    kind: ClassVar[str] = "Deployment"
    label: ClassVar[str] = "deployment"

    replicas: NonNegativeInt = 1
    strategy: Literal["RollingUpdate", "Recreate"] | None = None
    access: Forward | None = None

    @model_validator(mode="after")
    def _access(self) -> Deployment:
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

    def manifest(
        self,
        namespace: str,
        app_labels: Mapping[str, str],
        node_name: str | None,
        defaults: PodDefaults | None = None,
        automount_token: bool | None = None,
        *,
        scaled: int | None = None,
    ) -> dict[str, Any]:
        """The Deployment manifest.

        :param defaults: The app's pod defaults, under this workload's fields.
        :param automount_token: ``automountServiceAccountToken`` when this
            workload sets none (the app decides it; see ``automount_token``).
        :param scaled: The ``min_replicas`` of the autoscaler that owns
            ``spec.replicas``: rendered as the initial size instead of
            ``replicas`` (see :class:`~piceli.app.kinds.Autoscaler`).
        """
        labels = self.pod_labels(app_labels)
        pod = self.pod_spec(node_name, defaults, automount_token)
        spec = _compact(
            replicas=self.replicas if scaled is None else scaled,
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
    """A Service in front of a workload, declared with ``app.service(...)``.

    It selects the workload's immutable selector labels. Several ports need
    names. ``access`` (``app.access.forward``) declares how to reach it from a
    laptop; it is never rendered into the manifest.

    ``headless=True`` renders ``clusterIP: None`` (a DNS name per pod, no
    virtual IP): ``app.stateful_set(...)`` declares one as its governing
    Service. Only a headless Service may have no ports.
    """

    name: Name
    selector: Labels = Field(min_length=1)
    ports: tuple[ServicePort, ...] = ()
    type: Literal["ClusterIP", "NodePort", "LoadBalancer"] | None = None
    headless: bool = False
    component: Name
    access: Forward | None = None

    @model_validator(mode="after")
    def _names(self) -> Service:
        if not self.ports and not self.headless:
            raise ValueError("a Service needs at least one port (unless headless)")
        if self.headless and self.type not in (None, "ClusterIP"):
            raise ValueError("a headless Service is of type ClusterIP")
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
                clusterIP="None" if self.headless else None,
                selector=self.selector,
                ports=[port.manifest() for port in self.ports] or None,
            ),
        }


class NetworkPort(_Model):
    """One port (or ``port`` to ``end_port`` range) and protocol of a rule."""

    port: PortNumber
    protocol: Protocol = "TCP"
    end_port: PortNumber | None = None

    @model_validator(mode="after")
    def _range(self) -> NetworkPort:
        if self.end_port is not None and self.end_port < self.port:
            raise ValueError(
                f"port range {self.port}-{self.end_port}: end_port must not be "
                "below port"
            )
        return self

    def manifest(self) -> dict[str, Any]:
        return _compact(port=self.port, protocol=self.protocol, endPort=self.end_port)


def _cidr(value: str) -> Any:
    try:
        return ipaddress.ip_network(value, strict=True)
    except ValueError:
        raise ValueError(
            f"{value!r} is not a CIDR such as '10.0.0.0/8' (host bits must be zero)"
        ) from None


class NetworkPeer(_Model):
    """Who a rule talks to: pods, namespaces, or an IP range.

    Build one with :meth:`pods`, :meth:`namespace` or :meth:`cidr`. Pod and
    namespace selectors must have labels: an empty selector would match
    everything. A peer with both pod and namespace labels matches the pods
    with those labels *in* the matching namespaces.
    """

    pod_labels: Labels | None = None
    namespace_labels: Labels | None = None
    cidr_block: str | None = None
    cidr_except: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _shape(self) -> NetworkPeer:
        selectors = [
            ("pod_labels", self.pod_labels),
            ("namespace_labels", self.namespace_labels),
        ]
        for field, labels in selectors:
            if labels is not None and not labels:
                raise ValueError(
                    f"{field} needs labels; an empty selector would match every "
                    "pod or namespace"
                )
        if self.cidr_block is not None:
            if self.pod_labels or self.namespace_labels:
                raise ValueError("a CIDR peer cannot also select pods or namespaces")
            network = _cidr(self.cidr_block)
            for item in self.cidr_except:
                inner = _cidr(item)
                if inner.version != network.version or not inner.subnet_of(network):
                    raise ValueError(
                        f"except {item!r} must be a smaller range inside "
                        f"{self.cidr_block!r}"
                    )
                if inner == network:
                    raise ValueError(f"except {item!r} excludes the whole range")
        elif self.cidr_except:
            raise ValueError("except needs a CIDR")
        elif self.pod_labels is None and self.namespace_labels is None:
            raise ValueError("a peer needs pod labels, namespace labels or a CIDR")
        return self

    @classmethod
    def pods(cls, labels: Mapping[str, str]) -> NetworkPeer:
        """Pods of the policy's own namespace with these labels."""
        return cls(pod_labels=dict(labels))

    @classmethod
    def workload(cls, workload: Workload) -> NetworkPeer:
        """The pods of a workload (its selector), in the policy's namespace."""
        return cls(pod_labels=workload.selector_labels)

    @classmethod
    def namespace(
        cls,
        name: str | None = None,
        *,
        labels: Mapping[str, str] | None = None,
        pods: Mapping[str, str] | None = None,
    ) -> NetworkPeer:
        """Namespaces by name (``kubernetes.io/metadata.name``) or by ``labels``.

        With ``pods``, only those pods of the matching namespaces.
        """
        if (name is None) == (labels is None):
            raise ValueError("pass either a namespace name or labels=")
        selected = (
            {"kubernetes.io/metadata.name": name}
            if name is not None
            else dict(labels or {})
        )
        return cls(
            namespace_labels=selected,
            pod_labels=dict(pods) if pods is not None else None,
        )

    @classmethod
    def cidr(cls, block: str, *, except_: Sequence[str] = ()) -> NetworkPeer:
        """An IP range, minus the ``except_`` sub-ranges."""
        return cls(cidr_block=block, cidr_except=tuple(except_))

    def manifest(self) -> dict[str, Any]:
        if self.cidr_block is not None:
            return {
                "ipBlock": _compact(
                    cidr=self.cidr_block, **{"except": list(self.cidr_except) or None}
                )
            }
        return _compact(
            namespaceSelector=(
                {"matchLabels": self.namespace_labels}
                if self.namespace_labels
                else None
            ),
            podSelector=({"matchLabels": self.pod_labels} if self.pod_labels else None),
        )


def _ports(value: Any) -> Any:
    return tuple(
        {"port": item} if isinstance(item, int) and not isinstance(item, bool) else item
        for item in (value or ())
    )


class NetworkRule(_Model):
    """One allowed set of ``peers`` (ingress sources or egress destinations) on ``ports``.

    ``ports`` take plain numbers (TCP) or :class:`NetworkPort` (protocol, range).
    Peers without ports allow every port; ports without peers allow every
    peer. A rule with neither is refused: name ``NetworkPeer.cidr("0.0.0.0/0")``
    to mean everything.
    """

    peers: tuple[NetworkPeer, ...] = ()
    ports: Annotated[tuple[NetworkPort, ...], BeforeValidator(_ports)] = ()

    @model_validator(mode="after")
    def _not_open(self) -> NetworkRule:
        if not self.peers and not self.ports:
            raise ValueError(
                "a network rule needs peers or ports; to allow everything say so "
                "with NetworkPeer.cidr('0.0.0.0/0')"
            )
        return self

    @classmethod
    def dns(cls) -> NetworkRule:
        """Egress to the cluster DNS (``kube-dns`` in ``kube-system``) on port 53.

        A policy that restricts egress blocks name resolution unless it
        allows DNS; add this rule to it.
        """
        return cls(
            peers=(NetworkPeer.namespace("kube-system", pods={"k8s-app": "kube-dns"}),),
            ports=(NetworkPort(port=53, protocol="UDP"), NetworkPort(port=53)),
        )

    def manifest(self, direction: str) -> dict[str, Any]:
        return _compact(
            **{
                direction: [peer.manifest() for peer in self.peers] or None,
                "ports": [port.manifest() for port in self.ports] or None,
            }
        )


PolicyType = Literal["Ingress", "Egress"]


class NetworkPolicy(_Model):
    """Ingress and egress rules for selected pods, declared with ``app.network_policy``.

    ``pod_selector`` picks the protected pods (one Deployment's selector, or
    any labels, such as ``app.release_selector`` for every pod of the app).
    With no ``allow_from`` and no ``ports`` all ingress is denied. With
    ``allow_from`` (pod label sets in the same namespace), only matching pods
    may connect (on ``ports``, or any port). With only ``ports``, any source
    may connect on those ports. ``ingress`` adds typed rules
    (:class:`NetworkRule`: namespaces, CIDRs, protocols).

    ``egress`` rules restrict what the pods may connect to; once a pod is
    selected by an Egress policy, everything else is denied, DNS included
    (add :meth:`NetworkRule.dns`). ``policy_types`` is derived when empty:
    ``Ingress``, plus ``Egress`` when egress rules exist; a policy that
    declares egress rules and no ingress arguments is ``Egress`` only.
    Declare ``policy_types=("Egress",)`` with no rules to deny all egress.
    """

    name: Name
    pod_selector: Labels = Field(min_length=1)
    allow_from: tuple[Labels, ...] = ()
    ports: tuple[PortNumber, ...] = ()
    ingress: tuple[NetworkRule, ...] = ()
    egress: tuple[NetworkRule, ...] = ()
    policy_types: tuple[PolicyType, ...] = ()
    component: Name

    @model_validator(mode="after")
    def _types(self) -> NetworkPolicy:
        if len(set(self.policy_types)) != len(self.policy_types):
            raise ValueError("policy_types lists a type twice")
        if self.policy_types:
            if self.egress and "Egress" not in self.policy_types:
                raise ValueError("egress rules need 'Egress' in policy_types")
            if self._ingress_declared() and "Ingress" not in self.policy_types:
                raise ValueError("ingress rules need 'Ingress' in policy_types")
        return self

    def _ingress_declared(self) -> bool:
        return bool(self.allow_from or self.ports or self.ingress)

    @property
    def effective_policy_types(self) -> list[str]:
        if self.policy_types:
            return [kind for kind in ("Ingress", "Egress") if kind in self.policy_types]
        if self.egress and not self._ingress_declared():
            return ["Egress"]
        return ["Ingress", "Egress"] if self.egress else ["Ingress"]

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
        rules.extend(rule.manifest("from") for rule in self.ingress)
        types = self.effective_policy_types
        spec: dict[str, Any] = {
            "podSelector": {"matchLabels": self.pod_selector},
            "policyTypes": types,
        }
        if "Ingress" in types:
            spec["ingress"] = rules
        if "Egress" in types:
            spec["egress"] = [rule.manifest("to") for rule in self.egress]
        return {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": _compact(
                name=self.name, namespace=namespace, labels=dict(labels) or None
            ),
            "spec": spec,
        }


# ------------------------------------------------------------------------ rbac

_RBAC = "rbac.authorization.k8s.io"
_VERB = re.compile(r"[a-z][a-zA-Z]*")
_RESOURCE = re.compile(r"[a-z][a-z0-9.-]*(/[a-z][a-z0-9-]*)?")
# Kubernetes cannot restrict these verbs to named objects.
_UNNAMEABLE_VERBS = frozenset({"create", "deletecollection"})


class Rule(_Model):
    """One RBAC permission: ``verbs`` on ``resources`` of ``api_groups``.

    Used by :meth:`App.service_account <piceli.app.App.service_account>` as a
    namespaced rule (a Role) or a cluster rule (a ClusterRole).

    :param resources: Resource plurals, such as ``("pods",)`` or
        ``("pods/log",)`` for a subresource.
    :param verbs: Verbs, such as ``("get", "list", "watch")``.
    :param api_groups: API groups; ``""`` is the core group (the default).
    :param resource_names: Only these object names.
    :param allow_wildcard: Required to use ``"*"`` in any list: a wildcard
        grants everything, including verbs and resources added later.

    Invariants: ``resources``, ``verbs`` and ``api_groups`` are non-empty;
    ``"*"`` needs ``allow_wildcard=True``; ``resource_names`` cannot be
    combined with ``create`` or ``deletecollection``, which Kubernetes cannot
    restrict by name.

    Example::

        Rule(resources=["pods"], verbs=["get", "list", "watch"])
        Rule(api_groups=["apps"], resources=["deployments"], verbs=["get"],
             resource_names=["api"])
    """

    resources: tuple[str, ...] = Field(min_length=1)
    verbs: tuple[str, ...] = Field(min_length=1)
    api_groups: tuple[str, ...] = Field(default=("",), min_length=1)
    resource_names: tuple[str, ...] = ()
    allow_wildcard: bool = False

    @model_validator(mode="after")
    def _shape(self) -> Rule:
        wildcards = [
            field
            for field, values in (
                ("api_groups", self.api_groups),
                ("resources", self.resources),
                ("verbs", self.verbs),
                ("resource_names", self.resource_names),
            )
            if "*" in values
        ]
        if wildcards and not self.allow_wildcard:
            raise ValueError(
                f"'*' in {', '.join(wildcards)} grants everything, including "
                "what Kubernetes adds later; list what the workload needs, or "
                "pass allow_wildcard=True to mean it"
            )
        for verb in self.verbs:
            if verb != "*" and not _VERB.fullmatch(verb):
                raise ValueError(f"verb {verb!r}: verbs are words such as 'get'")
        for resource in self.resources:
            if resource != "*" and not _RESOURCE.fullmatch(resource):
                raise ValueError(
                    f"resource {resource!r}: resources are lower-case plurals such "
                    "as 'pods' or 'pods/log'"
                )
        for group in self.api_groups:
            if group not in {"", "*"} and not _DNS_SUBDOMAIN.fullmatch(group):
                raise ValueError(
                    f"api group {group!r}: use '' for the core group or a group "
                    "name such as 'apps'"
                )
        for name in self.resource_names:
            if not name or name in {".", ".."} or any(c in name for c in "/%"):
                raise ValueError(f"resource name {name!r} is not a valid object name")
        if self.resource_names and _UNNAMEABLE_VERBS & set(self.verbs):
            raise ValueError(
                "resource_names cannot restrict 'create' or 'deletecollection'; "
                "put those verbs in a rule without resource_names"
            )
        return self

    def manifest(self) -> dict[str, Any]:
        return _compact(
            apiGroups=list(self.api_groups),
            resources=list(self.resources),
            verbs=list(self.verbs),
            resourceNames=list(self.resource_names) or None,
        )


class ServiceAccount(_Model):
    """A service account with its permissions, declared with ``app.service_account``.

    Renders a ServiceAccount, and a Role and RoleBinding (named after it) when
    it has ``rules``, and a ClusterRole and ClusterRoleBinding when it has
    ``cluster_rules``. Cluster-scoped objects are shared by the whole cluster,
    so they are named ``<namespace>:<app>:<name>`` and annotated with
    ``piceli.io/namespace``: releases in two namespaces never collide, and a
    release manages, changes and prunes only its own.

    The ServiceAccount renders ``automountServiceAccountToken: false``, so a
    pod gets its token only when it is bound with ``service_account=`` on a
    workload of the app (which renders ``true`` on the pod).

    :param name: ServiceAccount name (also of the Role and RoleBinding).
    :param rules: Namespaced permissions (:class:`Rule`), in the release
        namespace.
    :param cluster_rules: Cluster-wide permissions (:class:`Rule`), such as
        reading nodes.
    :param component: Deployment component; defaults to ``name``.
    """

    name: Name
    rules: tuple[Rule, ...] = ()
    cluster_rules: tuple[Rule, ...] = ()
    component: Name | None = None

    @property
    def component_name(self) -> str:
        return self.component or self.name

    def cluster_name(self, namespace: str, app: str) -> str:
        """The ClusterRole and ClusterRoleBinding name in ``namespace``."""
        return f"{namespace}:{app}:{self.name}"

    def manifests(
        self,
        namespace: str,
        app: str,
        labels: Mapping[str, str],
        namespace_annotation: str,
    ) -> list[dict[str, Any]]:
        """ServiceAccount first, then the Role pair and the ClusterRole pair."""

        def metadata(name: str, *, cluster: bool = False) -> dict[str, Any]:
            return _compact(
                name=name,
                namespace=None if cluster else namespace,
                labels=dict(labels) or None,
                annotations={namespace_annotation: namespace} if cluster else None,
            )

        subject = {"kind": "ServiceAccount", "name": self.name, "namespace": namespace}
        result: list[dict[str, Any]] = [
            {
                "apiVersion": "v1",
                "kind": "ServiceAccount",
                "metadata": metadata(self.name),
                "automountServiceAccountToken": False,
            }
        ]
        pairs = (
            (self.rules, "Role", "RoleBinding", self.name, False),
            (
                self.cluster_rules,
                "ClusterRole",
                "ClusterRoleBinding",
                self.cluster_name(namespace, app),
                True,
            ),
        )
        for rules, role, binding, name, cluster in pairs:
            if not rules:
                continue
            result.append(
                {
                    "apiVersion": f"{_RBAC}/v1",
                    "kind": role,
                    "metadata": metadata(name, cluster=cluster),
                    "rules": [rule.manifest() for rule in rules],
                }
            )
            result.append(
                {
                    "apiVersion": f"{_RBAC}/v1",
                    "kind": binding,
                    "metadata": metadata(name, cluster=cluster),
                    "roleRef": {"apiGroup": _RBAC, "kind": role, "name": name},
                    "subjects": [subject],
                }
            )
        return result
