import json
from dataclasses import dataclass
from enum import Enum

from kubernetes.client.exceptions import ApiException


class ReasonEnum(Enum):
    AlreadyExists = "AlreadyExists"
    NotFound = "NotFound"
    Unknown = None


@dataclass
class ApiOperationException(Exception):
    code: int
    status: str
    reason: str
    message: str
    details: dict
    ex: ApiException
    body: dict

    @classmethod
    def from_api_exception(cls, ex: ApiException) -> "ApiOperationException":
        body = json.loads(ex.body)
        return cls(
            code=body.get("code", ex.status),
            status=body.get("status", ""),
            reason=body.get("reason", ex.reason),
            message=body.get("message", ""),
            details=body.get("details", {}),
            ex=ex,
            body=body,
        )

    @property
    def not_found(self) -> bool:
        return self.reason == ReasonEnum.NotFound.value

    @property
    def already_exists(self) -> bool:
        return self.reason == ReasonEnum.AlreadyExists.value

    @property
    def is_being_deleted(self) -> bool:
        return "object is being deleted" in self.message

    @property
    def is_immutable_field_error(self) -> bool:
        """Check if the error is due to an attempt to change immutable fields."""
        return "Forbidden:" in self.message and "is immutable" in self.message

    def immutable_fields(self) -> list[str]:
        """Extract the fields involved in the immutable error."""
        if not self.is_immutable_field_error:
            return []
        fields = []
        # The field-related information is usually in the `causes` array within `details`
        for cause in self.details.get("causes", []):
            field_path = cause.get("field", "")
            if field_path:
                fields.append(field_path)
        return fields
