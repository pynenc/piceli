"""Describe an application in typed Python; render it to resource intents.

Maturity: **preview** (the API may change before 1.0).

``App`` collects Deployments, Services, ConfigMaps, Secrets and NetworkPolicies
declared with typed models and renders them to the ``ResourceIntent`` objects
of a ``DeploymentComposition``, which ``piceli release`` plans and applies.
See ``docs/typed_apps.md``.
"""

from piceli.app.app import App
from piceli.app.model import (
    Config,
    ConfigKey,
    ConfigVolume,
    Container,
    ContainerPort,
    Deployment,
    ExistingClaim,
    FieldRef,
    MemoryVolume,
    Mount,
    NetworkPolicy,
    Probe,
    Resources,
    Secret,
    SecretKey,
    SecretVolume,
    Service,
    ServicePort,
)

__all__ = [
    "App",
    "Config",
    "ConfigKey",
    "ConfigVolume",
    "Container",
    "ContainerPort",
    "Deployment",
    "ExistingClaim",
    "FieldRef",
    "MemoryVolume",
    "Mount",
    "NetworkPolicy",
    "Probe",
    "Resources",
    "Secret",
    "SecretKey",
    "SecretVolume",
    "Service",
    "ServicePort",
]
