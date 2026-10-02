"""Component builds: generated host builds, the multi-source Job, the Job's run."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from piceli.artifacts.cluster_build import decode_receipt, encode_receipt
from piceli.infra import CompositionError
from piceli.infra.builders import (
    BuildItem,
    JobBuilder,
    JobSettings,
    component_job,
    host_build_document,
    job_run_components,
)
from piceli.infra.contract import parse_contracts

BASE = "docker.io/library/debian@sha256:" + "a" * 64
TOML = f"""
[component.api]
build = {{ rust = "crates/api", bin = "api", features = ["fast"] }}
image = {{ base = "{BASE}", user = 10001, dirs = {{ "/var/lib/api" = "0700" }} }}

[component.tool]
build = {{ python = "tool", module = "tool.main" }}
image = {{ base = "{BASE}" }}

[component.site]
build = {{ files = {{ "site" = "/srv/site", "VERSION" = "/srv/VERSION" }} }}
image = {{ cmd = ["/srv/site/run"] }}

[component.legacy]
build = {{ dockerfile = "Dockerfile" }}
"""


def test_each_recipe_becomes_a_host_build(tmp_path: Path) -> None:
    contracts = parse_contracts(TOML)
    (tmp_path / "site").mkdir()
    rust = host_build_document("api", contracts["api"], tmp_path, "shop/api")
    assert rust["build"]["tools"] == ["cargo", "cargo-zigbuild", "zig"]
    command = rust["build"]["commands"][0]
    assert command[-4:] == ["--bin", "api", "--features", "fast"]
    assert "src/crates/api/Cargo.toml" in command
    image = rust["output"]["image"][0]
    assert image["entrypoint"] == ["/usr/local/bin/api"]
    assert image["dirs"] == {"/var/lib/api": "10001:10001:0700"}
    assert image["base"] == {
        "image": "docker.io/library/debian",
        "digest": "sha256:" + "a" * 64,
    }
    assert rust["context"]["src"]["include"] == [
        "crates/api",
        "Cargo.toml",
        "Cargo.lock",
    ]
    python = host_build_document("tool", contracts["tool"], tmp_path, "shop/tool")
    assert python["output"]["image"][0]["entrypoint"] == ["python3", "-m", "tool.main"]
    files = host_build_document("site", contracts["site"], tmp_path, "shop/site")
    assert files["context"]["src"]["include"] == ["VERSION", "site/**"]
    assert "base" not in files["output"]["image"][0]  # scratch
    with pytest.raises(CompositionError) as raised:
        host_build_document("legacy", contracts["legacy"], tmp_path, "shop/legacy")
    assert raised.value.code == "component-build-unsupported"


def _item(name: str, source: str, commit: str, contracts: Any) -> BuildItem:
    return BuildItem(
        name, source, commit, "sha256:" + "e" * 64, contracts[name], f"shop/{name}"
    )


def test_the_job_fetches_every_needed_source_with_one_secret() -> None:
    contracts = parse_contracts(TOML)
    settings = JobSettings(
        image="registry.example/builder@sha256:" + "f" * 64,
        namespace="piceli-system",
        registry_url="oci://piceli-registry.piceli-system.svc:5000/shop",
        node_registry="piceli-registry.piceli-system.svc:5000",
        git_secret="git-token",
    )
    items = [
        _item("api", "shop", "1" * 40, contracts),
        _item("site", "shop", "1" * 40, contracts),
        _item("tool", "tools", "2" * 40, contracts),
    ]
    urls = {
        "shop": "https://git.example/shop.git",
        "tools": "https://git.example/tools.git",
    }
    job = component_job(settings, items, urls)
    container = job["spec"]["template"]["spec"]["containers"][0]
    env = {item["name"]: item for item in container["env"]}
    assert env["PICELI_BUILD_SOURCES"]["value"].splitlines() == [
        f"shop https://git.example/shop.git {'1' * 40}",
        f"tools https://git.example/tools.git {'2' * 40}",
    ]
    secrets = {
        item["valueFrom"]["secretKeyRef"]["name"]
        for item in env.values()
        if "valueFrom" in item
    }
    assert secrets == {"git-token"}
    built = [json.loads(arg) for arg in container["command"] if arg.startswith("{")]
    assert [item["component"] for item in built] == ["api", "site", "tool"]
    assert "password" not in json.dumps(job).replace("GIT_PASSWORD", "").replace(
        '"password"', ""
    )
    with pytest.raises(CompositionError):
        component_job(
            settings, [*items, _item("api", "shop", "3" * 40, contracts)], urls
        )


class _Outcome:
    state = "passed"


class _Cluster:
    def __init__(self, receipt: dict[str, Any]) -> None:
        self.receipt_text = encode_receipt(receipt).split(" ", 1)[1]
        self.jobs: list[dict[str, Any]] = []
        self.claims: list[dict[str, Any]] = []

    def ensure_claim(self, claim: dict[str, Any]) -> bool:
        self.claims.append(claim)
        return True

    def run_job(self, job: dict[str, Any]) -> _Outcome:
        self.jobs.append(job)
        return _Outcome()


def test_the_job_builder_reads_the_receipt() -> None:
    contracts = parse_contracts(TOML)
    item = _item("site", "shop", "1" * 40, contracts)
    ref = "piceli-registry.piceli-system.svc:5000/shop/site@sha256:" + "9" * 64
    receipt = {
        "components": {
            "site": {
                "pull_ref": ref,
                "manifest_digest": "sha256:" + "9" * 64,
                "digest": item.digest,
            }
        }
    }
    assert decode_receipt(encode_receipt(receipt).split(" ", 1)[1]) == receipt
    cluster = _Cluster(receipt)
    settings = JobSettings(
        image="registry.example/builder@sha256:" + "f" * 64,
        namespace="piceli-system",
        registry_url="oci://piceli-registry.piceli-system.svc:5000/shop",
        node_registry="piceli-registry.piceli-system.svc:5000",
    )
    builder = JobBuilder(
        settings, {"shop": "https://git.example/shop.git"}, cluster, mirror_route=None
    )
    built = builder.build([item], checkout=None)  # type: ignore[arg-type]
    assert built["site"].pull_ref == ref and len(cluster.jobs) == 1
    with pytest.raises(CompositionError) as raised:
        builder.build([_item("legacy", "shop", "1" * 40, contracts)], checkout=None)  # type: ignore[arg-type]
    assert raised.value.code == "component-build-unsupported"


def test_the_job_run_builds_a_files_component(tmp_path: Path) -> None:
    source = tmp_path / "sources" / "shop"
    (source / "site").mkdir(parents=True)
    (source / "site" / "index.html").write_text("<h1>hi</h1>\n")
    (source / "VERSION").write_text("1\n")
    (source / "piceli.toml").write_text(
        '[component.site]\nbuild = { files = { "site" = "/srv/site", "VERSION" = "/srv/VERSION" } }\n'
    )
    pushed: list[str] = []

    def deliver(
        archive: Any, target: Any, grant: Any, *, node_registry: Any
    ) -> dict[str, Any]:
        pushed.append(target.repository)
        return {
            "state": "succeeded",
            "pull_ref": f"{node_registry}/{target.repository}@sha256:" + "8" * 64,
        }

    receipt = job_run_components(
        sources=tmp_path / "sources",
        items=[
            {
                "component": "site",
                "source": "shop",
                "digest": "sha256:" + "7" * 64,
                "repository": "shop/site",
            }
        ],
        platforms=("linux/arm64",),
        cache=tmp_path / "cache",
        out=tmp_path / "out",
        registry_url="oci://registry.example:5000",
        node_registry="registry.example:5000",
        deliver=deliver,
    )
    entry = receipt["components"]["site"]
    assert entry["pull_ref"].startswith("registry.example:5000/shop/site@sha256:")
    assert entry["digest"] == "sha256:" + "7" * 64 and pushed == ["shop/site"]
