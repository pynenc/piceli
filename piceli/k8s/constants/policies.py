from enum import Enum


class RestartPolicy(Enum):
    """Restart policy"""

    NEVER = "Never"
    ALWAYS = "Always"
    ON_FAILURE = "OnFailure"


class ConcurrencyPolicy(Enum):
    """Specifies how to treat concurrent executions of a Job"""

    ALLOW = "Allow"
    # (default): The cron job allows concurrently running jobs

    FORBID = "Forbid"
    # Forbid: The cron job does not allow concurrent runs;
    # if it is time for a new job run and the previous job run hasn't finished yet,
    # the cron job skips the new job run

    REPLACE = "Replace"
    # Replace: If it is time for a new job run and the previous job run hasn't finished yet,
    # the cron job replaces the currently running job run with a new job run


class ImagePullPolicy(Enum):
    """
    Image pull policy

    Defaults to Always if :latest tag is specified, or IfNotPresent otherwise.
    More info: https://kubernetes.io/docs/concepts/containers/images#updating-images
    """

    ALWAYS = "Always"
    IF_NOT_PRESENT = "IfNotPresent"
    NEVER = "Never"
