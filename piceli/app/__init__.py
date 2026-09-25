"""Describe an application in typed Python; render it to resource intents.

Maturity: **preview** (the API may change before 1.0).

``App`` collects Deployments, Services, ConfigMaps, Secrets, ServiceAccounts
(with their RBAC rules), NetworkPolicies and objects of any other kind
(``app.resource``, custom resources included) declared with typed models,
applies an ``Environment``'s overrides, and renders them to the ``ResourceIntent`` objects
of a ``DeploymentComposition``, which ``piceli release`` plans and applies.
Services may also declare how to reach them from a laptop
(``app.access.forward``), which ``piceli access`` and ``piceli status`` use.
See ``docs/typed_apps.md`` and ``docs/access.md``.
"""

from piceli.app.access import Access, Forward
from piceli.app.app import App
from piceli.app.environment import Environment
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
    PodDefaults,
    Probe,
    Resources,
    Rule,
    Secret,
    SecretKey,
    SecretVolume,
    Security,
    Service,
    ServiceAccount,
    ServicePort,
)
from piceli.app.resource import Resource
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
    "Environment",
    "ExistingClaim",
    "FieldRef",
    "Forward",
    "HealthProbe",
    "MemoryVolume",
    "Mount",
    "NetworkPolicy",
    "PodDefaults",
    "Probe",
    "Resource",
    "Resources",
    "Rule",
    "Secret",
    "SecretKey",
    "SecretVolume",
    "Security",
    "Service",
    "ServiceAccount",
    "ServicePort",
]
