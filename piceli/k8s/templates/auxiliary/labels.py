import re

from pydantic import AfterValidator
from typing_extensions import Annotated

CLUSTER_LABELS: set[str] = {"team", "component", "state"}


def validate_cluster_label(key: str, value: str) -> None:
    """
    Validates the format of a cluster label according to GCP's label guidelines.
    https://cloud.google.com/kubernetes-engine/docs/how-to/creating-managing-labels

    :param str key: The key of the label being validated.
    :param str value: The value of the label being validated.
    :raises ValueError: If the value does not conform to the expected format of GCP cluster labels.
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
    """
    Validates the format of a standard Kubernetes label according to Kubernetes label syntax requirements.

    https://kubernetes.io/docs/concepts/overview/working-with-objects/labels/

    :param str key: The key of the label being validated.
    :param str value: The value of the label being validated.
    :raises ValueError: If either the key or value does not conform to the expected format for Kubernetes labels.
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
    """
    Validates a dictionary of labels, ensuring each key-value pair meets Kubernetes or GCP cluster label standards.

    This function distinguishes between standard Kubernetes labels and specific GCP cluster labels, applying the correct validation rules accordingly.

    :param dict[str, str] v: The dictionary of label key-value pairs to be validated.
    :return: The original dictionary if all labels are valid.
    :raises ValueError: If any label key or value does not meet the validation criteria.
    """
    for key, value in v.items():
        if key in CLUSTER_LABELS:
            validate_cluster_label(key, value)
        else:
            validate_label(key, value)
    return v


Labels = Annotated[dict[str, str], AfterValidator(check_labels)]
