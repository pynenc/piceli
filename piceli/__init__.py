from importlib.metadata import version
from typing import TYPE_CHECKING, Any

__version__ = version("piceli")

# The typed app API is exported lazily: ``import piceli`` stays cheap and has no
# side effects; ``from piceli import App`` imports ``piceli.app`` on first use.
_APP_EXPORTS = frozenset(
    {
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
    }
)

# The pipeline API (``piceli deploy``) is exported lazily the same way.
_PIPELINE_EXPORTS = frozenset(
    {
        "Build",
        "NodeImport",
        "NodeLoopbackRegistry",
        "Pipeline",
        "Random",
        "Registry",
        "Secrets",
        "Smoke",
        "Static",
        "Target",
        "Template",
        "TlsCa",
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
        Config,
        ConfigKey,
        ConfigVolume,
        Container,
        ContainerPort,
        Deployment,
        Environment,
        ExistingClaim,
        FieldRef,
        Forward,
        HealthProbe,
        MemoryVolume,
        Mount,
        NetworkPolicy,
        PodDefaults,
        Probe,
        Resource,
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
    from piceli.checks import Checks  # noqa: F401
    from piceli.pipeline import (  # noqa: F401
        Build,
        NodeImport,
        NodeLoopbackRegistry,
        Pipeline,
        Random,
        Registry,
        Secrets,
        Smoke,
        Static,
        Target,
        Template,
        TlsCa,
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
