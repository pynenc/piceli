import pytest
from kubernetes import client
from pydantic import ValidationError

from piceli.k8s import templates
from tests.unit.templates import yaml_utils


def test_persistent_volume() -> None:
    """test persistent volume"""
    per_vol = templates.PersistentVolume(
        name="test-pv",
        storage="100Gi",
        disk_name="disk-name",
        labels={"component": "test"},
    )
    per_vol_dict = client.ApiClient().sanitize_for_serialization(per_vol.get())
    assert per_vol_dict == yaml_utils.get_yaml_dict("persistent_volume.yml")


def test_persistent_volume_claim() -> None:
    """test persistent volume claim"""
    vol_claim = templates.PersistentVolumeClaim(
        name="test-pvc",
        storage="100Gi",
        labels={"component": "test"},
    )
    vol_claim_dict = client.ApiClient().sanitize_for_serialization(vol_claim.get())
    assert vol_claim_dict == yaml_utils.get_yaml_dict("persistent_volume_claim.yml")


def test_storage_validation() -> None:
    """Test the size used are valid"""

    def test_invalid_storage(storage: str) -> None:
        with pytest.raises(ValidationError):
            templates.PersistentVolumeClaim(name="test-pvc", storage=storage)

    # test invalid
    test_invalid_storage("XX")
    test_invalid_storage("1Yi")
    # test it works
    templates.PersistentVolumeClaim(name="test-pvc", storage="1Gi")
    templates.PersistentVolumeClaim(
        name="test-pvc", storage="-1Gi"
    )  # k8s converts to 1Gi
