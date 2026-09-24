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
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

UI_CONFIG_ENV = "PICELI__UI_CONFIG"

_NAME = re.compile(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?")
_TARGET = re.compile(r"(?:service|pod)/[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?")
_SAFE_PATH = re.compile(r"/[A-Za-z0-9._~!$&'()*+,;=:@%/?#-]*")


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "tier"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class UiShortcut(_Model):
    """One-click loopback port forward shown in the dashboard quick-launch bar."""

    id: str
    label: str
    description: str = ""
    target: str
    namespace: str | None = None
    local_port: int = Field(ge=1, le=65535)
    remote_port: int = Field(ge=1, le=65535)
    path: str = "/"
    health_path: str | None = None

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
            raise ValueError("shortcut target must be service/NAME or pod/NAME")
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


def load_ui_config(path: Path | None = None) -> UiConfig:
    """Load ``path`` (or ``$PICELI__UI_CONFIG``); return an empty config when unset."""
    if path is None:
        env_value = os.environ.get(UI_CONFIG_ENV)
        path = Path(env_value) if env_value else None
    if path is None:
        return UiConfig()
    with path.open("rb") as handle:
        return UiConfig.model_validate(tomllib.load(handle))
