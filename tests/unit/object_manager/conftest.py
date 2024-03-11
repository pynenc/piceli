import pytest

from piceli.k8s.object_manager.base import ObjectManager
from piceli.k8s.ops import loader


@pytest.fixture
def object_manager() -> ObjectManager:
    """Create a k8s object manager."""
    k8s_objects = loader.load_resources_from_yaml(
        ["tests/unit/object_manager/resources/cronjob.yml"]
    )
    return ObjectManager(k8s_objects[0])
