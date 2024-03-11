import os

from piceli.k8s.k8s_objects.base import K8sObject, OriginTemplate, OriginYAML
from piceli.k8s.ops import loader


def test_load_all_resources() -> None:
    test_module = "tests.unit.ops.resources.simple_job"
    test_yaml = os.path.join(os.path.dirname(__file__), "resources", "simple_job.yml")

    resources = loader.load_all_resources([test_module], [test_yaml])

    assert len(resources) == 2, "Should load one template object and one YAML resource"
    assert all(isinstance(r, K8sObject) for r in resources)
    assert resources[0].spec == resources[1].spec
    for resource in resources:
        if isinstance(resource.origin, OriginTemplate):
            assert resource.origin.module == test_module
            assert resource.origin.name == "simple_job"
        elif isinstance(resource.origin, OriginYAML):
            assert resource.origin.path == test_yaml
        else:
            raise AssertionError(f"Invalid origin type for {resource=}")


def test_load_yaml_multiple_jobs() -> None:
    test_yaml = os.path.join(
        os.path.dirname(__file__), "resources", "multiple_jobs.yml"
    )

    resources = loader.load_resources_from_yaml([test_yaml])

    assert len(resources) == 2, "Should load two YAML resources from the same file"
    assert all(isinstance(r, K8sObject) for r in resources)
    assert resources[0].spec != resources[1].spec
    for resource in resources:
        assert isinstance(resource.origin, OriginYAML)
        assert resource.origin.path == test_yaml
