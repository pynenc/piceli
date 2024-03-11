from enum import Enum


class Phase(Enum):
    """Phases of k8s objects"""


class PhaseVolume(Phase):
    """Phases of Persistent Volumes and Persistent Volume Claims"""

    # a free resource that is not yet bound to a claim
    AVAILABLE = "Available"

    # the volume is bound to a claim
    BOUND = "Bound"

    # the claim has been deleted, but the resource is not yet reclaimed by the cluster
    RELEASED = "Released"

    # the volume has failed its automatic reclamation
    FAILED = "Failed"


class PhasePVC(Phase):
    """Specific class for Persistent Volume Claims"""

    # this doesn't appear in the kubernetes docs phases for volumes
    PENDING = "Pending"


class PhasePod(Phase):
    """Phases of Pods"""

    # The Pod has been accepted by the Kubernetes cluster,
    # but one or more of the containers has not been set up and made ready to run.
    # This includes time a Pod spends waiting to be scheduled
    # as well as the time spent downloading container images over the network.
    PENDING = "Pending"

    # The Pod has been bound to a node, and all of the containers have been created.
    # At least one container is still running, or is in the process of starting or restarting.
    RUNNING = "Running"

    # All containers in the Pod have terminated in success, and will not be restarted.
    SUCCEEDED = "Succeeded"

    # All containers in the Pod have terminated, and at least one container has terminated in failure.
    # That is, the container either exited with non-zero status or was terminated by the system.
    FAILED = "Failed"

    # For some reason the state of the Pod could not be obtained.
    # This phase typically occurs due to an error in communicating with the node where the Pod should be running.
    UNKNOWN = "Unknown"
