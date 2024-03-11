import re

from pydantic import AfterValidator
from typing_extensions import Annotated

CLUSTER_LABELS: set[str] = {"team", "component", "state"}


def validate_cluster_label(key: str, value: str) -> None:
    """validate format of cluster label
    https://cloud.google.com/kubernetes-engine/docs/how-to/creating-managing-labels
    """
    values_re = re.compile("^[a-z][-_a-z0-9]*$")
    if not value or not 0 < len(value) < 64:
        raise ValueError(
            f"value {value} for cluster label {key} must be a string between 1 and 63 chars"
        )
    if not bool(values_re.fullmatch(value)):
        raise ValueError(
            f"value {value} for cluster label {key} can contain only 'a-z', '0-9', '_', and '-' and starts only by 'a-z'"
        )


def validate_label(key: str, value: str) -> None:
    """validate format of standard label
    https://kubernetes.io/docs/concepts/overview/working-with-objects/labels/
    """
    keys_re = re.compile("^[a-z0-9A-Z][-_.a-z0-9A-Z]*(?<![-_.])$")
    values_re = re.compile("^[a-z0-9A-Z][-_.a-z0-9A-Z]*$")
    if not key or not 0 < len(key) < 64:
        raise ValueError(f"label {key} must be a string between 1 and 63 chars")
    if not bool(keys_re.fullmatch(key)):
        raise ValueError(
            f"label {key} invalid can contain only 'a-z', 'A-Z', '0-9', '.', '_', and '-' and starts and ends only by 'a-z', 'A-Z', '0-9' "
        )
    if value:
        if len(value) > 63:
            raise ValueError(f"value {value} for label {key} cannot exceed 63 chars")
        if not bool(values_re.fullmatch(value)):
            raise ValueError(
                f"value {value} for label {key} can contain only 'a-z', 'A-Z', '0-9', '.', '_', and '-' and starts only by 'a-z', 'A-Z', '0-9' "
            )


def check_labels(v: dict[str, str]) -> dict[str, str]:
    """Validates that the dictionary contain a valid k8s labels"""
    for key, value in v.items():
        if key in CLUSTER_LABELS:
            validate_cluster_label(key, value)
        else:
            validate_label(key, value)
    return v


Labels = Annotated[dict[str, str], AfterValidator(check_labels)]
