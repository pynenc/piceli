from enum import Enum


class DeploymentStrategyType(Enum):
    """Strategy of the deployment"""

    RECREATE = "Recreate"
    # Recreate: All existing Pods are killed before new ones are created when

    ROLLING_UPDATE = "RollingUpdate"
    # RollingUpdate: The Deployment updates Pods in a rolling update fashion
    # You can specify maxUnavailable and maxSurge to control the rolling update process.
    # https://kubernetes.io/docs/concepts/workloads/controllers/deployment/
    # so far only one replica, if we have more we need to check this
