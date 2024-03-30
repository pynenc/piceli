from kubernetes.utils.quantity import parse_quantity
from pydantic import AfterValidator
from typing_extensions import Annotated


def check_quantity(v: str) -> str:
    """
    Validates that a string is a valid Kubernetes quantity representation.

    Kubernetes quantities can specify memory, CPU, and other resources in various units,
    and this function ensures the format is correct according to Kubernetes parsing logic.

    :param str v: The string representation of the quantity to validate.
    :return: The validated string if it represents a valid Kubernetes quantity.
    :raises ValueError: If the string does not represent a valid Kubernetes quantity.

    This function is crucial for validating resource specifications in Piceli templates,
    ensuring they are correctly interpreted by Kubernetes for scheduling and resource allocation.
    """
    try:
        parse_quantity(v)
        return v
    except ValueError as ex:
        raise ValueError(f"{v} is not a valid k8s quantity") from ex


Quantity = Annotated[str, AfterValidator(check_quantity)]
