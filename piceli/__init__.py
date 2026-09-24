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
        "Static",
        "Target",
        "Template",
        "TlsCa",
    }
)

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
        ExistingClaim,
        FieldRef,
        Forward,
        HealthProbe,
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
    from piceli.pipeline import (  # noqa: F401
        Build,
        NodeImport,
        NodeLoopbackRegistry,
        Pipeline,
        Random,
        Registry,
        Secrets,
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
    raise AttributeError(f"module 'piceli' has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted([*globals(), *_APP_EXPORTS, *_PIPELINE_EXPORTS])
