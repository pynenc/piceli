"""Shared commands alongside the platform's (0.78.0, opt-in).

The commands synchronise through marker files, never through sleeps: a
shared command that waits for a file the platform command writes can only
finish when both ran at the same time. Every wait gives up after 30 s.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from piceli.artifacts.build_spec import BuildSpecError
from piceli.artifacts.host_build import HostBuildGrant, HostBuildSpec
from piceli.artifacts.multi_platform import MultiPlatformHostBuild
from tests.unit.host_build_support import FACTS_16K, publish_base
from tests.unit.test_registry_delivery import FakeRegistry

#: Seconds of slack between two clocks (a step's start and its runner's time).
SLACK = 0.25


def wait_for(path: str) -> str:
    """Shell: wait until ``path`` exists (30 s at most, then exit 9)."""
    return (
        f'i=0; while [ ! -e "{path}" ]; do i=$((i+1)); '
        f'[ "$i" -gt 600 ] && exit 9; sleep 0.05; done'
    )


MARKS = "$PICELI_SHARED_TARGET_DIR/../marks"
#: A platform command that marks it started, then builds the web binary.
PLATFORM = (
    f'mkdir -p "{MARKS}" && touch "{MARKS}/platform" && '
    'mkdir -p "$CARGO_TARGET_DIR/{rust_arch}/release" && '
    'cat src/main.txt > "$CARGO_TARGET_DIR/{rust_arch}/release/web"'
)
#: A shared command that finishes only after the platform command started,
#: and writes the site late, so an image assembled early would miss it.
SHARED = (
    wait_for(f"{MARKS}/platform")
    + ' && sleep 0.3 && mkdir -p "$CARGO_TARGET_DIR/site" && '
    'cat src/main.txt > "$CARGO_TARGET_DIR/site/app.wasm"'
)


def toml(
    commands: list[str],
    shared: list[str | tuple[str, list[str]]],
    *,
    alongside: bool = True,
    platforms: bool = False,
    other: bool = False,
    port: int,
    base: str,
) -> str:
    def argv(script: str) -> str:
        return f"['sh', '-c', '{script}']"

    def entry(item: str | tuple[str, list[str]]) -> str:
        if isinstance(item, str):
            return argv(item)
        script, images = item
        return f"{{ run = {argv(script)}, images = {images!r} }}"

    image = """
[[output.image]]
name = "{name}"
repository = "example/{name}"
base = {{ image = "127.0.0.1:{port}/base/runtime", digest = "{base}" }}
entrypoint = ["/usr/local/bin/web"]
target_files = {{ "{{rust_arch}}/release/web" = "/usr/local/bin/web" }}
shared_files = {{ "{shared}" = "/srv/site" }}
"""
    text = f"""
revision = "piceli.host-build.v1"
name = "shop"

[build]
tools = ["sh"]
{'platforms = ["linux/arm64", "linux/amd64"]' if platforms else ""}
{"shared_commands_alongside = true" if alongside else ""}
shared_commands = [{", ".join(entry(item) for item in shared)}]
commands = [{", ".join(argv(item) for item in commands)}]

