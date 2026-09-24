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


def __getattr__(name: str) -> Any:
    if name in _APP_EXPORTS:
        from piceli import app

        return getattr(app, name)
    raise AttributeError(f"module 'piceli' has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted([*globals(), *_APP_EXPORTS])
