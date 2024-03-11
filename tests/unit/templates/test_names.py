import pytest
from pydantic import BaseModel, ValidationError

from piceli.k8s.templates.auxiliary import names


class CheckName(BaseModel):
    name: names.Name


class CheckDNSLabel(BaseModel):
    label: names.DNSLabel


class CheckDNSSubdomain(BaseModel):
    subdomain: names.DNSSubdomain


class CheckUUID(BaseModel):
    uuid: names.UUID


class CheckIANASvcName(BaseModel):
    svc_name: names.IANASvcName


@pytest.mark.parametrize("value", ["valid-name", "a1-b2-c3"])
def test_name_valid(value: str) -> None:
    CheckName(name=value)


@pytest.mark.parametrize("value", ["-invalid", "invalid-", "invalid name", ""])
def test_name_invalid(value: str) -> None:
    with pytest.raises(ValueError):
        CheckName(name=value)


@pytest.mark.parametrize("value", ["valid-label", "a", "a1"])
def test_DNSLabel_valid(value: str) -> None:
    CheckDNSLabel(label=value)


@pytest.mark.parametrize("value", ["a" * 64, "-invalid", "invalid-"])
def test_DNSLabel_invalid(value: str) -> None:
    with pytest.raises(ValidationError):
        CheckDNSLabel(label=value)


@pytest.mark.parametrize("value", ["valid.sub.domain", "a.b.c"])
def test_DNSSubdomain_valid(value: str) -> None:
    CheckDNSSubdomain(subdomain=value)


@pytest.mark.parametrize(
    "value", ["-invalid.com", "invalid-.com", "invalid.com-", "a" * 254]
)
def test_DNSSubdomain_invalid(value: str) -> None:
    with pytest.raises(ValidationError):
        CheckDNSSubdomain(subdomain=value)


@pytest.mark.parametrize("value", ["123e4567-e89b-12d3-a456-426614174000"])
def test_UUID_valid(value: str) -> None:
    CheckUUID(uuid=value)


@pytest.mark.parametrize(
    "value", ["invalid-uuid", "12345678-1234-1234-1234-1234567890123"]
)
def test_UUID_invalid(value: str) -> None:
    with pytest.raises(ValidationError):
        CheckUUID(uuid=value)


@pytest.mark.parametrize("value", ["valid-svc", "a1-b2"])
def test_IANASvcName_valid(value: str) -> None:
    CheckIANASvcName(svc_name=value)


@pytest.mark.parametrize("value", ["-invalid", "invalid-", "invalid--name", "a" * 16])
def test_IANASvcName_invalid(value: str) -> None:
    with pytest.raises(ValidationError):
        CheckIANASvcName(svc_name=value)
