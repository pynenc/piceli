"""Consumer-supplied presentation config for the local operator/observe dashboard.

The library ships no application-specific shortcuts or topology.  A consumer
describes its own one-click port-forward shortcuts, topology tiers, header
badges, and extra "managed" label selectors in a TOML file passed with
``--ui-config PATH`` (or the ``PICELI__UI_CONFIG`` environment variable)::

    topology_subtitle = "My application tiers"

    [[shortcuts]]
    id = "web"
    label = "Web UI"
    target = "service/web"
    local_port = 3000
    remote_port = 3000
    path = "/login"

    [[tiers]]
    name = "Application"
    components = ["web", { name = "worker", role = "Task worker" }]

    [inventory]
    managed_labels = { "app.kubernetes.io/part-of" = "my-app" }
    revision_label = "my-app/revision"

With no config the dashboard shows no shortcuts and groups live workloads by
their ``app.kubernetes.io/component`` label (falling back to ``app``).

The same file is an *access profile*: ``piceli observe forwards apply --profile``
starts and supervises every declared shortcut.  A shortcut may declare a
connection health probe and a bounded restart policy::

    [shortcuts.health]
    type = "http"             # or "tcp"
    path = "/healthz"
    expect_status = [200, 399]
    interval = 5.0
    timeout = 2.0
    failure_threshold = 3

    [shortcuts.restart]
    backoff_initial = 1.0
    backoff_max = 30.0
    max_restarts = 10
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

UI_CONFIG_ENV = "PICELI__UI_CONFIG"

_NAME = re.compile(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?")
_TARGET = re.compile(r"(?:service|pod|deployment)/[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?")
_SAFE_PATH = re.compile(r"/[A-Za-z0-9._~!$&'()*+,;=:@%/?#-]*")


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "tier"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HealthProbe(_Model):
    """How the supervisor checks that a loopback forward still reaches its upstream.

    ``tcp`` connects to the local port and then watches the connection for
    ``settle`` seconds: a port forward whose upstream is gone accepts the local
    connection and immediately closes or resets it, which counts as a failure.
    ``http`` sends ``GET path`` and expects a status inside ``expect_status``
    (inclusive).  A probe must fail ``failure_threshold`` consecutive times
    before the forward is restarted; failures during ``startup_grace`` seconds
    after a (re)start do not count.
    """

    type: Literal["tcp", "http"] = "tcp"
    path: str = "/"
    expect_status: tuple[int, int] = (200, 399)
    interval: float = Field(default=5.0, gt=0, le=3600)
    timeout: float = Field(default=2.0, gt=0, le=60)
    failure_threshold: int = Field(default=3, ge=1, le=100)
    startup_grace: float = Field(default=3.0, ge=0, le=600)
    settle: float = Field(default=0.3, ge=0, le=10)

    @field_validator("path")
    @classmethod
    def _valid_path(cls, value: str) -> str:
        if not _SAFE_PATH.fullmatch(value):
            raise ValueError("health probe path must be a safe absolute URL path")
        return value

    @field_validator("expect_status")
    @classmethod
    def _valid_status(cls, value: tuple[int, int]) -> tuple[int, int]:
        low, high = value
        if not 100 <= low <= high <= 599:
            raise ValueError("expect_status must be [low, high] within 100..599")
        return value

    def public_dict(self) -> dict[str, Any]:
        """Return the probe description shown in status JSON."""
        value: dict[str, Any] = {
            "type": self.type,
            "interval": self.interval,
            "timeout": self.timeout,
            "failure_threshold": self.failure_threshold,
        }
        if self.type == "http":
            value["path"] = self.path
            value["expect_status"] = list(self.expect_status)
        return value


class RestartPolicy(_Model):
    """Bounded exponential backoff for restarting a failed or unhealthy forward.

    ``max_restarts`` counts consecutive restarts without an intervening healthy
    probe; once exceeded the forward is marked ``failed`` and left stopped
    until it is started again explicitly.
    """

    backoff_initial: float = Field(default=1.0, gt=0, le=600)
    backoff_max: float = Field(default=30.0, gt=0, le=3600)
    max_restarts: int = Field(default=10, ge=0, le=10_000)

    @model_validator(mode="after")
    def _ordered(self) -> RestartPolicy:
        if self.backoff_max < self.backoff_initial:
            raise ValueError("backoff_max must be >= backoff_initial")
        return self

    def delay(self, attempt: int) -> float:
        """Backoff before restart number ``attempt`` (1-based)."""
        exponent = max(0, min(attempt - 1, 30))
        return float(min(self.backoff_max, self.backoff_initial * 2**exponent))


def legacy_health(health_path: str | None) -> HealthProbe:
    """Probe equivalent to the pre-profile ``health_path`` behaviour.

    Without a path it is a TCP probe.  With one, any status below 500 counts as
    healthy: authentication challenges and redirects still prove the forward
    reaches a live upstream.
    """
    if health_path is None:
        return HealthProbe()
    return HealthProbe(type="http", path=health_path, expect_status=(200, 499))


class UiShortcut(_Model):
    """One-click loopback port forward shown in the dashboard quick-launch bar.

    ``health`` and ``restart`` make it an access-profile entry supervised with
    connection health.  ``health_path`` is kept as shorthand for an HTTP probe
    that accepts any status below 500; it cannot be combined with ``health``.
    ``required = false`` lets ``forwards apply`` skip a shortcut whose local
    port is already served by another process instead of refusing to start.
    """

    id: str
    label: str
    description: str = ""
    target: str
    namespace: str | None = None
    local_port: int = Field(ge=1, le=65535)
    remote_port: int = Field(ge=1, le=65535)
    path: str = "/"
    health_path: str | None = None
    health: HealthProbe | None = None
    restart: RestartPolicy = Field(default_factory=RestartPolicy)
    required: bool = True

    @model_validator(mode="after")
    def _single_health(self) -> UiShortcut:
        if self.health is not None and self.health_path is not None:
            raise ValueError("use either health_path or a [health] table, not both")
        return self

    @property
    def probe(self) -> HealthProbe:
        """The effective health probe for this shortcut."""
        return self.health or legacy_health(self.health_path)

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        if not _NAME.fullmatch(value):
            raise ValueError("shortcut id must be a DNS-1123 style name")
        return value

    @field_validator("target")
    @classmethod
    def _valid_target(cls, value: str) -> str:
        if not _TARGET.fullmatch(value):
            raise ValueError(
                "shortcut target must be service/NAME, deployment/NAME or pod/NAME"
            )
        return value

    @field_validator("namespace")
    @classmethod
    def _valid_namespace(cls, value: str | None) -> str | None:
        if value is not None and not _NAME.fullmatch(value):
            raise ValueError("shortcut namespace must be a DNS-1123 name")
        return value

    @field_validator("path", "health_path")
    @classmethod
    def _valid_path(cls, value: str | None) -> str | None:
        if value is not None and not _SAFE_PATH.fullmatch(value):
            raise ValueError("shortcut paths must be safe absolute URL paths")
        return value

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.local_port}{self.path}"


class UiComponent(_Model):
    """One workload card inside a topology tier."""

    name: str
    role: str = ""
    description: str = ""
    ports: str = ""
    shortcut: str | None = None


class UiTier(_Model):
    """A named group of components in the topology view."""

    name: str
    id: str = ""
    badge: str = ""
    components: tuple[UiComponent, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            data["components"] = tuple(
                {"name": item} if isinstance(item, str) else item
                for item in data.get("components", ())
            )
            if not data.get("id") and isinstance(data.get("name"), str):
                data["id"] = _slug(data["name"])
        return data


class UiBadge(_Model):
    """A short informational pill shown in the dashboard header."""

    label: str
    text: str


class InventoryConfig(_Model):
    """Extra label selectors for classifying live objects in operator reports."""

    managed_labels: dict[str, str] = Field(default_factory=dict)
    revision_label: str | None = None


class UiConfig(_Model):
    """Typed dashboard configuration; an empty instance is a valid default."""

    topology_subtitle: str = ""
    badges: tuple[UiBadge, ...] = ()
    shortcuts: tuple[UiShortcut, ...] = ()
    tiers: tuple[UiTier, ...] = ()
    inventory: InventoryConfig = Field(default_factory=InventoryConfig)

    @model_validator(mode="after")
    def _consistent(self) -> UiConfig:
        ids = [shortcut.id for shortcut in self.shortcuts]
        if len(ids) != len(set(ids)):
            raise ValueError("shortcut ids must be unique")
        for tier in self.tiers:
            for component in tier.components:
                if component.shortcut is not None and component.shortcut not in ids:
                    raise ValueError(
                        f"component {component.name!r} references unknown shortcut "
                        f"{component.shortcut!r}"
                    )
        return self

    def shortcut(self, shortcut_id: str) -> UiShortcut | None:
        return next((item for item in self.shortcuts if item.id == shortcut_id), None)

    def public_dict(self) -> dict[str, Any]:
        """Return the JSON the dashboard page needs (no inventory selectors)."""
        return {
            "topology_subtitle": self.topology_subtitle,
            "badges": [badge.model_dump() for badge in self.badges],
            "tiers": [tier.model_dump() for tier in self.tiers],
        }


def load_access_profile(path: Path) -> UiConfig:
    """Load an access profile; it is the same TOML format as ``--ui-config``."""
    return load_ui_config(path)


def load_ui_config(path: Path | None = None) -> UiConfig:
    """Load ``path`` (or ``$PICELI__UI_CONFIG``); return an empty config when unset."""
    if path is None:
        env_value = os.environ.get(UI_CONFIG_ENV)
        path = Path(env_value) if env_value else None
    if path is None:
        return UiConfig()
    with path.open("rb") as handle:
        return UiConfig.model_validate(tomllib.load(handle))
