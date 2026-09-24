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


def test_load_modules_by_path_executes_modules() -> None:
    """Modules loaded by path must be executed so their objects are found"""
    modules = list(loader.load_modules_by_path(str(RESOURCE_DIR), sub_elements=True))
    names = {
        obj.name for module in modules for obj in loader.load_models_from_module(module)
    }
    assert {"tasker-scheduler", "other-job"} <= names


def test_load_all_by_module_path_single_file() -> None:
    objects = list(
        loader.load_all(
            module_name="",
            module_path=str(RESOURCE_DIR / "simple_job.py"),
            folder_path="",
            sub_elements=False,
        )
    )
    assert [(o.kind, o.name) for o in objects] == [("Job", "tasker-scheduler")]


DEPLOYMENT_MODULE = """
from piceli.k8s import templates

web = templates.Deployment(
    name="web",
    containers=[
        templates.Container(
            name="web",
            image="nginx",
            ports=[templates.Port(name="http", port=80)],
        )
    ],
    create_service=True,
)
"""


def test_load_modules_by_path_from_tmp_dir(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(DEPLOYMENT_MODULE)
    (tmp_path / "notes.txt").write_text("not python")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "__init__.py").write_text("")
    (sub / "app.py").write_text(DEPLOYMENT_MODULE.replace('"web"', '"api"'))

    # files with the same stem in different folders must not collide
    objects = list(loader.load_all("", str(tmp_path), "", sub_elements=True))
    assert sorted((o.kind, o.name) for o in objects) == [
        ("Deployment", "api"),
        ("Deployment", "web"),
        ("Service", "api"),
        ("Service", "web"),
    ]

    # without sub elements only the top level module is loaded
    objects = list(loader.load_all("", str(tmp_path), "", sub_elements=False))
    assert sorted((o.kind, o.name) for o in objects) == [
        ("Deployment", "web"),
        ("Service", "web"),
    ]


def test_load_modules_by_path_loads_each_file_once(tmp_path: Path) -> None:
    counter = tmp_path / "counter.txt"
    (tmp_path / "mod.py").write_text(
        "from pathlib import Path\n"
        f"p = Path({str(counter)!r})\n"
        "p.write_text(str(int(p.read_text() if p.exists() else 0) + 1))\n"
    )
    first = list(loader.load_modules_by_path(str(tmp_path), sub_elements=True))
    second = list(loader.load_modules_by_path(str(tmp_path / "mod.py"), False))
    assert len(first) == 1
    assert first[0] is second[0]
    assert counter.read_text() == "1"


def test_load_modules_by_path_ignores_non_python_file(tmp_path: Path) -> None:
    other = tmp_path / "manifest.yml"
    other.write_text("kind: Job")
    assert list(loader.load_modules_by_path(str(other), sub_elements=False)) == []
