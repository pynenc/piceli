import os
from typing import cast
from unittest.mock import Mock

import pytest

from piceli.k8s.k8s_objects.base import K8sObject
from piceli.k8s.object_manager.base import ObjectManager
from piceli.k8s.ops import loader
from piceli.k8s.ops.deploy.deployment_graph import DeploymentGraph


@pytest.fixture
def resources() -> list[K8sObject]:
    test_yaml = os.path.join(os.path.dirname(__file__), "resources", "deployment.yml")
    return loader.load_resources_from_yaml([test_yaml])


@pytest.fixture
def resources_update() -> list[K8sObject]:
    test_yaml = os.path.join(
        os.path.dirname(__file__), "resources", "deployment_update.yml"
    )
    return loader.load_resources_from_yaml([test_yaml])


@pytest.fixture
def mock_k8s_objects() -> list[K8sObject]:
    """Create mock Kubernetes objects for testing."""
    mock_objs = []
    for kind in [
        "Namespace",
        "Secret",
        "ConfigMap",
        "Deployment",
        "Service",
        "Job",
        "CronJob",
    ]:
        mock_obj = Mock(spec=K8sObject)
        mock_obj.kind = kind
        mock_obj.name = f"example-{kind.lower()}"
        mock_obj.namespace = "example-namespace"
        mock_obj.identifier = f"{kind.lower()}-identifier"
        mock_obj.spec = {"metadata": {"name": mock_obj.name}}
        mock_objs.append(mock_obj)
    return cast(list[K8sObject], mock_objs)


@pytest.fixture
def object_managers(mock_k8s_objects: list[K8sObject]) -> list[ObjectManager]:
    """Create sample ObjectManagers for testing."""
    return [ObjectManager(k8s_object=obj) for obj in mock_k8s_objects]


@pytest.fixture
def deployment_graph() -> DeploymentGraph:
    """Create a DeploymentGraph instance for testing."""
    return DeploymentGraph()
