from pathlib import Path

from piceli.k8s.ops import loader

TEST_DIR = Path(__file__).parent
RESOURCE_DIR = TEST_DIR / "resources"


def test_load_modules_by_path() -> None:
    # Test loading modules directly from file paths without sub-elements
    modules = list(loader.load_modules_by_path(str(RESOURCE_DIR), sub_elements=False))
    assert any(m.__name__.endswith("simple_job") for m in modules)
    assert not any(m.__name__.endswith("other_job") for m in modules)

    # Test loading modules with sub-elements
    modules_with_sub = list(
        loader.load_modules_by_path(str(RESOURCE_DIR), sub_elements=True)
    )
    assert any(m.__name__.endswith("simple_job") for m in modules_with_sub)
    assert any(m.__name__.endswith("other_job") for m in modules_with_sub)


def test_load_models_from_by_module_name() -> None:
    module_name = "tests.unit.ops.resources.simple_job"
    resources = list(loader.find_modules_by_name(module_name, sub_elements=False))
    assert len(resources) == 1
    assert resources[0].endswith("simple_job")

    module_name = "tests.unit.ops.resources"
    resources = list(loader.find_modules_by_name(module_name, sub_elements=True))
    assert any(r.endswith("simple_job") for r in resources)
    assert any(r.endswith("other_job") for r in resources)


def test_load_files_from_folder_without_sub_elements() -> None:
    """Test loading files from the resources folder without including subdirectories."""
    files = list(loader.load_files_from_folder(str(RESOURCE_DIR), sub_elements=False))
    assert str(RESOURCE_DIR / "multiple_jobs.yml") in files
    assert str(RESOURCE_DIR / "simple_job.py") in files
    assert str(RESOURCE_DIR / "simple_job.yml") in files
    assert str(RESOURCE_DIR / "sub_resources" / "other_job.py") not in files
    assert str(RESOURCE_DIR / "sub_resources" / "other_job.yml") not in files


def test_load_files_from_folder_with_sub_elements() -> None:
    """Test loading files from the resources folder including subdirectories."""
    files = list(loader.load_files_from_folder(str(RESOURCE_DIR), sub_elements=True))
    assert str(RESOURCE_DIR / "multiple_jobs.yml") in files
    assert str(RESOURCE_DIR / "simple_job.py") in files
    assert str(RESOURCE_DIR / "simple_job.yml") in files
    assert str(RESOURCE_DIR / "sub_resources" / "other_job.py") in files
    assert str(RESOURCE_DIR / "sub_resources" / "other_job.yml") in files
