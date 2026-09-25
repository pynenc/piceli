"""Access declarations: how a person reaches a workload from their laptop.

Maturity: **preview**.

An access declaration is part of the typed model but never part of the
cluster: it renders to no Kubernetes object and does not change any manifest.
``piceli access`` supervises exactly the declared forwards, ``piceli status``
reports their URLs and whether each one is up, and the local dashboard shows
them as its shortcuts. See ``docs/access.md``.

Example::

    app = App("shop")
    web = app.deployment("web", image=ctx.image("web"), ports=[3000])
    app.service(
        web,
        port=3000,
        access=app.access.forward(local=18080, path="/login", health="/healthz"),
    )

Importing this module is side-effect free.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from piceli.k8s.ui_config import HealthProbe, RestartPolicy, UiShortcut

_SAFE_PATH = re.compile(r"/[A-Za-z0-9._~!$&'()*+,;=:@%/?#-]*")
# Kept local (not imported from ``piceli.app.model``, which imports this module).
_Name = Annotated[str, Field(pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", max_length=63)]
_Port = Annotated[int, Field(ge=1, le=65535)]
_PortName = Annotated[
    str, Field(pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", max_length=15)
]


class Forward(BaseModel):
    """A loopback port forward to a Service (or a Deployment's pods).

    Build it with :meth:`Access.forward` (``app.access.forward(...)``) and pass
    it as ``access=`` to ``app.service(...)`` or ``app.deployment(...)``.

    :param local: Port on ``127.0.0.1`` of the laptop.
    :param port: The Service port (number or name) to reach; defaults to the
        first port. For a Deployment it is a container port of the main
        container and defaults to its first port.
    :param path: Path appended to the URL that ``status`` prints.
    :param health: How the supervisor checks the forward: ``None`` or
        ``"tcp"`` for a TCP probe, a path such as ``"/healthz"`` for an HTTP
        probe that expects 200-399, or a full
        :class:`~piceli.k8s.ui_config.HealthProbe`.
    :param name: Forward id (``piceli access --only NAME``); defaults to the
        Service or Deployment name.
    :param label: Human label; defaults to the id.
    :param description: One line shown by the dashboard.
    :param required: ``False`` lets ``piceli access`` skip this forward when its
        local port is taken, instead of refusing to start.
    :param restart: Restart backoff and budget.

    Invariants: ``local`` ports and ids are unique within an app; the target
    port must exist on the Service or container.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    local: _Port
    port: _Port | _PortName | None = None
    path: str = "/"
    health: HealthProbe = Field(default_factory=HealthProbe)
    name: _Name | None = None
    label: str | None = Field(default=None, min_length=1, max_length=80)
    description: str = Field(default="", max_length=200)
    required: bool = True
    restart: RestartPolicy = Field(default_factory=RestartPolicy)

    @field_validator("path")
    @classmethod
    def _safe_path(cls, value: str) -> str:
        if not _SAFE_PATH.fullmatch(value):
            raise ValueError("path must be a safe absolute URL path")
        return value

    def shortcut(
        self, default_id: str, target: str, remote_port: int, namespace: str | None
    ) -> UiShortcut:
        """The access-profile entry that the supervisor and dashboard consume."""
        ident = self.name or default_id
        return UiShortcut(
            id=ident,
            label=self.label or ident,
            description=self.description,
            target=target,
            namespace=namespace,
            local_port=self.local,
            remote_port=remote_port,
            path=self.path,
            health=self.health,
            restart=self.restart,
            required=self.required,
        )


class Access:
    """Access factories, reachable as ``app.access``."""

    @staticmethod
    def forward(
        local: int,
        *,
        port: int | str | None = None,
        path: str = "/",
        health: str | HealthProbe | None = None,
        name: str | None = None,
        label: str | None = None,
        description: str = "",
        required: bool = True,
        restart: RestartPolicy | None = None,
    ) -> Forward:
        """Declare a loopback forward (see :class:`Forward` for the parameters)."""
        probe: HealthProbe
        if health is None or health == "tcp":
            probe = HealthProbe()
        elif isinstance(health, HealthProbe):
            probe = health
        elif isinstance(health, str) and health.startswith("/"):
            probe = HealthProbe(type="http", path=health, expect_status=(200, 399))
        else:
            raise ValueError(
                "health must be None, 'tcp', a path starting '/', or a HealthProbe"
            )
        values: dict[str, Any] = {
            "local": local,
            "port": port,
            "path": path,
            "health": probe,
            "name": name,
            "label": label,
            "description": description,
            "required": required,
        }
        if restart is not None:
            values["restart"] = restart
        return Forward.model_validate(values)
