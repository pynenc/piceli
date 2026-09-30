"""Multi-platform host builds: one spec, one layer set per platform, one image index."""

from __future__ import annotations

import argparse
import dataclasses
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from piceli.artifacts.build_spec import BuildSpecError, run_build_spec_command
from piceli.artifacts.host_build import HostBuildGrant, HostBuildSpec
from piceli.artifacts.image_manifest import OCI_INDEX, scan_image_stream
from piceli.artifacts.multi_platform import (
    IndexEntry,
    MultiPlatformHostBuild,
    image_index,
)
from piceli.artifacts.node_facts import NodeFacts
from piceli.artifacts.plan import digest
from tests.unit.host_build_support import publish_base, write_project
from tests.unit.test_host_build import Recorder
from tests.unit.test_registry_delivery import FakeRegistry

BOTH = ("linux/amd64", "linux/arm64")


@pytest.fixture
def registry() -> Iterator[FakeRegistry]:
    fake = FakeRegistry()
    yield fake
    fake.close()


@pytest.fixture
def project(tmp_path: Path, registry: FakeRegistry) -> Path:
    base = publish_base(registry, architectures=("amd64", "arm64"))
    path = write_project(tmp_path / "project", registry.port, base)
    text = path.read_text().replace(
        'tools = ["sh"]', 'tools = ["sh"]\nplatforms = ["linux/arm64", "linux/amd64"]'
    )
    path.write_text(text)
    return path


def build(
    spec: HostBuildSpec, out: Path, platforms: tuple[str, ...] = ()
) -> dict[str, Any]:
    item = MultiPlatformHostBuild(spec, platforms)
    grant = HostBuildGrant(item.plan().plan_hash, time.time() + 600)
    return item.run(grant, out, runner=Recorder()).to_dict()


