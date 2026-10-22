"""Shared commands (0.23): steps that are the same for every platform run once."""

from __future__ import annotations

import time
import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from piceli.artifacts.build_spec import BuildSpecError
from piceli.artifacts.host_build import (
    HOST_PREVIEW_REVISION,
    HostBuildGrant,
    HostBuildSpec,
)
from piceli.artifacts.multi_platform import MultiPlatformHostBuild
from piceli.artifacts.node_facts import NodeFacts
from piceli.artifacts.plan import canonical, digest
from tests.unit.host_build_support import (
    FACTS_16K,
    HOST_TOML,
    add_shared_commands,
    publish_base,
    write_project,
)
from tests.unit.test_host_build import Recorder
from tests.unit.test_registry_delivery import FakeRegistry

BOTH = ("linux/arm64", "linux/amd64")
#: The spec digest of `HOST_TOML` (port 5000, a zero base digest) before
#: shared commands existed: a spec without them keeps it.
PLAIN_SPEC_SHA256 = (
    "sha256:b6f1184d316c85c99adbfe4e26862a8d465534570bc65c6da059fe7c7f6dabf6"
)


@pytest.fixture
def registry() -> Iterator[FakeRegistry]:
    fake = FakeRegistry()
    yield fake
    fake.close()


@pytest.fixture
def project(tmp_path: Path, registry: FakeRegistry) -> Path:
    base = publish_base(registry, architectures=("amd64", "arm64"))
    path = write_project(tmp_path / "project", registry.port, base)
    path.write_text(
        path.read_text().replace(
            'tools = ["sh"]',
            'tools = ["sh"]\nplatforms = ["linux/arm64", "linux/amd64"]',
        )
    )
    return add_shared_commands(path)


def document(**build: Any) -> dict[str, Any]:
    value = tomllib.loads(
        HOST_TOML.replace("@PORT@", "5000").replace("@BASE@", "sha256:" + "0" * 64)
    )
    value["build"].update(build)
    return value


def refused(value: dict[str, Any]) -> str:
    with pytest.raises(BuildSpecError) as error:
        HostBuildSpec.from_dict(value, Path("/nonexistent"))
    assert error.value.code == "invalid-spec"
    return str(error.value)


def site_files(entry: dict[str, Any]) -> dict[str, str]:
    return {
        item["path"]: item["sha256"]
        for item in entry["files"]
        if item["path"].startswith("/srv/site/")
    }


def test_a_shared_command_runs_once_and_every_platform_image_has_its_output(
    project: Path, tmp_path: Path
) -> None:
    cache, out = tmp_path / "cache", tmp_path / "out"
    spec = HostBuildSpec.from_toml(project).with_cache_dir(cache)
    item = MultiPlatformHostBuild(spec)
    grant = HostBuildGrant(item.plan().plan_hash, time.time() + 600)
    runner = Recorder()
    receipt = item.run(grant, out, runner=runner).to_dict()
    # Two platforms, one command each, plus the shared command once.
    assert len(runner.argv) == 2 * 2 + 1
    assert (cache / "target" / "shared" / "runs").read_text() == "run\n"
    shared = [step for step in receipt["steps"] if step["kind"] == "shared-1"]
    assert len(shared) == 1
    assert shared[0]["platform"] == "any"
    assert shared[0]["state"] == "succeeded"
    assert isinstance(shared[0]["seconds"], float)
    assert receipt["shared_commands"] == {"ran_with": "linux/arm64", "steps": shared}
    assert receipt["builds"]["linux/amd64"]["steps"][0]["kind"] == "command-1"
    images = receipt["outputs"]["images"]
    arm, amd = images["web/linux-arm64"], images["web/linux-amd64"]
    assert site_files(arm) == site_files(amd)
    assert set(site_files(arm)) == {"/srv/site/app.wasm"}
    assert arm["digest"] != amd["digest"]
    # Each platform's provenance names the shared command its image holds.
    for entry in (arm, amd):
        provenance = (out / entry["provenance"]["path"]).read_text()
        assert "shared_commands" in provenance


