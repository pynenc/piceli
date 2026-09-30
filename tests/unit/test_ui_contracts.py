"""Approval boundary and public-contract compatibility checks."""

import pytest
from pydantic import ValidationError

from piceli.services.contracts import OperationRequest, ResourceIdentity, Target


def test_approval_request_rejects_missing_or_malformed_identity() -> None:
    with pytest.raises(ValidationError):
        OperationRequest(
            plan_id="plan", approved_digest="latest", idempotency_key="once"
        )
    with pytest.raises(ValidationError):
        OperationRequest(plan_id="plan", approved_digest="a" * 64, idempotency_key="")


def test_public_target_cannot_serialize_credential_fields() -> None:
    with pytest.raises(ValidationError):
        Target.model_validate(
            {
                "id": "target",
                "name": "test",
                "namespace": "shop",
                "kubeconfig": "/private/config",
            }
        )


def test_resource_identity_distinguishes_replacement_and_target() -> None:
    resource = ResourceIdentity(
        target_id="one",
        api_version="v1",
        kind="Pod",
        namespace="shop",
        name="api",
        uid="old",
    )
    assert resource != resource.model_copy(update={"uid": "new"})
    assert resource != resource.model_copy(update={"target_id": "two"})