def test_one_spec_with_two_platforms_yields_an_index_of_both(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DOCKER_HOST", "unix:///nonexistent/docker.sock")
    spec = HostBuildSpec.from_toml(project).with_cache_dir(tmp_path / "cache")
    out = tmp_path / "out"
    receipt = build(spec, out)
    assert receipt["platforms"] == ["linux/arm64", "linux/amd64"]
    images = receipt["outputs"]["images"]
    assert set(images) == {"web/linux-amd64", "web/linux-arm64"}
    for platform in BOTH:
        entry = images[f"web/{platform.replace('/', '-')}"]
        with (out / entry["archive"]).open("rb") as stream:
            plan = scan_image_stream(stream)
        assert plan.platform == platform
        assert plan.manifest_digest == entry["digest"]
        assert (out / entry["sbom"]["path"]).is_file()
        # Each platform compiled its own target: {rust_arch} was substituted.
        files = {item["path"] for item in entry["files"]}
        assert "/usr/local/bin/web" in files
    assert receipt["node_facts"]["linux/amd64"]["architecture"] == "amd64"
    assert receipt["node_facts"]["linux/arm64"]["page_size"] == 4096
    index = receipt["outputs"]["indexes"]["web"]
    body = (out / index["path"]).read_bytes()
    assert digest(body) == index["digest"]
    document = json.loads(body)
    assert document["mediaType"] == OCI_INDEX
    platforms = [
        (item["platform"]["os"], item["platform"]["architecture"])
        for item in document["manifests"]
    ]
    assert platforms == [("linux", "amd64"), ("linux", "arm64")]
    assert {item["digest"] for item in document["manifests"]} == {
        images["web/linux-amd64"]["digest"],
        images["web/linux-arm64"]["digest"],
    }
    # The same inputs in a fresh cache give the same index digest.
    again = build(
        HostBuildSpec.from_toml(project).with_cache_dir(tmp_path / "cache2"),
        tmp_path / "out2",
    )
    assert again["outputs"]["indexes"]["web"]["digest"] == index["digest"]


def test_the_plan_hash_covers_every_platform_and_binds_the_grant(
    project: Path, tmp_path: Path
) -> None:
    spec = HostBuildSpec.from_toml(project).with_cache_dir(tmp_path / "cache")
    both = MultiPlatformHostBuild(spec).plan()
    one = MultiPlatformHostBuild(spec, ("linux/amd64",)).plan()
    assert both.platforms == ("linux/arm64", "linux/amd64")
    assert both.plan_hash not in {plan.plan_hash for plan in both.plans}
    # One platform keeps that platform's own plan hash.
    single = spec.for_node(NodeFacts.for_platform("linux/amd64")).plan()
    assert one.plan_hash == single.plan_hash
    assert both.preview()["builds"]["linux/amd64"]["plan_hash"] == single.plan_hash
    with pytest.raises(BuildSpecError) as error:
        MultiPlatformHostBuild(spec).run(
            HostBuildGrant(single.plan_hash, time.time() + 60), tmp_path / "out"
        )
    assert error.value.code == "plan-not-approved"
    assert not (tmp_path / "out").exists()


def test_platforms_are_validated(project: Path, tmp_path: Path) -> None:
    spec = HostBuildSpec.from_toml(project)
    for platforms in (
        ("linux/amd64", "linux/amd64"),
        ("linux/riscv64",),
        ("darwin/arm64",),
    ):
        with pytest.raises(BuildSpecError) as error:
            MultiPlatformHostBuild(spec, platforms).plan()
        assert error.value.code == "build-platforms-invalid"
    narrow = dataclasses.replace(spec, declared_platforms=("linux/arm64",))
    with pytest.raises(BuildSpecError) as error:
        MultiPlatformHostBuild(narrow, ("linux/amd64",)).plan()
    assert error.value.code == "build-platforms-invalid"
    undeclared = dataclasses.replace(spec, declared_platforms=())
    with pytest.raises(BuildSpecError) as error:
        MultiPlatformHostBuild(undeclared).plan()
    assert error.value.code == "build-platforms-invalid"
    # A node of an undeclared platform is refused for a deploy too.
    with pytest.raises(BuildSpecError) as error:
        narrow.for_node(NodeFacts.for_platform("linux/amd64"))
    assert error.value.code == "node-platform-mismatch"


def test_a_spec_without_platforms_keeps_its_digest(project: Path) -> None:
    spec = HostBuildSpec.from_toml(project)
    assert spec.to_dict()["platforms"] == ["linux/arm64", "linux/amd64"]
    plain = dataclasses.replace(spec, declared_platforms=())
    assert "platforms" not in plain.to_dict()
    with pytest.raises(BuildSpecError):
        dataclasses.replace(spec, platform="linux/arm64")


def test_the_index_is_canonical() -> None:
    amd = IndexEntry("linux/amd64", "sha256:" + "a" * 64, 10)
    arm = IndexEntry("linux/arm64", "sha256:" + "b" * 64, 11)
    assert image_index([arm, amd]) == image_index([amd, arm])
    with pytest.raises(ValueError):
        image_index([amd, amd])
    with pytest.raises(ValueError):
        image_index([])


def _args(**values: Any) -> argparse.Namespace:
    defaults: dict[str, Any] = {
        "inputs": None,
        "platforms": None,
        "cache_dir": None,
        "lock": None,
        "approve_builder": None,
        "approve_plan": None,
        "allow_network": False,
        "output_dir": None,
        "docker": None,
        "docker_sha256": None,
        "log": None,
        "progress": "quiet",
        "max_seconds": 600.0,
    }
    return argparse.Namespace(**(defaults | values))


def test_the_cli_previews_and_runs_a_multi_platform_host_build(
    project: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cache = tmp_path / "cache"
    preview = _args(build_spec_command="preview", spec=project, cache_dir=cache)
    assert run_build_spec_command(preview) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["platforms"] == ["linux/arm64", "linux/amd64"]
    out = tmp_path / "out" / "receipt.json"
    run = _args(
        build_spec_command="run",
        spec=project,
        cache_dir=cache,
        approve_plan=plan["plan_hash"],
        out=out,
    )
    assert run_build_spec_command(run, runner=Recorder()) == 0
    receipt = json.loads(out.read_text())
    assert set(receipt["outputs"]["indexes"]) == {"web"}
    capsys.readouterr()
    # A docker builder approval makes no sense for a host build.
    wrong = _args(
        build_spec_command="run",
        spec=project,
        cache_dir=cache,
        approve_plan=plan["plan_hash"],
        approve_builder="sha256:" + "0" * 64,
        out=out,
    )
    assert run_build_spec_command(wrong) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "build-builder-mismatch"
