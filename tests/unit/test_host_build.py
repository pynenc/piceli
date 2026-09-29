"""Host builds: no container engine, node facts in the plan, deterministic layers."""

from __future__ import annotations

import dataclasses
import io
import json
import tarfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from piceli.artifacts.build_spec import BuildSpecError, _default_runner
from piceli.artifacts.host_build import HostBuildGrant, HostBuildSpec
from piceli.artifacts.image_manifest import scan_image_stream
from tests.unit.host_build_support import (
    FACTS_4K,
    FACTS_16K,
    FACTS_AMD64,
    publish_base,
    write_project,
)
from tests.unit.test_registry_delivery import FakeRegistry


@pytest.fixture
def registry() -> Iterator[FakeRegistry]:
    fake = FakeRegistry()
    yield fake
    fake.close()


@pytest.fixture
def project(tmp_path: Path, registry: FakeRegistry) -> Path:
    base = publish_base(registry)
    return write_project(tmp_path / "project", registry.port, base)


class Recorder:
    """The real process runner, recording every argv it starts."""

    def __init__(self) -> None:
        self.argv: list[list[str]] = []
        self.env: list[dict[str, str]] = []

    def __call__(
        self, argv, cwd, limits, environment, expires_at, /, *, on_output=None
    ):  # type: ignore[no-untyped-def]
        self.argv.append(list(argv))
        self.env.append(dict(environment))
        return _default_runner(
            argv, cwd, limits, environment, expires_at, on_output=on_output
        )


def spec(path: Path, cache: Path, facts: Any = FACTS_16K) -> HostBuildSpec:
    return HostBuildSpec.from_toml(path).for_node(facts).with_cache_dir(cache)


def build(
    path: Path, cache: Path, out: Path, runner: Recorder | None = None
) -> dict[str, Any]:
    item = spec(path, cache)
    grant = HostBuildGrant(item.plan().plan_hash, time.time() + 600)
    return item.run(grant, out, runner=runner or Recorder()).to_dict()


def members(archive: Path, layer_digest: str) -> dict[str, tarfile.TarInfo]:
    import gzip

    with tarfile.open(archive) as outer:
        blob = outer.extractfile(f"blobs/sha256/{layer_digest[7:]}")
        assert blob is not None
        data = gzip.decompress(blob.read())
    with tarfile.open(fileobj=io.BytesIO(data)) as inner:
        return {info.name: info for info in inner}


def test_node_facts_reach_the_commands_and_the_plan_hash(
    project: Path, tmp_path: Path
) -> None:
    cache = tmp_path / "cache"
    big, small = spec(project, cache).plan(), spec(project, cache, FACTS_4K).plan()
    assert big.env == {"LG_PAGE": "14"} and small.env == {"LG_PAGE": "12"}
    assert big.plan_hash != small.plan_hash
    preview = big.preview()
    assert preview["node_facts"]["page_size"] == 16384
    assert preview["builder"]["kind"] == "host"
    assert preview["uses_container_engine"] is False
    assert ("aarch64/release/web", "/usr/local/bin/web") in big.images[0].target_files
    # Without facts there is no plan; another node's platform is refused.
    with pytest.raises(BuildSpecError) as error:
        HostBuildSpec.from_toml(project).plan()
    assert error.value.code == "node-facts-unavailable"
    pinned = HostBuildSpec.from_toml(project)
    pinned = dataclasses.replace(pinned, platform="linux/arm64")
    with pytest.raises(BuildSpecError) as error:
        pinned.for_node(FACTS_AMD64)
    assert error.value.code == "node-platform-mismatch"


