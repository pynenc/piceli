"""Unit: which live Secrets count as this release's, and which values fit."""

from __future__ import annotations

import base64
import secrets
from typing import Any

import pytest

from piceli.k8s.ops.plan import ObservedResource, Ownership
from piceli.k8s.release_secret_spec import (
    RandomSecretSpec,
    SecretError,
    TlsSelfSignedSpec,
)
from piceli.k8s.secret_adoption import _check, owned_by_release

OWNER, MANAGER, NAMESPACE = "shop", "shop", "shop-prod"


def _observed(
    *,
    owner: str | None = OWNER,
    manager: str = MANAGER,
    ownership: Ownership = Ownership.MANAGED,
    annotations: dict[str, str] | None = None,
) -> ObservedResource:
    metadata: dict[str, Any] = {
        "name": "credentials",
        "namespace": NAMESPACE,
        "uid": "u-1",
        "resourceVersion": "1",
        "annotations": dict(annotations or {}),
        "managedFields": [
            {
                "manager": manager,
                "operation": "Apply",
                "apiVersion": "v1",
                "fieldsType": "FieldsV1",
                "fieldsV1": {"f:data": {}},
            }
        ],
    }
    if owner is not None:
        metadata["annotations"]["piceli.io/owner"] = owner
    return ObservedResource.from_manifest(
        {"apiVersion": "v1", "kind": "Secret", "metadata": metadata, "data": {}},
        ownership=ownership,
    )


def _owned(observed: ObservedResource) -> bool:
    return owned_by_release(
        observed, owner=OWNER, field_manager=MANAGER, namespace=NAMESPACE
    )


def test_only_the_release_owner_and_field_manager_count() -> None:
    assert _owned(_observed())
    assert not _owned(_observed(owner=None, ownership=Ownership.UNMANAGED))
    assert not _owned(_observed(owner="other", ownership=Ownership.UNMANAGED))
    # An inherited owner is managed, but its values are not this owner's.
    assert not _owned(_observed(owner="inherited"))
    assert not _owned(_observed(manager="kubectl"))
    marked = {"piceli.io/namespace": "shop-staging"}
    assert not _owned(_observed(annotations=marked))


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode()


def test_random_values_must_have_the_generator_length_and_alphabet() -> None:
    spec = RandomSecretSpec(type="random", bytes=24)
    _check("password", spec, {"password": _b64(secrets.token_urlsafe(24).encode())})
    for wrong in (secrets.token_urlsafe(32), "a" * 31 + "!", "short"):
        with pytest.raises(SecretError) as error:
            _check("password", spec, {"password": _b64(wrong.encode())})
        assert error.value.code == "secret-adoption-invalid"
        assert wrong not in str(error.value)
    with pytest.raises(SecretError):
        _check("password", spec, {"password": "%%% not base64"})


def test_tls_values_must_be_a_certificate_and_a_key() -> None:
    spec = TlsSelfSignedSpec(type="tls-self-signed", dns_names=("web",))
    certificate = b"-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
    key = b"-----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----\n"
    _check("web", spec, {"web.crt": _b64(certificate), "web.key": _b64(key)})
    with pytest.raises(SecretError):
        _check("web", spec, {"web.crt": _b64(key), "web.key": _b64(key)})
    with pytest.raises(SecretError):
        _check("web", spec, {"web.crt": _b64(certificate), "web.key": _b64(b"x")})
