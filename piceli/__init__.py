from importlib.metadata import version
from typing import TYPE_CHECKING, Any

__version__ = version("piceli")

# The typed app API is exported lazily: ``import piceli`` stays cheap and has no
# side effects; ``from piceli import App`` imports ``piceli.app`` on first use.
_APP_EXPORTS = frozenset(
    {
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
        "Scaling",
        "Secret",
        "SecretKey",
        "SecretVolume",
        "Security",
        "Service",
        "ServiceAccount",
        "ServicePort",
        "StatefulSet",
        "Workload",
    }
)

# The pipeline API (``piceli deploy``) is exported lazily the same way.
_PIPELINE_EXPORTS = frozenset(
    {
        "AwsSecret",
        "Build",
        "NodeImport",
        "NodeLoopbackRegistry",
        "Pipeline",
        "Random",
        "Registry",
        "Secrets",
        "Smoke",
        "Sops",
        "Static",
        "Target",
        "Template",
        "TlsCa",
        "Vault",
    }
)

# Post-deploy check declarations for ``Pipeline(checks=...)``.  Only the
# builder is exported: ``piceli.checks.CheckContext`` differs from
# ``piceli.pipeline.CheckContext``, so the runner API stays in its package.
_CHECKS_EXPORTS = frozenset({"Checks"})

if TYPE_CHECKING:
    from piceli.app import (  # noqa: F401
        Access,
        App,
        Autoscaler,
        ClaimTemplate,
        Config,
        ConfigKey,
        ConfigVolume,
        Container,
        ContainerPort,
        CronJob,
        DaemonSet,
        Deployment,
        DisruptionBudget,
        Environment,
        ExistingClaim,
        FieldRef,
        Forward,
        GatewayRef,
        HealthProbe,
        HttpRoute,
        Ingress,
        Job,
        MemoryVolume,
        Mount,
        NetworkPolicy,
        PodDefaults,
        Probe,
        Resource,
        Resources,
        Route,
        Rule,
        Scaling,
        Secret,
        SecretKey,
        SecretVolume,
        Security,
        Service,
        ServiceAccount,
        ServicePort,
        StatefulSet,
        Workload,
    )
    from piceli.checks import Checks  # noqa: F401
    from piceli.pipeline import (  # noqa: F401
        AwsSecret,
        Build,
        NodeImport,
        NodeLoopbackRegistry,
        Pipeline,
        Random,
        Registry,
        Secrets,
        Smoke,
        Sops,
        Static,
        Target,
        Template,
        TlsCa,
        Vault,
    )


def __getattr__(name: str) -> Any:
    if name in _APP_EXPORTS:
        from piceli import app

        return getattr(app, name)
    if name in _PIPELINE_EXPORTS:
        from piceli import pipeline

        return getattr(pipeline, name)
    if name in _CHECKS_EXPORTS:
        from piceli import checks

        return getattr(checks, name)
    raise AttributeError(f"module 'piceli' has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted([*globals(), *_APP_EXPORTS, *_PIPELINE_EXPORTS, *_CHECKS_EXPORTS])