def test_host_build_runs_only_declared_tools_and_no_container_engine(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No docker reachable at all: the build must not need it.
    monkeypatch.setenv("DOCKER_HOST", "unix:///nonexistent/docker.sock")
    runner = Recorder()
    receipt = build(project, tmp_path / "cache", tmp_path / "out", runner)
    assert [Path(argv[0]).name for argv in runner.argv] == ["sh"]
    assert all("docker" not in " ".join(argv) for argv in runner.argv)
    env = runner.env[0]
    assert "DOCKER_HOST" not in env
    assert env["CARGO_TARGET_DIR"] == str((tmp_path / "cache" / "target").absolute())
    assert env["LG_PAGE"] == "14"
    assert receipt["builder"]["kind"] == "host"
    assert receipt["node_facts"]["page_size"] == 16384
    web = receipt["outputs"]["images"]["web"]
    archive = tmp_path / "out" / web["archive"]
    with archive.open("rb") as stream:
        plan = scan_image_stream(stream)
    assert plan.config_digest == web["image_id"]
    assert plan.manifest_digest == web["digest"]
    assert plan.platform == "linux/arm64"
    config = json.loads(plan.config_body)
    assert config["config"]["Entrypoint"] == ["/usr/local/bin/web"]
    assert config["config"]["Cmd"] is None  # ENTRYPOINT clears the base Cmd
    assert config["config"]["User"] == "10001:10001"
    assert config["config"]["Env"] == ["PATH=/usr/bin:/bin"]
    origins = [layer["origin"] for layer in web["layers"]]
    assert origins == ["base", "build", "build", "build", "build"]
    files = {item["path"]: item for item in web["files"]}
    assert set(files) == {"/etc/web/page", "/srv/static.txt", "/usr/local/bin/web"}
    # The page size fact reached the compiled output.
    page_layer = web["layers"][2]["digest"]  # directories first, then by path
    entries = members(archive, page_layer)
    assert entries["etc/web/page"].size == 2
    dirs_layer = members(archive, web["layers"][1]["digest"])
    owned = dirs_layer["var/lib/web"]
    assert (owned.uid, owned.gid, owned.mode) == (10001, 10001, 0o700)
    binary = members(archive, web["layers"][4]["digest"])["usr/local/bin/web"]
    assert binary.mode == 0o755 and binary.mtime == 0


def test_a_one_file_change_changes_exactly_one_layer(
    project: Path, tmp_path: Path
) -> None:
    cache = tmp_path / "cache"
    first = build(project, cache, tmp_path / "out")["outputs"]["images"]["web"]
    stage = cache / "stage" / "shop" / "src"
    unchanged = (stage / "main.txt").stat().st_mtime_ns
    (project.parent / "src" / "static.txt").write_text("static 2\n")
    second = build(project, cache, tmp_path / "out")["outputs"]["images"]["web"]
    before = [layer["digest"] for layer in first["layers"]]
    after = [layer["digest"] for layer in second["layers"]]
    assert len(before) == len(after)
    assert sum(a != b for a, b in zip(before, after, strict=True)) == 1
    assert first["digest"] != second["digest"]
    # The stage is synchronised: an unchanged file keeps its mtime.
    assert (stage / "main.txt").stat().st_mtime_ns == unchanged
    assert (stage / "static.txt").read_text() == "static 2\n"


def test_the_same_inputs_give_the_same_digests_in_a_fresh_cache(
    project: Path, tmp_path: Path
) -> None:
    one = build(project, tmp_path / "a", tmp_path / "out-a")["outputs"]["images"]
    two = build(project, tmp_path / "b", tmp_path / "out-b")["outputs"]["images"]
    assert one["web"]["digest"] == two["web"]["digest"]
    assert one["web"]["image_id"] == two["web"]["image_id"]
    assert (tmp_path / "out-a" / one["web"]["archive"]).read_bytes() == (
        tmp_path / "out-b" / two["web"]["archive"]
    ).read_bytes()


def test_the_base_is_pulled_once_per_cache(
    project: Path, tmp_path: Path, registry: FakeRegistry
) -> None:
    cache = tmp_path / "cache"
    build(project, cache, tmp_path / "out")
    gets = [path for method, path in registry.requests if method == "GET"]
    build(project, cache, tmp_path / "out")
    again = [path for method, path in registry.requests if method == "GET"]
    blob_gets = [path for path in again[len(gets) :] if "/blobs/" in path]
    assert blob_gets == []


def test_a_missing_output_and_a_missing_tool_are_refused(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = project.read_text().replace(
        '"/etc/web/page"', '"/etc/web/page", "{rust_arch}/release/absent" = "/x"'
    )
    project.write_text(text)
    with pytest.raises(BuildSpecError) as error:
        build(project, tmp_path / "cache", tmp_path / "out")
    assert error.value.code == "host-output-missing"
    project.write_text(text.replace('tools = ["sh"]', 'tools = ["sh", "no-such-tool"]'))
    with pytest.raises(BuildSpecError) as error:
        spec(project, tmp_path / "cache").plan()
    assert error.value.code == "host-tool-missing"


def test_an_undeclared_command_or_reserved_env_is_refused(project: Path) -> None:
    text = project.read_text()
    project.write_text(text.replace('["sh", "-c",', '["bash", "-c",'))
    with pytest.raises(BuildSpecError, match="not a declared tool"):
        HostBuildSpec.from_toml(project)
    project.write_text(text.replace("LG_PAGE =", "CARGO_TARGET_DIR ="))
    with pytest.raises(BuildSpecError, match="cannot set"):
        HostBuildSpec.from_toml(project)


def test_the_grant_binds_the_plan(project: Path, tmp_path: Path) -> None:
    item = spec(project, tmp_path / "cache")
    grant = HostBuildGrant(
        spec(project, tmp_path, FACTS_4K).plan().plan_hash, time.time() + 60
    )
    with pytest.raises(BuildSpecError) as error:
        item.run(grant, tmp_path / "out")
    assert error.value.code == "plan-not-approved"
    assert not (tmp_path / "cache").exists() or not any(
        (tmp_path / "cache").rglob("*.oci.tar")
    )


def test_each_image_gets_an_sbom_and_provenance(project: Path, tmp_path: Path) -> None:
    (project.parent / "src" / "Cargo.lock").write_text(
        "version = 3\n\n"
        '[[package]]\nname = "web"\nversion = "0.1.0"\n\n'
        '[[package]]\nname = "itoa"\nversion = "1.0.11"\n'
        'source = "registry+https://github.com/rust-lang/crates.io-index"\n'
        'checksum = "' + "a" * 64 + '"\n'
    )
    project.write_text(
        project.read_text().replace(
            'include = ["*.txt"]', 'include = ["*.txt", "Cargo.lock"]'
        )
    )
    out = tmp_path / "out"
    web = build(project, tmp_path / "cache", out)["outputs"]["images"]["web"]
    sbom = json.loads((out / web["sbom"]["path"]).read_text())
    assert sbom["spdxVersion"] == "SPDX-2.3"
    names = {item["name"]: item for item in sbom["packages"]}
    assert names["example/web"]["versionInfo"] == web["digest"]
    assert (
        names["itoa"]["externalRefs"][0]["referenceLocator"] == "pkg:cargo/itoa@1.0.11"
    )
    assert {item["fileName"] for item in sbom["files"]} == {
        "/etc/web/page",
        "/srv/static.txt",
        "/usr/local/bin/web",
    }
    statement = json.loads((out / web["provenance"]["path"]).read_text())
    assert statement["subject"][0]["digest"]["sha256"] == web["digest"][7:]
    definition = statement["predicate"]["buildDefinition"]
    assert definition["internalParameters"]["node_facts"]["page_size"] == 16384
    assert definition["externalParameters"]["env"] == {"LG_PAGE": "14"}
    assert str(tmp_path) not in json.dumps(statement) + json.dumps(sbom)
    # The SBOM depends only on inputs and outputs.
    again = build(project, tmp_path / "cache-2", tmp_path / "out-2")["outputs"][
        "images"
    ]
    assert again["web"]["sbom"]["sha256"] == web["sbom"]["sha256"]
