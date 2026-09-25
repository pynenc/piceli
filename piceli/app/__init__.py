"""Describe an application in typed Python; render it to resource intents.

Maturity: **preview** (the API may change before 1.0).

``App`` collects Deployments, StatefulSets, DaemonSets, Jobs, CronJobs,
Services, ConfigMaps, Secrets, ServiceAccounts (with their RBAC rules),
NetworkPolicies, HorizontalPodAutoscalers, PodDisruptionBudgets, Ingresses,
Gateway API HTTPRoutes and objects of any other kind (``app.resource``, custom
resources included) declared with typed models, applies an ``Environment``'s
overrides, and renders them to the ``ResourceIntent`` objects
of a ``DeploymentComposition``, which ``piceli release`` plans and applies.
Services may also declare how to reach them from a laptop
(``app.access.forward``), which ``piceli access`` and ``piceli status`` use.
See ``docs/typed_apps.md`` and ``docs/access.md``.
"""

from piceli.app.access import Access, Forward
from piceli.app.app import App
from piceli.app.environment import Environment
from piceli.app.kinds import (
    Autoscaler,
    CronJob,
    DaemonSet,
    DisruptionBudget,
    GatewayRef,
    HttpRoute,
    Ingress,
    Job,
    Route,
    StatefulSet,
)
from piceli.app.model import (
    ClaimTemplate,
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
    Workload,
)
from piceli.app.resource import Resource
from piceli.k8s.ui_config import HealthProbe

__all__ = [
    "Access",
    "App",
    "Autoscaler",
    "ClaimTemplate",
    "Config",
    "ConfigKey",
    "ConfigVolume",
    "Container",
    "ContainerPort",
    "CronJob",
    "DaemonSet",
    "Deployment",
    "DisruptionBudget",
    "Environment",
    "ExistingClaim",
    "FieldRef",
    "Forward",
    "GatewayRef",
    "HealthProbe",
    "HttpRoute",
    "Ingress",
    "Job",
    "MemoryVolume",
    "Mount",
    "NetworkPolicy",
    "PodDefaults",
    "Probe",
    "Resource",
    "Resources",
    "Route",
    "Rule",
    "Secret",
    "SecretKey",
    "SecretVolume",
    "Security",
    "Service",
    "ServiceAccount",
    "ServicePort",
    "StatefulSet",
    "Workload",
]
