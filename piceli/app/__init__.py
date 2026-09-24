"""Describe an application in typed Python; render it to resource intents.

Maturity: **preview** (the API may change before 1.0).

``App`` collects Deployments, Services, ConfigMaps, Secrets and NetworkPolicies
declared with typed models and renders them to the ``ResourceIntent`` objects
of a ``DeploymentComposition``, which ``piceli release`` plans and applies.
Services may also declare how to reach them from a laptop
(``app.access.forward``), which ``piceli access`` and ``piceli status`` use.
See ``docs/typed_apps.md`` and ``docs/access.md``.
"""

from piceli.app.access import Access, Forward
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
from piceli.k8s.ui_config import HealthProbe

__all__ = [
    "Access",
    "App",
    "Config",
    "ConfigKey",
    "ConfigVolume",
    "Container",
    "ContainerPort",
    "Deployment",
    "ExistingClaim",
    "FieldRef",
    "Forward",
    "HealthProbe",
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
