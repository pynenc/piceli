"""0.22: a host build installs the toolchains its spec pins before its commands.

``[build] toolchains`` names pin files relative to the stage root (or
versions); the versions are read at plan time and bind the plan hash, the
tools are installed once into ``<cache>/toolchains`` and put first on the
commands' ``PATH``, and the receipt and provenance record them. No network:
the downloads come from a fake opener.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from piceli.artifacts.build_spec import BuildSpecError
from piceli.artifacts.host_build import HostBuildGrant, HostBuildSpec
from piceli.dev import toolchains
from tests.unit.host_build_support import FACTS_16K, publish_base, write_project
from tests.unit.test_host_build import Recorder
from tests.unit.test_registry_delivery import FakeRegistry
from tests.unit.test_toolchains import BINARYEN_URL, NODE_ASSET, NODE_BASE, releases

PINS = 'toolchains = { node = "src/.nvmrc", binaryen = "123" }\n'


@pytest.fixture
def registry() -> Iterator[FakeRegistry]:
    fake = FakeRegistry()
    yield fake
    fake.close()


@pytest.fixture
def opener(monkeypatch: pytest.MonkeyPatch) -> Any:
    fake = releases()
    monkeypatch.setattr(toolchains, "_open_url", fake)
    monkeypatch.setattr(toolchains, "host_platform", lambda: ("linux", "amd64"))
    return fake


def project(root: Path, registry: FakeRegistry, pins: str = PINS) -> Path:
    path = write_project(root, registry.port, publish_base(registry))
    (root / "src" / ".nvmrc").write_text("v24.11.0\n")
    text = path.read_text()
    text = text.replace(
        'env = { LG_PAGE = "{page_size_log2}" }\n',
        'env = { LG_PAGE = "{page_size_log2}" }\n' + pins,
    )
    text = text.replace('include = ["*.txt"]', 'include = ["*.txt", ".nvmrc"]')
    text = text.replace(
        '["sh", "-c", "mkdir',
        '["sh", "-c", "node > \\"$CARGO_TARGET_DIR/node.txt\\" && '
        'wasm-opt > \\"$CARGO_TARGET_DIR/wasm-opt.txt\\" && mkdir',
    )
    path.write_text(text)
    return path


def spec(path: Path, cache: Path) -> HostBuildSpec:
    return HostBuildSpec.from_toml(path).for_node(FACTS_16K).with_cache_dir(cache)


def test_pinned_tools_reach_the_commands_and_the_records(
    tmp_path: Path, registry: FakeRegistry, opener: Any
) -> None:
    path = project(tmp_path / "p", registry)
    cache, out = tmp_path / "cache", tmp_path / "out"
    item = spec(path, cache)
    plan = item.plan()
    assert plan.toolchains == {
        "node": {"version": "24.11.0", "file": "src/.nvmrc"},
        "binaryen": {"version": "123", "file": None},
    }
    assert plan.preview()["toolchains"]["node"]["version"] == "24.11.0"
    runner = Recorder()
    receipt = item.run(
        HostBuildGrant(plan.plan_hash, time.time() + 600), out, runner=runner
    ).to_dict()
    root = (cache / "toolchains").absolute()
    node_bin = root / "node" / "24.11.0-linux-amd64" / "bin"
    binaryen_bin = root / "binaryen" / "123-linux-amd64" / "bin"
    assert runner.env[0]["PATH"].split(":")[:2] == [str(node_bin), str(binaryen_bin)]
    target = cache / "target" / "arm64-16384"
    assert (target / "node.txt").read_text() == "v24.11.0\n"
    assert (target / "wasm-opt.txt").read_text() == "wasm-opt version 123\n"
    recorded = receipt["toolchains"]
    assert recorded["node"]["version"] == "24.11.0"
    assert recorded["node"]["url"] == f"{NODE_BASE}/{NODE_ASSET}"
    assert recorded["binaryen"]["installed"] is True
    assert [step["kind"] for step in receipt["steps"]][:2] == [
        "toolchains",
        "command-1",
    ]
    web = receipt["outputs"]["images"]["web"]
    statement = json.loads((out / web["provenance"]["path"]).read_text())
    definition = statement["predicate"]["buildDefinition"]
    assert definition["externalParameters"]["toolchains"] == {
        "binaryen": {"version": "123", "file": None},
        "node": {"version": "24.11.0", "file": "src/.nvmrc"},
    }
    downloads = {
        item["name"]: item
        for item in definition["resolvedDependencies"]
        if str(item.get("name")).startswith("toolchain:")
    }
    assert downloads["toolchain:node"]["uri"] == f"{NODE_BASE}/{NODE_ASSET}"
    assert downloads["toolchain:binaryen"]["uri"] == BINARYEN_URL
    assert downloads["toolchain:node"]["digest"]["sha256"] == recorded["node"]["sha256"]
    assert str(tmp_path) not in json.dumps(statement)
    # The next build finds them installed: no download.
    calls = len(opener.calls)
    again = item.run(
        HostBuildGrant(plan.plan_hash, time.time() + 600), tmp_path / "out-2"
    ).to_dict()
    assert len(opener.calls) == calls
    assert again["toolchains"]["node"]["installed"] is False


def test_changing_a_pin_changes_the_plan_hash(
    tmp_path: Path, registry: FakeRegistry
) -> None:
    path = project(tmp_path / "p", registry)
    cache = tmp_path / "cache"
    before = spec(path, cache).plan().plan_hash
    (path.parent / "src" / ".nvmrc").write_text("24.12.0\n")
    assert spec(path, cache).plan().plan_hash != before
    path.write_text(path.read_text().replace('binaryen = "123"', 'binaryen = "124"'))
    assert spec(path, cache).plan().toolchains["binaryen"]["version"] == "124"
    # A spec without toolchains keeps its digest (nothing new in to_dict).
    plain = project(tmp_path / "plain", registry, pins="")
    assert "toolchains" not in HostBuildSpec.from_toml(plain).to_dict()


def test_a_pin_outside_the_contexts_or_unreadable_is_refused(
    tmp_path: Path, registry: FakeRegistry
) -> None:
    missing = project(
        tmp_path / "a", registry, 'toolchains = { node = "src/.node-version" }\n'
    )
    with pytest.raises(BuildSpecError) as raised:
        spec(missing, tmp_path / "cache").plan()
    assert raised.value.code == "host-toolchain-invalid"
    assert "src/.node-version" in str(raised.value)
    alias = project(tmp_path / "b", registry)
    (alias.parent / "src" / ".nvmrc").write_text("lts/iron\n")
    with pytest.raises(BuildSpecError) as raised:
        spec(alias, tmp_path / "cache").plan()
    assert raised.value.code == "host-toolchain-invalid"
    assert "src/.nvmrc" in str(raised.value)
    for pins in (
        'toolchains = { python = "3.12" }\n',
        'toolchains = { node = "../.nvmrc" }\n',
    ):
        with pytest.raises(BuildSpecError) as raised:
            HostBuildSpec.from_toml(project(tmp_path / "c", registry, pins))
        assert raised.value.code == "invalid-spec"
    rust = project(
        tmp_path / "d",
        registry,
        'toolchains = { rust = "src/rust-toolchain.toml" }\n',
    )
    rust.write_text(
        rust.read_text().replace(
            'env = { LG_PAGE = "{page_size_log2}" }',
            'env = { LG_PAGE = "{page_size_log2}", RUSTUP_TOOLCHAIN = "stable" }',
        )
    )
    with pytest.raises(BuildSpecError) as raised:
        HostBuildSpec.from_toml(rust)
    assert "RUSTUP_TOOLCHAIN" in str(raised.value)


def test_a_checksum_mismatch_fails_the_build_before_any_command(
    tmp_path: Path, registry: FakeRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(toolchains, "_open_url", releases(node_sha="e" * 64))
    monkeypatch.setattr(toolchains, "host_platform", lambda: ("linux", "amd64"))
    item = spec(project(tmp_path / "p", registry), tmp_path / "cache")
    runner = Recorder()
    with pytest.raises(BuildSpecError) as raised:
        item.run(
            HostBuildGrant(item.plan().plan_hash, time.time() + 600),
            tmp_path / "out",
            runner=runner,
        )
    assert raised.value.code == "host-toolchain-checksum"
    assert NODE_ASSET in str(raised.value)
    assert runner.argv == []


def test_a_pin_file_change_moves_the_image_change_key(tmp_path: Path) -> None:
    """A composition rebuilds an image when a pin file changes, even one in a
    context the image does not read."""
    from piceli.infra.pipelines import image_keys, load_spec

    text = """
