from enum import Enum


class SecretType(Enum):
    """Type of Secrets"""

    GENERIC = "generic"
    DOCKER_JSON = "kubernetes.io/dockerconfigjson"
    DOCKER_REGISTRY = "docker-registry"
    OPAQUE = "Opaque"