[context.src]
path = "src"
include = ["*.txt"]
""" + image.format(name="web", port=port, base=base, shared="site")
    if other:
        text += image.format(name="other", port=port, base=base, shared="other")
    return text


@pytest.fixture
def registry() -> Iterator[FakeRegistry]:
    fake = FakeRegistry()
    yield fake
    fake.close()


@pytest.fixture
def write(tmp_path: Path, registry: FakeRegistry) -> Any:
    base = publish_base(registry, architectures=("amd64", "arm64"))
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.txt").write_text("v1\n")

    def write(commands: list[str], shared: list[Any], **options: Any) -> Path:
        path = root / "host-build.toml"
        path.write_text(
            toml(commands, shared, port=registry.port, base=base, **options)
        )
        return path

    return write


def run(path: Path, tmp_path: Path, **options: Any) -> dict[str, Any]:
    spec = (
        HostBuildSpec.from_toml(path)
        .for_node(FACTS_16K)
        .with_cache_dir(tmp_path / "cache")
    )
    grant = HostBuildGrant(spec.plan().plan_hash, time.time() + 600)
    return spec.run(grant, tmp_path / "out", **options).to_dict()


def step(receipt: dict[str, Any], kind: str) -> dict[str, Any]:
    (found,) = [item for item in receipt["steps"] if item["kind"] == kind]
    return found


def end(item: dict[str, Any]) -> float:
    return float(item["start_seconds"]) + float(item["seconds"])


def gone(pid_file: Path) -> bool:
    pid = int(pid_file.read_text().split()[0])
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def test_the_platform_command_starts_before_the_shared_one_ends(
    write: Any, tmp_path: Path
) -> None:
    log = tmp_path / "build.log"
    receipt = run(write([PLATFORM], [SHARED]), tmp_path, log=log)
    shared, command = step(receipt, "shared-1"), step(receipt, "command-1")
    image = step(receipt, "image-web")
    assert shared["state"] == command["state"] == "succeeded"
    assert command["start_seconds"] < end(shared)
    # Packaged only after both sides, with the shared output in it.
    assert image["start_seconds"] >= end(shared) - SLACK
    assert image["start_seconds"] >= end(command) - SLACK
    files = {item["path"] for item in receipt["outputs"]["images"]["web"]["files"]}
    assert "/srv/site/app.wasm" in files
    # The two sides keep their own target directories.
    cache = tmp_path / "cache" / "target"
    assert (cache / "shared" / "site" / "app.wasm").is_file()
    assert (cache / "arm64-16384" / "aarch64" / "release" / "web").is_file()
    assert not (cache / "shared" / "aarch64").exists()
    text = log.read_text()
    assert "run alongside the platform's commands" in text
    assert "### shared command 1/1 (sh) succeeded" in text


def test_without_the_field_the_shared_commands_still_run_first(
    write: Any, tmp_path: Path
) -> None:
    # The platform command reads the shared output, as the docs allow.
    reads = (
        'test -f "$PICELI_SHARED_TARGET_DIR/site/app.wasm" && '
        'mkdir -p "$CARGO_TARGET_DIR/{rust_arch}/release" && '
        'cat src/main.txt > "$CARGO_TARGET_DIR/{rust_arch}/release/web"'
    )
    writes = (
        'mkdir -p "$CARGO_TARGET_DIR/site" && '
        'cat src/main.txt > "$CARGO_TARGET_DIR/site/app.wasm"'
    )
    path = write([reads], [writes], alongside=False)
    spec = HostBuildSpec.from_toml(path)
    assert "shared_commands_alongside" not in spec.to_dict()
    assert "shared_commands_alongside" not in spec.for_node(FACTS_16K).plan().preview()
    receipt = run(path, tmp_path)
    kinds = [item["kind"] for item in receipt["steps"]]
    assert kinds == ["shared-1", "command-1", "image-web"]
    starts = [item["start_seconds"] for item in receipt["steps"]]
    assert starts == sorted(starts)
    assert (
        step(receipt, "command-1")["start_seconds"]
        >= end(step(receipt, "shared-1")) - SLACK
    )


def test_a_failing_shared_command_stops_the_platform_side_and_is_named(
    write: Any, tmp_path: Path
) -> None:
    pid = tmp_path / "platform.pid"
    platform = (
        f'mkdir -p "{MARKS}" && echo $$ > {pid} && touch "{MARKS}/platform" '
        "&& exec sleep 60"
    )
    fails = wait_for(f"{MARKS}/platform") + " && echo broken bundle && exit 3"
    log = tmp_path / "build.log"
    began = time.monotonic()
    with pytest.raises(BuildSpecError) as caught:
        run(write([platform], [fails]), tmp_path, log=log)
    assert time.monotonic() - began < 30
    error = caught.value
    assert error.code == "build-failed"
    assert "shared command 1/1 (sh)" in str(error)
    assert "broken bundle" in error.output_tail
    assert error.steps[-1]["kind"] == "shared-1"
    assert error.steps[-1]["state"] == "failed"
    stopped = [item for item in error.steps if item["kind"] == "command-1"]
    assert [item["state"] for item in stopped] == ["cancelled"]
    assert gone(pid)
    text = log.read_text()
    assert "stopped after" in text
    assert "because shared command 1/1 (sh) failed" in text


def test_a_failing_platform_command_stops_the_shared_side(
    write: Any, tmp_path: Path
) -> None:
    pid = tmp_path / "shared.pid"
    shared = (
        f'mkdir -p "{MARKS}" && echo $$ > {pid} && touch "{MARKS}/shared" '
        "&& exec sleep 60"
    )
    fails = wait_for(f"{MARKS}/shared") + " && exit 4"
    began = time.monotonic()
    with pytest.raises(BuildSpecError) as caught:
        run(write([fails], [shared]), tmp_path)
    assert time.monotonic() - began < 30
    error = caught.value
    assert error.code == "build-failed"
    assert "linux/arm64 command 1/1 (sh)" in str(error)
    assert error.steps[-1]["kind"] == "command-1"
    assert error.steps[-1]["exit_code"] == 4
    assert [item["state"] for item in error.steps if item["kind"] == "shared-1"] == [
        "cancelled"
    ]
    assert gone(pid)


def test_an_image_waits_only_for_the_shared_commands_that_name_it(
    write: Any, tmp_path: Path
) -> None:
    # shared-2 (for "other") finishes only once web's archive exists: web
    # cannot have waited for it.
    archive = tmp_path / "out" / "images" / "web.oci.tar"
    web = (
        'mkdir -p "$CARGO_TARGET_DIR/site" && '
        'cat src/main.txt > "$CARGO_TARGET_DIR/site/app.wasm"'
    )
    other = (
        wait_for(str(archive)) + ' && mkdir -p "$CARGO_TARGET_DIR/other" && '
        'cat src/main.txt > "$CARGO_TARGET_DIR/other/app.wasm"'
    )
    platform = (
        'mkdir -p "$CARGO_TARGET_DIR/{rust_arch}/release" && '
        'cat src/main.txt > "$CARGO_TARGET_DIR/{rust_arch}/release/web"'
    )
    path = write([platform], [(web, ["web"]), (other, ["other"])], other=True)
    receipt = run(path, tmp_path)
    assert step(receipt, "image-web")["start_seconds"] < end(step(receipt, "shared-2"))
    assert (
        step(receipt, "image-other")["start_seconds"]
        >= end(step(receipt, "shared-2")) - SLACK
    )
    files = {item["path"] for item in receipt["outputs"]["images"]["other"]["files"]}
    assert "/srv/site/app.wasm" in files


def test_a_multi_platform_build_overlaps_with_its_first_platform(
    write: Any, tmp_path: Path
) -> None:
    spec = HostBuildSpec.from_toml(
        write([PLATFORM], [SHARED], platforms=True)
    ).with_cache_dir(tmp_path / "cache")
    item = MultiPlatformHostBuild(spec)
    grant = HostBuildGrant(item.plan().plan_hash, time.time() + 600)
    receipt = item.run(grant, tmp_path / "out").to_dict()
    arm = receipt["builds"]["linux/arm64"]["steps"]
    amd = receipt["builds"]["linux/amd64"]["steps"]
    shared = next(item for item in arm if item["kind"] == "shared-1")
    assert next(item for item in arm if item["kind"] == "command-1")[
        "start_seconds"
    ] < end(shared)
    # Offsets count from the whole build: the second platform starts later.
    assert amd[0]["kind"] == "command-1"
    assert amd[0]["start_seconds"] >= end(shared) - SLACK
    assert all(isinstance(item["start_seconds"], float) for item in arm + amd)
    assert receipt["shared_commands"]["ran_with"] == "linux/arm64"


def test_the_field_is_checked_and_changes_the_plan_hash(
    write: Any, tmp_path: Path
) -> None:
    on = HostBuildSpec.from_toml(write([PLATFORM], [SHARED]))
    off = HostBuildSpec.from_toml(write([PLATFORM], [SHARED], alongside=False))
    assert on.to_dict()["shared_commands_alongside"] is True
    assert on.spec_sha256 != off.spec_sha256
    plan_on, plan_off = on.for_node(FACTS_16K).plan(), off.for_node(FACTS_16K).plan()
    assert plan_on.plan_hash != plan_off.plan_hash
    assert plan_on.preview()["shared_commands_alongside"] is True
    document: dict[str, Any] = {
        "revision": "piceli.host-build.v1",
        "name": "shop",
        "build": {"tools": ["sh"], "commands": [["sh", "-c", "true"]]},
        "context": {"src": {"path": "src", "include": ["*.txt"]}},
        "output": {"files": [{"name": "bin", "target_files": {"a": "a"}}]},
    }
    for build, message in (
        ({"shared_commands_alongside": True}, "needs build.shared_commands"),
        (
            {
                "shared_commands_alongside": "yes",
                "shared_commands": [["sh", "-c", "true"]],
            },
            "true or false",
        ),
    ):
        broken = {**document, "build": {**document["build"], **build}}
        with pytest.raises(BuildSpecError) as caught:
            HostBuildSpec.from_dict(broken, tmp_path)
        assert caught.value.code == "invalid-spec"
        assert message in str(caught.value)
