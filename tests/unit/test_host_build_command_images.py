"""0.68.0: commands for some images, and builds of the changed images only.

A command may be ``{ run = [...], images = [...] }``: a build of some images
(``HostBuildSpec.for_images``, what a pipeline build of its changed images
asks) runs it only when it makes one of them, and assembles only those
images. A plain argv list runs in every build.
"""

from __future__ import annotations

import time
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from piceli.artifacts.build_spec import BuildSpecError
from piceli.artifacts.host_build import HostBuildGrant, HostBuildSpec
from tests.unit.host_build_support import (
    FACTS_16K,
    HOST_TOML,
    publish_base,
    write_project,
)
from tests.unit.test_host_build import Recorder
from tests.unit.test_registry_delivery import FakeRegistry

#: A command for the ``api`` image only; it records each run.
API_COMMAND = (
    '  { images = ["api"], run = ["sh", "-c", "echo api > '
    '\\"$CARGO_TARGET_DIR/api.txt\\" && echo run >> \\"$CARGO_TARGET_DIR/api-runs\\""] },\n'
)
API_IMAGE = """
[[output.image]]
name = "api"
repository = "example/api"
base = { image = "127.0.0.1:@PORT@/base/runtime", digest = "@BASE@" }
entrypoint = ["/usr/local/bin/api"]
target_files = { "api.txt" = "/usr/local/bin/api" }
"""


def two_images(text: str) -> str:
    """`HOST_TOML` with an ``api`` image; its build command is for ``web``."""
    text = text.replace(
        "\ncommands = [\n  [", '\ncommands = [\n  { images = ["web"], run = [', 1
    )
    text = text.replace('"],\n]\nenv', '"] },\n' + API_COMMAND + "]\nenv", 1)
    return text + API_IMAGE


@pytest.fixture
def registry() -> Iterator[FakeRegistry]:
    fake = FakeRegistry()
    yield fake
    fake.close()


@pytest.fixture
def project(tmp_path: Path, registry: FakeRegistry) -> Path:
    base = publish_base(registry)
    path = write_project(tmp_path / "project", registry.port, base)
    text = two_images(path.read_text())
    path.write_text(text.replace("@PORT@", str(registry.port)).replace("@BASE@", base))
    return path


def document() -> dict[str, Any]:
    text = two_images(HOST_TOML)
    return tomllib.loads(
        text.replace("@PORT@", "5000").replace("@BASE@", "sha256:" + "0" * 64)
    )


def refused(value: dict[str, Any]) -> str:
    with pytest.raises(BuildSpecError) as error:
        HostBuildSpec.from_dict(value, Path("/nonexistent"))
    assert error.value.code == "invalid-spec"
    return str(error.value)


def test_a_command_names_the_images_it_is_for() -> None:
    spec = HostBuildSpec.from_dict(document(), Path("/x"))
    assert spec.command_images == (("web",), ("api",))
    listed = spec.to_dict()["commands"]
    assert listed[1] == {"run": list(spec.commands[1]), "images": ["api"]}
    # The table form reads back to the same spec.
    value = document()
    value["build"]["commands"] = listed
    assert HostBuildSpec.from_dict(value, Path("/x")).spec_sha256 == spec.spec_sha256
    # A plain command list keeps its form (and so every earlier digest).
    value["build"]["commands"] = [list(item) for item in spec.commands]
    plain = HostBuildSpec.from_dict(value, Path("/x"))
    assert plain.command_images == ()
    assert plain.to_dict()["commands"] == [list(item) for item in spec.commands]


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        ({"run": ["sh", "-c", "true"], "images": ["cache"]}, "'cache', which is not"),
        ({"run": ["sh", "-c", "true"], "images": []}, "1-32 image names"),
        ({"run": ["sh", "-c", "true"], "images": ["web", "web"]}, "each once"),
        ({"run": ["sh", "-c", "true"]}, "expected fields"),
        (
            {"run": ["sh", "-c", "true"], "images": ["web"], "when": "x"},
            "expected fields",
        ),
        ({"run": [], "images": ["web"]}, "non-empty list of strings"),
        ({"run": ["cargo", "build"], "images": ["web"]}, "not a declared tool"),
    ],
)
def test_an_invalid_images_command_is_refused(
    entry: dict[str, Any], reason: str
) -> None:
    for key in ("commands", "shared_commands"):
        value = document()
        value["build"][key] = [entry]
        assert reason in refused(value), key


def test_for_images_refuses_an_undeclared_image() -> None:
    spec = HostBuildSpec.from_dict(document(), Path("/x"))
    with pytest.raises(BuildSpecError, match="'cache'"):
        spec.for_images(["cache"])


def _run(spec: HostBuildSpec, out: Path, runner: Recorder) -> dict[str, Any]:
    plan = spec.plan()
    grant = HostBuildGrant(plan.plan_hash, time.time() + 600)
    return spec.run(grant, out, runner=runner).to_dict()


def test_only_the_asked_images_are_assembled_and_their_commands_run(
    project: Path, tmp_path: Path
) -> None:
    cache = tmp_path / "cache"
    spec = HostBuildSpec.from_toml(project).for_node(FACTS_16K).with_cache_dir(cache)
    plans = {
        names: spec.for_images(names).plan()
        for names in (("api",), ("web",), ("api", "web"))
    }
    assert [len(plan.commands) for plan in plans.values()] == [1, 1, 2]
    assert [image.name for image in plans[("api",)].images] == ["api"]
    assert plans[("api", "web")].plan_hash == spec.plan().plan_hash
    assert len({plan.plan_hash for plan in plans.values()}) == 3
    preview = plans[("api",)].preview()
    assert list(preview["images"]) == ["api"]
    assert preview["commands"] == [list(spec.commands[1])]

    runner = Recorder()
    receipt = _run(spec.for_images(["api"]), tmp_path / "out", runner)
    assert len(runner.argv) == 1 and "api.txt" in runner.argv[0][-1]
    assert list(receipt["outputs"]["images"]) == ["api"]
    kinds = [step["kind"] for step in receipt["steps"]]
    assert kinds == ["command-1", "image-api"]
    assert (tmp_path / "out" / "attestations" / "api.provenance.json").exists()
    assert not (tmp_path / "out" / "attestations" / "web.provenance.json").exists()
    # Every image: every command (the plain spec, as before).
    runner = Recorder()
    receipt = _run(spec, tmp_path / "all", runner)
    assert len(runner.argv) == 2
    assert sorted(receipt["outputs"]["images"]) == ["api", "web"]


def test_a_spec_build_assembles_and_pushes_only_the_changed_images(
    project: Path, tmp_path: Path
) -> None:
    from piceli.infra.pipelines import SpecBuildRequest, run_spec_build

    cache = tmp_path / "cache"
    pushed: list[str] = []

    def deliver(name: str, archive: Path, image_id: str, repository: str) -> Any:
        pushed.append(name)
        return {"state": "succeeded", "pull_ref": f"reg/{repository}@{image_id}"}

    request = SpecBuildRequest(
        "host-build.toml",
        "infra",
        {"infra": "1" * 40},
        {"api": {"repository": "example/api", "key": "sha256:" + "2" * 64}},
    )
    built = run_spec_build(
        request,
        {"infra": project.parent},
        platforms=("linux/arm64",),
        cache=cache,
        out=tmp_path / "out",
        deliver=deliver,
    )
    assert pushed == ["api"]
    assert list(built["images"]) == ["api"]
    assert [item["index"] for item in built["commands"]] == [1]
    assert list((cache / "target").rglob("api-runs"))
    assert not list((cache / "target").rglob("web"))
