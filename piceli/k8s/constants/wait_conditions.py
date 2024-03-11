from enum import Enum


class WaitCondition(Enum):
    """Wait conditions for wait_for method"""


class WaitConditionPod(WaitCondition):
    """Wait conditions specific for pods"""

    POD_SCHEDULED = "PodScheduled"
    # the Pod has been scheduled to a node.

    POD_HAS_NETWORK = "PodHasNetwork"
    # (alpha feature; must be enabled explicitly) the Pod sandbox has been successfully created and networking configured.

    CONTAINERS_READY = "ContainersReady"
    # all containers in the Pod are ready.

    INITIALIZED = "Initialized"
    # all init containers have completed successfully.

    READY = "Ready"
    # the Pod is able to serve requests and should be added to the load balancing pools of all matching Services.


class WaitConditionJob(WaitCondition):
    """Wait conditions specific for jobs"""

    COMPLETE = "Complete"
    FAILED = "Failed"
    SUSPENDED = "Suspended"


class WaitConditionDeployment(WaitCondition):
    """Wait conditions specific for deployments"""

    AVAILABLE = "Available"
    # means that your Deployment has minimum availability

    PROGRESSING = "Progressing"
    # Kubernetes marks a Deployment as progressing when one of the following tasks is performed:
    # The Deployment creates a new ReplicaSet.
    # The Deployment is scaling up its newest ReplicaSet.
    # The Deployment is scaling down its older ReplicaSet(s).
    # New Pods become ready or available (ready for at least MinReadySeconds).