def test_spec_builds_run_the_shared_commands_with_the_first_platform(
    project: Path, tmp_path: Path
) -> None:
    from piceli.infra.pipelines import SpecBuildRequest, run_spec_build

    cache = tmp_path / "cache"
    pushed: list[str] = []

    def deliver(name: str, archive: Path, image_id: str, repository: str) -> Any:
        pushed.append(str(archive))
        return {"state": "succeeded", "pull_ref": f"reg/{repository}@{image_id}"}

    request = SpecBuildRequest(
        "host-build.toml",
        "infra",
        {"infra": "1" * 40},
        {"web": {"repository": "example/web", "key": "sha256:" + "2" * 64}},
    )
    built = run_spec_build(
        request,
        {"infra": project.parent},
        platforms=BOTH,
        cache=cache,
        out=tmp_path / "out",
        deliver=deliver,
    )
    assert len(pushed) == 2
    assert (cache / "target" / "shared" / "runs").read_text() == "run\n"
    assert [item["index"] for item in built["shared_commands"]] == [1]
    assert [item["index"] for item in built["commands"]] == [1, 2]
    timings = built["images"]["web"]["timings"]
    assert timings["commands_seconds"] >= built["shared_commands"][0]["seconds"]


def test_a_single_platform_build_runs_the_shared_commands_first(
    project: Path, tmp_path: Path
) -> None:
    spec = (
        HostBuildSpec.from_toml(project)
        .for_node(FACTS_16K)
        .with_cache_dir(tmp_path / "cache")
    )
    plan = spec.plan()
    preview = plan.preview()
    assert preview["shared_commands"] == [list(spec.shared_commands[0])]
    # LG_PAGE uses a node fact: the shared commands do not see it.
    assert preview["shared_env"] == {}
    assert plan.env == {"LG_PAGE": "14"}
    grant = HostBuildGrant(plan.plan_hash, time.time() + 600)
    runner = Recorder()
    receipt = spec.run(grant, tmp_path / "out", runner=runner).to_dict()
    kinds = [step["kind"] for step in receipt["steps"]]
    assert kinds[:3] == ["shared-1", "command-1", "command-2"]
    assert "LG_PAGE" not in runner.env[0]
    assert runner.env[1]["LG_PAGE"] == "14"
    assert set(site_files(receipt["outputs"]["images"]["web"])) == {
        "/srv/site/app.wasm"
    }
    # The plan hash covers the shared commands.
    other = HostBuildSpec.from_dict(
        {
            **tomllib.loads(project.read_text()),
            "build": {
                **tomllib.loads(project.read_text())["build"],
                "shared_commands": [["sh", "-c", "true"]],
            },
        },
        project.parent,
    )
    assert other.for_node(FACTS_16K).plan().plan_hash != plan.plan_hash


def test_shared_commands_cannot_use_node_facts() -> None:
    for placeholder in ("{rust_arch}", "{page_size_log2}", "{platform}", "{arch}"):
        message = refused(
            document(shared_commands=[["sh", "-c", f"echo {placeholder}"]])
        )
        assert placeholder in message
        assert "build.shared_commands" in message
    # Shell braces stay literal, as in build.commands.
    HostBuildSpec.from_dict(
        document(shared_commands=[["sh", "-c", "echo ${HOME}"]]), Path("/x")
    )
    assert "not a declared tool" in refused(
        document(shared_commands=[["cargo", "build"]])
    )
    assert "list of argv lists" in refused(document(shared_commands=[]))
    value = document(shared_commands=[["sh", "-c", "true"]])
    value["output"]["image"][0]["shared_files"] = {"{rust_arch}/site": "/srv/site"}
    assert "placeholder" in refused(value)
    value = document()
    value["output"]["image"][0]["shared_files"] = {"site": "/srv/site"}
    assert "declare build.shared_commands" in refused(value)


def test_a_spec_without_shared_commands_keeps_its_digests(tmp_path: Path) -> None:
    spec = HostBuildSpec.from_dict(document(), tmp_path)
    assert spec.spec_sha256 == PLAIN_SPEC_SHA256
    assert "shared_commands" not in spec.to_dict()
    assert all("shared_files" not in image.to_dict() for image in spec.images)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.txt").write_text("v1\n")
    (tmp_path / "src" / "static.txt").write_text("static 1\n")
    plan = spec.for_node(NodeFacts.for_platform("linux/amd64")).plan()
    assert "shared_commands" not in plan.preview()
    # The plan hash is computed from exactly the fields it had before.
    assert plan.plan_hash == digest(
        canonical(
            {
                "revision": HOST_PREVIEW_REVISION,
                "spec_sha256": spec.spec_sha256,
                "node": plan.facts.placeholders(),
                "contexts": {name: item.sha256 for name, item in plan.contexts.items()},
                "tools": plan.builder()["tools"],
                "commands": [list(item) for item in plan.commands],
                "env": dict(plan.env),
                "images": [item.to_dict() for item in plan.images],
            }
        )
    )
