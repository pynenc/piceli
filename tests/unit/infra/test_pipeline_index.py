"""0.74.0: a pipeline image built for several platforms is recorded as its
OCI index: the digest, the media type and each platform's manifest, in the
GitOps status, the history and ``piceli gitops builds``.

A spec that declares ``build.platforms`` builds every one of them, also
those the environment's nodes do not run (they stay in the registry under
the index, so a release can copy it by digest).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from piceli.infra import builds as projection
from piceli.infra.builders import BuiltImage, JobBuilder, JobSettings
from piceli.infra.controller import CompositionConfig
from piceli.infra.pipelines import SpecBuildRequest
from tests.unit.infra.git_support import Repo, example_files
from tests.unit.infra.test_builders import _Cluster, _spec_request
from tests.unit.infra.test_pipeline_composition import (
    EXAMPLE,
    SUMMARY,
    FakeBuilder,
    FakePorts,
    _controller,
)

BOTH = ("linux/amd64", "linux/arm64")
INDEX = "application/vnd.oci.image.index.v1+json"


def _manifest(name: str, platform: str) -> str:
    return "sha256:" + hashlib.sha256(f"{name} {platform}".encode()).hexdigest()


class IndexBuilder(FakeBuilder):
    """Answers like the real builders: several platforms, one index."""

    def build_spec(self, request: SpecBuildRequest, checkout: Any) -> dict[str, Any]:
        self.requests.append(request)
        built = {}
        for name, wanted in request.images.items():
            digests = {item: _manifest(name, item) for item in request.platforms}
            extra = {"platforms": digests} if len(digests) > 1 else {}
            built[name] = BuiltImage(
                f"registry.example:5000/{wanted['repository']}@{wanted['key']}",
                wanted["key"],
                extra,
            )
        return built


def _world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, declare: bool) -> Any:
    files = example_files(EXAMPLE / "infra")
    files["infra.py"] = (
        files["infra.py"]
        .replace(
            "from piceli.infra import Cluster, Controller, Source",
            "from piceli.infra import Cluster, Controller, Node, Source",
        )
        .replace(
            "    controller=Controller(on=REGISTRY_NODE),",
            '    nodes=[Node(REGISTRY_NODE, arch="amd64"), Node("arm-1", arch="arm64")],\n'
            "    controller=Controller(on=REGISTRY_NODE),",
        )
        .replace(
            '        namespace="example-main",',
            '        namespace="example-main",\n        on_nodes=["arm-1"],',
        )
    )
    if declare:
        files["host-build.toml"] = files["host-build.toml"].replace(
            '[build]\ntools = ["true"]',
            '[build]\nplatforms = ["linux/amd64", "linux/arm64"]\ntools = ["true"]',
        )
        assert "platforms = " in files["host-build.toml"]
    remotes = tmp_path / "remotes"
    infra = Repo(remotes, "infra", files)
    for name in ("api", "worker"):
        repo = Repo(remotes, name, example_files(EXAMPLE / name))
        repo.tag("v0.1.0")
        monkeypatch.setenv(f"EXAMPLE_{name.upper()}_URL", repo.url)
    monkeypatch.setenv("EXAMPLE_INFRA_URL", infra.url)
    ports = FakePorts()
    ports.builder = IndexBuilder()
    controller = _controller(tmp_path, infra, ports, {"now": 1_000_000.0})
    controller.config = CompositionConfig(
        composition=SUMMARY,
        repo=controller.config.repo,
        poll_seconds=10,
        platforms=BOTH,
    )
    return controller, ports


def test_declared_platforms_are_built_and_recorded_as_one_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, ports = _world(tmp_path, monkeypatch, declare=True)
    status = controller.poll_once()
    # main's pods run on one arm64 node, yet both declared platforms build.
    assert {tuple(item.platforms) for item in ports.builder.requests} == {BOTH}
    api = status["envs"]["main"]["components"]["api"]
    assert api["media_type"] == INDEX
    assert sorted(api["platforms"]) == list(BOTH)
    assert api["platforms"]["linux/amd64"] == _manifest("api", "linux/amd64")
    assert api["image"].endswith("@" + api["digest"])
    # The history keeps it, and `gitops builds` lists each image's digests.
    history = controller.channel.read_history()
    document = projection.records(status, history, now=1_000_100.0)
    done = [item for item in document["builds"] if item["target"] == "main"]
    assert done and done[0]["digests"]["api"] == {
        "digest": api["digest"],
        "media_type": INDEX,
        "platforms": api["platforms"],
    }


def test_without_declared_platforms_the_placed_platform_only_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 0.68.0 rule, and the single-platform entry, are unchanged."""
    controller, ports = _world(tmp_path, monkeypatch, declare=False)
    status = controller.poll_once()
    found = {tuple(item.platforms) for item in ports.builder.requests}
    assert ("linux/arm64",) in found
    api = status["envs"]["main"]["components"]["api"]
    assert "media_type" not in api and "platforms" not in api


def test_the_job_builder_keeps_each_platforms_manifest_and_what_it_built() -> None:
    index = "sha256:" + "9" * 64
    digests = {"linux/amd64": "sha256:" + "1" * 64, "linux/arm64": "sha256:" + "2" * 64}
    request = _spec_request()
    receipt = {
        "images": {
            "api": {
                "pull_ref": f"registry:5000/example/api@{index}",
                "manifest_digest": index,
                "key": request.images["api"]["key"],
                "platform": None,
                "platforms": digests,
            }
        }
    }
    settings = JobSettings(
        image="registry.example/builder@sha256:" + "f" * 64,
        namespace="piceli-system",
        registry_url="oci://registry:5000",
        node_registry="registry:5000",
        platforms=("linux/arm64",),  # enabled; the build made more
    )
    builder = JobBuilder(settings, {}, _Cluster(receipt), mirror_route=None)
    built = builder.build_spec(request, checkout=None)  # type: ignore[arg-type]
    assert built["api"].manifest_digest == index
    assert built["api"].extra["platforms"] == digests
    assert built["api"].extra["provenance"]["platforms"] == list(BOTH)
