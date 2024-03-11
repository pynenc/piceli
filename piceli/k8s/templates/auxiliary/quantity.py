from kubernetes.utils.quantity import parse_quantity
from pydantic import AfterValidator
from typing_extensions import Annotated


def check_quantity(v: str) -> str:
    """Validates that the string is a valid k8s quantity"""
    try:
        parse_quantity(v)
        return v
    except ValueError as ex:
        raise ValueError(f"{v} is not a valid k8s quantity") from ex


Quantity = Annotated[str, AfterValidator(check_quantity)]