revision = "piceli.host-build.v1"
name = "shop"

[build]
tools = ["sh"]
commands = [["sh", "-c", "true"]]
toolchains = { node = "pins/.nvmrc" }

[context.app]
include = ["*.txt"]

[context.pins]
include = [".nvmrc"]

[[output.image]]
name = "web"
repository = "example/web"
files = { "app/a.txt" = "/a.txt" }
contexts = ["app"]
"""

    class Sources:
        def __init__(self, nvmrc: str) -> None:
            self.nvmrc = nvmrc

        def fetch(self, source: str) -> None:
            return None

        def tree(self, key: str, commit: str, prefix: str) -> list[Any]:
            return [("a.txt", "100644", "a" * 40), (".nvmrc", "100644", self.nvmrc)]

    build = load_spec(text.encode(), "deploy/host-build.toml", repo="shop")

    def key(nvmrc: str) -> str:
        keys = image_keys(build, {"shop": "c" * 40}, Sources(nvmrc), platform="x")
        return keys["web"].key

    assert key("1" * 40) == key("1" * 40)
    assert key("1" * 40) != key("2" * 40)


def test_a_pinned_rust_also_gets_the_build_platforms_target(
    tmp_path: Path, registry: FakeRegistry
) -> None:
    """A development rust-toolchain.toml lists wasm32, not the linux target
    a host build links for: the plan adds it, per platform."""
    from piceli.artifacts.host_build import substitute

    path = project(
        tmp_path / "p",
        registry,
        pins='toolchains = { rust = "src/rust-toolchain.toml" }\n',
    )
    (path.parent / "src" / "rust-toolchain.toml").write_text(
        '[toolchain]\nchannel = "1.97.0"\ntargets = ["wasm32-unknown-unknown"]\n'
    )
    path.write_text(
        path.read_text().replace(
            'include = ["*.txt", ".nvmrc"]',
            'include = ["*.txt", ".nvmrc", "rust-toolchain.toml"]',
        )
    )
    native = f"{substitute('{rust_arch}', FACTS_16K)}-unknown-linux-gnu"
    rust = spec(path, tmp_path / "cache").plan().toolchains["rust"]
    assert rust["channel"] == "1.97.0"
    assert sorted(rust["targets"]) == sorted(["wasm32-unknown-unknown", native])
    # Listed already: not twice.
    (path.parent / "src" / "rust-toolchain.toml").write_text(
        f'[toolchain]\nchannel = "1.97.0"\ntargets = ["{native}"]\n'
    )
    assert spec(path, tmp_path / "cache").plan().toolchains["rust"]["targets"] == [
        native
    ]
