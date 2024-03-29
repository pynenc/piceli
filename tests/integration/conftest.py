import os

import pytest

from piceli.k8s.k8s_objects.base import K8sObject
from piceli.k8s.ops import loader


@pytest.fixture
def resources() -> list[K8sObject]:
    test_yaml = os.path.join(os.path.dirname(__file__), "resources", "deployment.yml")
    return list(loader.load_resources_from_files([test_yaml]))


@pytest.fixture
def resources_update() -> list[K8sObject]:
    test_yaml = os.path.join(
        os.path.dirname(__file__), "resources", "deployment_update.yml"
    )
    return list(loader.load_resources_from_files([test_yaml]))
