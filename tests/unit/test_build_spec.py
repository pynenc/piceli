"""Declarative containerized builds: spec, plan, grants, receipts and CLI.

Every test here uses a fake docker runner; nothing talks to a Docker daemon.
Set ``PICELI_DOCKER_TESTS=1`` to also run the real ``rust-hello`` example.
"""

import argparse
import dataclasses
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import time
import tomllib
from pathlib import Path
from typing import Any

import pytest

from piceli.artifacts.build_spec import (
    BUILD_RECEIPT_REVISION,
    BuildGrant,
    BuildReceipt,
    BuildSpec,
    BuildSpecError,
    DockerTool,
    add_build_spec_commands,
    run_build_spec_command,
)
from piceli.artifacts.process import ProcessLimits, ToolPin
from piceli.errors import ERRORS

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples/builds/rust-hello"
BUILDER = "sha256:" + "a" * 64
OTHER_BUILDER = "sha256:" + "b" * 64
BASE = "sha256:" + "c" * 64


class FakeDocker:
    """Interprets the argv a real ``docker`` would receive, deterministically."""

    def __init__(self, native: str = "linux/aarch64", fail: str | None = None):
        self.native = native
        self.fail = fail
        self.calls: list[list[str]] = []
        self.environments: list[dict[str, str]] = []
        self.images: dict[str, dict[str, Any]] = {}
        self.on_build: Any = None
        self.smoke_exit = 0
        self.smoke_state = "succeeded"
        self.salt = ""

    def __call__(
        self,
        argv: list[str],
        cwd: Path,
        limits: ProcessLimits,
        environment: dict[str, str],
        expires_at: float | None,
        /,
        *,
        on_output: Any = None,
    ) -> tuple[dict[str, Any], bytes, bytes]:
        self.calls.append(argv)
        self.environments.append(environment)
        ok = {"state": "succeeded", "exit_code": 0, "seconds": 0.01}
        command = argv[1:]
        if command[:1] == ["tag"]:
            self.images[command[2]] = self.images[command[1]]
            return ok, b"", b""
        if command[:2] == ["image", "rm"]:
            self.images.pop(command[-1], None)
            return ok, b"", b""
        if command[:1] == ["rm"]:
            return ok, b"", b""
        if command[:1] == ["run"]:
            if on_output is not None:
                on_output("stdout", b"self-test output\n")
            state = self.smoke_state
            if state == "succeeded" and self.smoke_exit:
                state = "failed"
            return {**ok, "state": state, "exit_code": self.smoke_exit}, b"", b""
        if command[:2] == ["buildx", "version"]:
            return ok, b"github.com/docker/buildx v0.99.0 fake\n", b""
        if command[:2] == ["context", "show"]:
            return ok, b"default\n", b""
        if command[:1] == ["info"]:
            return ok, self.native.encode() + b"\n", b""
        if command[:2] == ["image", "inspect"]:
            image = self.images.get(command[2])
            if image is None:
                return {**ok, "state": "failed", "exit_code": 1}, b"[]", b"no"
            return ok, json.dumps([image]).encode(), b""
        assert command[:2] == ["buildx", "build"], argv
        if self.on_build is not None:
            self.on_build()
        if on_output is not None:
            on_output("stderr", b"#1 [internal] load build definition\n#1 DONE")
            on_output("stderr", b" 0.0s\n")
        if self.fail is not None and self.fail in " ".join(argv):
            if on_output is not None:
                on_output("stderr", b"secret-token-in-log /private/path")
            return (
                {**ok, "state": "failed", "exit_code": 1},
                b"",
                b"secret-token-in-log /private/path",
            )
        option = dict(zip(command, [*command[1:], ""], strict=False))
        platform = option["--platform"]
        dockerfile = Path(option["--file"]).read_text()
        seed = hashlib.sha256((dockerfile + platform + self.salt).encode()).hexdigest()
        metadata: dict[str, Any] = {
            "containerimage.buildinfo": {
                "sources": [
                    {
                        "type": "docker-image",
                        "ref": f"docker.io/library/rust:1@{BUILDER}",
                        "pin": "sha256:"
                        + hashlib.sha256(platform.encode()).hexdigest(),
                    }
                ]
            }
        }
        if "--output" in option:
            destination = Path(option["--output"].split("dest=", 1)[1])
            stage = dockerfile.split("AS piceli-files", 1)[1].split("FROM", 1)[0]
            for target in re.findall(r'COPY --from=\S+ \["[^"]+", "/([^"]+)"\]', stage):
                path = destination / target
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(f"{seed}:{target}".encode())
        else:
            image_id = (
                "sha256:"
                + hashlib.sha256((seed + option["--target"]).encode()).hexdigest()
            )
            os_name, arch = platform.split("/", 1)
            self.images[option["--tag"]] = {
                "Id": image_id,
                "Os": os_name,
                "Architecture": arch,
            }
            metadata["containerimage.config.digest"] = image_id
            metadata["containerimage.digest"] = image_id
        Path(option["--metadata-file"]).write_text(json.dumps(metadata))
        return ok, b"", b"#1 building\n"


@pytest.fixture
def docker_tool(tmp_path: Path) -> DockerTool:
    binary = tmp_path / "bin" / "docker"
    binary.parent.mkdir()
    binary.write_bytes(b"#!/bin/sh\nexit 1\n")
    binary.chmod(0o755)
    return DockerTool(ToolPin.capture(binary), {"PATH": "/usr/bin", "HOME": "/h"})


def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    for name, content in {
        "Cargo.toml": "[package]\nname = 'svc'\n",
        "Cargo.lock": "version = 4\n",
        "src/main.rs": "fn main() {}\n",
        "target/release/svc": "stale binary",
        ".venv/big.bin": "x" * 1000,
    }.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return root


def document(**changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "revision": "piceli.build-spec.v1",
        "name": "svc",
        "builder": {"image": "docker.io/library/rust:1", "digest": BUILDER},
        "build": {
            "platforms": ["linux/arm64"],
            "command": ["cargo", "build", "--release", "--locked"],
            "env": {"CARGO_TERM_COLOR": "never"},
        },
        "context": {
            "app": {
                "path": "project",
                "include": ["Cargo.toml", "Cargo.lock", "src/**/*.rs"],
            }
        },
        "cache": {
            "registry": {"target": "/usr/local/cargo/registry", "id": "cargo"},
            "target": {"target": "/work/target"},
        },
        "output": {
            "file": [{"from": "target/release/svc", "to": "bin/svc"}],
            "image": [
                {
                    "name": "svc",
                    "repository": "example/svc",
                    "tag": "dev",
                    "base": {"image": "debian:bookworm-slim", "digest": BASE},
                    "files": {"bin/svc": "/usr/local/bin/svc"},
                    "entrypoint": ["/usr/local/bin/svc"],
                    "user": "65532:65532",
                }
            ],
        },
    }
    value.update(changes)
    return value


def make_spec(tmp_path: Path, **changes: Any) -> BuildSpec:
    project(tmp_path)
    return BuildSpec.from_dict(document(**changes), tmp_path)


def grant(**changes: Any) -> BuildGrant:
    value: dict[str, Any] = {"builder_digest": BUILDER, "expires_at": time.time() + 60}
    value.update(changes)
    return BuildGrant(**value)


# --- spec and plan --------------------------------------------------------------


def test_example_spec_parses_and_previews() -> None:
    spec = BuildSpec.from_toml(EXAMPLE / "build.toml")
    assert spec.platforms == ("linux/arm64",)
    assert spec.network == "none"
    preview = spec.plan().preview()
    assert preview["contexts"]["app"]["files"] == 3
    assert preview["contexts"]["app"]["bytes"] < 16 * 1024
    assert preview["requires_network_grant"] is False
    assert preview["builder"]["digest"].startswith("sha256:")


def test_preview_is_pure(tmp_path: Path) -> None:
    spec = make_spec(tmp_path)
    before = sorted(str(path) for path in tmp_path.rglob("*"))
    plan = spec.plan()
    preview = plan.preview()
    assert sorted(str(path) for path in tmp_path.rglob("*")) == before
    assert preview["plan_hash"] == spec.plan().plan_hash
    assert preview["outputs"] == {
        "files": ["bin/svc"],
        "images": {"svc": ["example/svc:dev"]},
    }
    # Private locations never appear in the preview: placeholders only.
    assert str(tmp_path) not in json.dumps(preview)


def test_generated_dockerfile_uses_pins_contexts_and_cache_mounts(
    tmp_path: Path,
) -> None:
    text = make_spec(tmp_path).plan().dockerfiles["linux/arm64"]
    assert f"FROM docker.io/library/rust:1@{BUILDER} AS piceli-build" in text
    assert 'COPY --from=app ["/", "/work/"]' in text
    assert "--network=none" in text
    assert "--mount=type=cache,id=cargo,target=/usr/local/cargo/registry" in text
    assert "id=piceli-svc-target-linux-arm64,target=/work/target" in text
    assert "cp -- /work/target/release/svc /piceli/out/bin/svc" in text
    assert f"FROM debian:bookworm-slim@{BASE} AS piceli-image-svc" in text
    assert 'ENTRYPOINT ["/usr/local/bin/svc"]' in text
    assert "find /piceli -exec touch -d @0 {} +" in text


def test_context_never_ships_target_or_venv(tmp_path: Path) -> None:
    plan = make_spec(tmp_path).plan()
    manifest = plan.contexts["app"]
    assert [item.path for item in manifest.files] == [
        "Cargo.lock",
        "Cargo.toml",
        "src/main.rs",
    ]


def test_invocation_argv_is_explicit(tmp_path: Path) -> None:
    invocations = make_spec(tmp_path).plan().invocations
    files, image = (item["argv"] for item in invocations)
    assert files[:3] == ["docker", "buildx", "build"]
    assert files[5:7] == ["--platform", "linux/arm64"]
    for flag in ("--provenance=false", "--sbom=false", "--network=none"):
        assert flag in files
    assert "SOURCE_DATE_EPOCH=0" in files
    assert "app=<staging>/contexts/app" in files
    assert "type=local,dest=<staging>/out/linux-arm64" in files
    assert image[image.index("--tag") + 1] == "example/svc:dev"
    assert "--load" in image


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("builder", "digest"), "latest"),
        (("builder", "image"), "rust:1@sha256:" + "a" * 64),
        (("build", "platforms"), ["windows/amd64"]),
        (("build", "network"), "host"),
        (("build", "env"), {"A": "x$(id)"}),
        (("build", "command"), []),
        (("context", "app", "include"), ["../escape"]),
        (("context", "app", "path"), "/abs"),
        (("cache", "target", "target"), "relative"),
        (("output", "file"), [{"from": "a", "to": "../x"}]),
        (("output", "file"), [{"from": "a", "to": ".env"}]),
        (("output", "image"), []),
    ],
)
def test_invalid_specs_rejected(
    tmp_path: Path, path: tuple[str, ...], value: Any
) -> None:
    doc = document()
    target = doc
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    if path == ("output", "image"):
        doc["output"] = {"image": []}
    with pytest.raises(BuildSpecError) as error:
        BuildSpec.from_dict(doc, tmp_path)
    assert error.value.code == "invalid-spec"


def test_unknown_fields_and_revision_rejected(tmp_path: Path) -> None:
    with pytest.raises(BuildSpecError):
        BuildSpec.from_dict(document(extra=1), tmp_path)
    with pytest.raises(BuildSpecError):
        BuildSpec.from_dict(document(revision="v0"), tmp_path)


def test_image_files_must_name_declared_outputs(tmp_path: Path) -> None:
    doc = document()
    doc["output"]["image"][0]["files"] = {"bin/other": "/x"}
    with pytest.raises(BuildSpecError):
        BuildSpec.from_dict(doc, tmp_path)


def test_spec_sha_changes_with_builder_digest(tmp_path: Path) -> None:
    first = make_spec(tmp_path)
    other = make_spec(
        tmp_path, builder={"image": "docker.io/library/rust:1", "digest": OTHER_BUILDER}
    )
    assert first.spec_sha256 != other.spec_sha256
    assert first.plan().plan_hash != other.plan().plan_hash


# --- run --------------------------------------------------------------------------

CONTRACT = {
    "revision",
    "spec_sha256",
    "builder",
    "platforms",
    "sources",
    "outputs",
    "started_at",
    "finished_at",
}


def test_run_writes_outputs_and_contract_receipt(
    tmp_path: Path, docker_tool: DockerTool
) -> None:
    spec = make_spec(tmp_path)
    fake = FakeDocker()
    receipt = spec.run(grant(), tmp_path / "dist", docker=docker_tool, runner=fake)
    data = receipt.to_dict()
    assert data.keys() >= CONTRACT
    assert data["revision"] == BUILD_RECEIPT_REVISION
    assert data["builder"]["image"] == "docker.io/library/rust:1"
    assert data["builder"]["digest"] == BUILDER
    assert data["builder"]["platform_manifests"]["linux/arm64"].startswith("sha256:")
    assert data["platforms"] == ["linux/arm64"]
    assert data["native_platform"] == "linux/arm64"
    assert data["emulated_platforms"] == []
    assert data["sources"] == []
    written = (tmp_path / "dist/bin/svc").read_bytes()
    assert data["outputs"]["files"] == {
        "bin/svc": "sha256:" + hashlib.sha256(written).hexdigest()
    }
    image = data["outputs"]["images"]["svc"]
    assert image["ref"] == "example/svc:dev"
    assert image["platform"] == "linux/arm64"
    assert image["image_id"].startswith("sha256:")
    assert image["digest"] is None  # classic store: no manifest digest
    assert data["tool"]["docker_sha256"] == docker_tool.tool.sha256
    assert data["tool"]["buildx_builder"] == "default"
    assert [step["kind"] for step in data["steps"]] == ["files", "image:svc"]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", data["started_at"])
    assert BuildReceipt.from_json(receipt.to_json()).files == receipt.files
    # No private locations, environment or process output in the receipt.
    text = receipt.to_json()
    assert str(tmp_path) not in text and "building" not in text
    for argv in fake.calls:
        assert argv[0] == str(docker_tool.tool.path)


def test_same_spec_twice_same_output_digests(
    tmp_path: Path, docker_tool: DockerTool
) -> None:
    spec = make_spec(tmp_path)
    fake = FakeDocker()
    first = spec.run(grant(), tmp_path / "a", docker=docker_tool, runner=fake)
    second = spec.run(grant(), tmp_path / "b", docker=docker_tool, runner=fake)
    assert first.to_dict()["outputs"] == second.to_dict()["outputs"]
    assert first.to_dict()["plan_hash"] == second.to_dict()["plan_hash"]


def test_builder_digest_change_visible_and_needs_new_approval(
    tmp_path: Path, docker_tool: DockerTool
) -> None:
    first = make_spec(tmp_path)
    other = BuildSpec.from_dict(
        document(
            builder={"image": "docker.io/library/rust:1", "digest": OTHER_BUILDER}
        ),
        tmp_path,
    )
    fake = FakeDocker()
    with pytest.raises(BuildSpecError) as error:
        other.run(grant(), tmp_path / "o", docker=docker_tool, runner=fake)
    assert error.value.code == "builder-not-approved"
    assert fake.calls == []
    a = first.run(grant(), tmp_path / "a", docker=docker_tool, runner=fake).to_dict()
    b = other.run(
        grant(builder_digest=OTHER_BUILDER),
        tmp_path / "b",
        docker=docker_tool,
        runner=fake,
    ).to_dict()
    assert (a["builder"]["digest"], b["builder"]["digest"]) == (BUILDER, OTHER_BUILDER)
    assert a["spec_sha256"] != b["spec_sha256"]
    assert a["outputs"]["files"] != b["outputs"]["files"]


def test_grant_checks(tmp_path: Path, docker_tool: DockerTool) -> None:
    spec = make_spec(tmp_path)
    fake = FakeDocker()
    with pytest.raises(BuildSpecError) as expired:
        spec.run(
            grant(expires_at=time.time() - 1),
            tmp_path / "o",
            docker=docker_tool,
            runner=fake,
        )
    assert expired.value.code == "grant-expired"
    with pytest.raises(BuildSpecError) as plan:
        spec.run(
            grant(plan_hash="sha256:" + "0" * 64),
            tmp_path / "o",
            docker=docker_tool,
            runner=fake,
        )
    assert plan.value.code == "plan-not-approved"
    networked = BuildSpec.from_dict(
        document(build={**document()["build"], "network": "default"}), tmp_path
    )
    with pytest.raises(BuildSpecError) as network:
        networked.run(grant(), tmp_path / "o", docker=docker_tool, runner=fake)
    assert network.value.code == "network-not-granted"
    assert fake.calls == []
    receipt = spec.run(
        grant(plan_hash=spec.plan().plan_hash),
        tmp_path / "o",
        docker=docker_tool,
        runner=fake,
    )
    assert receipt.to_dict()["state"] == "succeeded"
    with pytest.raises(BuildSpecError) as invalid:
        BuildGrant("latest", time.time() + 60)
    assert invalid.value.code == "invalid-grant"


def test_tool_pin_change_aborts(tmp_path: Path, docker_tool: DockerTool) -> None:
    spec = make_spec(tmp_path)
    fake = FakeDocker()
    fake.on_build = lambda: docker_tool.tool.path.write_bytes(b"swapped")
    with pytest.raises(ValueError, match="tool pin changed"):
        spec.run(grant(), tmp_path / "o", docker=docker_tool, runner=fake)


def test_minimal_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, docker_tool: DockerTool
) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "do-not-pass")
    monkeypatch.setenv("DOCKER_CONTEXT", "desktop")
    discovered = DockerTool.discover(docker_tool.tool.path)
    assert "AWS_SECRET_ACCESS_KEY" not in discovered.environment
    assert discovered.environment["DOCKER_CONTEXT"] == "desktop"
    assert discovered.environment["PATH"].startswith(str(docker_tool.tool.path.parent))
    fake = FakeDocker()
    make_spec(tmp_path).run(grant(), tmp_path / "o", docker=discovered, runner=fake)
    assert all("AWS_SECRET_ACCESS_KEY" not in env for env in fake.environments)


def test_emulated_platforms_and_multi_platform_keys(
    tmp_path: Path, docker_tool: DockerTool
) -> None:
    spec = make_spec(
        tmp_path,
        build={**document()["build"], "platforms": ["linux/arm64", "linux/amd64"]},
    )
    receipt = spec.run(
        grant(), tmp_path / "o", docker=docker_tool, runner=FakeDocker("linux/x86_64")
    ).to_dict()
    assert receipt["native_platform"] == "linux/amd64"
    assert receipt["emulated_platforms"] == ["linux/arm64"]
    assert set(receipt["outputs"]["files"]) == {
        "linux-arm64/bin/svc",
        "linux-amd64/bin/svc",
    }
    images = receipt["outputs"]["images"]
    assert images["svc/linux-arm64"]["ref"] == "example/svc:dev-arm64"
    assert images["svc/linux-amd64"]["platform"] == "linux/amd64"
    assert (tmp_path / "o/linux-amd64/bin/svc").is_file()


def test_build_failure_reports_steps_without_output(
    tmp_path: Path, docker_tool: DockerTool
) -> None:
    spec = make_spec(tmp_path)
    log = tmp_path / "logs" / "build.log"
    with pytest.raises(BuildSpecError) as error:
        spec.run(
            grant(),
            tmp_path / "o",
            docker=docker_tool,
            runner=FakeDocker(fail="piceli-files"),
            log=log,
        )
    assert error.value.code == "build-failed"
    assert error.value.steps[0]["state"] == "failed"
    assert "secret-token" not in json.dumps(error.value.steps)
    # The opt-in log keeps the raw output for the operator.
    assert b"secret-token-in-log" in log.read_bytes()
    assert not (tmp_path / "o/bin/svc").exists()


def test_undeclared_or_missing_outputs_rejected(
    tmp_path: Path, docker_tool: DockerTool
) -> None:
    spec = make_spec(tmp_path)

    class Extra(FakeDocker):
        def __call__(self, argv, cwd, limits, environment, expires_at, **kw):  # type: ignore[no-untyped-def]
            result = super().__call__(argv, cwd, limits, environment, expires_at, **kw)
            if "--output" in argv:
                dest = argv[argv.index("--output") + 1].split("dest=", 1)[1]
                Path(dest, "surprise").write_text("x")
            return result

    with pytest.raises(BuildSpecError) as extra:
        spec.run(grant(), tmp_path / "o", docker=docker_tool, runner=Extra())
    assert extra.value.code == "output-invalid"

    class Missing(FakeDocker):
        def __call__(self, argv, cwd, limits, environment, expires_at, **kw):  # type: ignore[no-untyped-def]
            result = super().__call__(argv, cwd, limits, environment, expires_at, **kw)
            if "--output" in argv:
                dest = argv[argv.index("--output") + 1].split("dest=", 1)[1]
                Path(dest, "bin/svc").unlink()
            return result

    with pytest.raises(BuildSpecError) as missing:
        spec.run(grant(), tmp_path / "o", docker=docker_tool, runner=Missing())
    assert missing.value.code == "output-invalid"


def test_loaded_image_must_match_platform(
    tmp_path: Path, docker_tool: DockerTool
) -> None:
    spec = make_spec(tmp_path)

    class Wrong(FakeDocker):
        def __call__(self, argv, cwd, limits, environment, expires_at, **kw):  # type: ignore[no-untyped-def]
            result = super().__call__(argv, cwd, limits, environment, expires_at, **kw)
            for image in self.images.values():
                image["Architecture"] = "amd64"
            return result

    with pytest.raises(BuildSpecError) as error:
        spec.run(grant(), tmp_path / "o", docker=docker_tool, runner=Wrong())
    assert error.value.code == "image-mismatch"


def test_context_change_between_plan_and_staging(
    tmp_path: Path, docker_tool: DockerTool
) -> None:
    spec = make_spec(tmp_path)
    fake = FakeDocker()
    original = fake.__call__

    def runner(argv, cwd, limits, environment, expires_at, **kw):  # type: ignore[no-untyped-def]
        if argv[1:3] == ["buildx", "version"]:
            (tmp_path / "project/src/main.rs").write_text("changed\n")
        return original(argv, cwd, limits, environment, expires_at, **kw)

    with pytest.raises(BuildSpecError) as error:
        spec.run(grant(), tmp_path / "o", docker=docker_tool, runner=runner)
    assert error.value.code == "context-changed"


# --- sources --------------------------------------------------------------------


def git(repo: Path, *args: str) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, env=env
    )


def sourced(tmp_path: Path) -> BuildSpec:
    repo = project(tmp_path)
    (repo / ".gitignore").write_text("target/\n.venv/\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "initial")
    (tmp_path / "inputs.toml").write_text(
        '[[source]]\nname = "app"\npath = "project"\n'
    )
    doc = document(inputs="inputs.toml")
    doc["context"]["app"] = {**doc["context"]["app"], "source": "app"}
    del doc["context"]["app"]["path"]
    return BuildSpec.from_dict(doc, tmp_path)


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_receipt_records_source_identities(
    tmp_path: Path, docker_tool: DockerTool
) -> None:
    spec = sourced(tmp_path)
    receipt = spec.run(
        grant(), tmp_path / "dist", docker=docker_tool, runner=FakeDocker()
    ).to_dict()
    [source] = receipt["sources"]
    assert source["name"] == "app"
    assert source["dirty"] is False
    assert re.fullmatch(r"[a-f0-9]{40}", source["commit"])
    assert receipt["inputs_spec_sha256"].startswith("sha256:")


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_unrelated_file_changed_during_build_is_provenance_only(
    tmp_path: Path, docker_tool: DockerTool
) -> None:
    spec = sourced(tmp_path)
    fake = FakeDocker()
    fake.on_build = lambda: (tmp_path / "project/README.md").write_text("drift\n")
    receipt = spec.run(
        grant(), tmp_path / "dist", docker=docker_tool, runner=fake
    ).to_dict()
    # The receipt keeps the identity from the start and records the move.
    [source] = receipt["sources"]
    assert source["dirty"] is False
    assert receipt["sources_changed_during_build"] == ["app"]
    assert (tmp_path / "dist/bin/svc").is_file()


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_staged_file_changed_during_build_fails(
    tmp_path: Path, docker_tool: DockerTool
) -> None:
    spec = sourced(tmp_path)
    fake = FakeDocker()
    fake.on_build = lambda: (tmp_path / "project/src/main.rs").write_text("x\n")
    with pytest.raises(BuildSpecError) as error:
        spec.run(grant(), tmp_path / "dist", docker=docker_tool, runner=fake)
    assert error.value.code == "context-changed"
    assert not (tmp_path / "dist/bin/svc").exists()


def test_staged_file_change_fails_without_sources(
    tmp_path: Path, docker_tool: DockerTool
) -> None:
    spec = make_spec(tmp_path)
    fake = FakeDocker()
    fake.on_build = lambda: (tmp_path / "project/Cargo.lock").chmod(0o755)
    with pytest.raises(BuildSpecError) as error:
        spec.run(grant(), tmp_path / "o", docker=docker_tool, runner=fake)
    assert error.value.code == "context-changed"


def test_unstaged_files_in_context_directory_may_change(
    tmp_path: Path, docker_tool: DockerTool
) -> None:
    spec = make_spec(tmp_path)
    fake = FakeDocker()

    def edit() -> None:
        (tmp_path / "project/README.md").write_text("new\n")
        (tmp_path / "project/src/extra.rs").write_text("// not staged\n")

    fake.on_build = edit
    receipt = spec.run(grant(), tmp_path / "o", docker=docker_tool, runner=fake)
    assert receipt.to_dict()["sources_changed_during_build"] == []


def test_spec_file_change_during_build_fails(
    tmp_path: Path, docker_tool: DockerTool
) -> None:
    path = write_spec(tmp_path)
    spec = BuildSpec.from_toml(path)
    fake = FakeDocker()
    original = path.read_text()
    # A comment is not a change to the declaration; a new tag is.
    fake.on_build = lambda: path.write_text(original + "\n# note\n")
    spec.run(grant(), tmp_path / "a", docker=docker_tool, runner=fake)
    fake.on_build = lambda: path.write_text(original.replace('"dev"', '"other"'))
    with pytest.raises(BuildSpecError) as error:
        spec.run(grant(), tmp_path / "b", docker=docker_tool, runner=fake)
    assert error.value.code == "spec-changed"


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_dirty_source_rejected_unless_allowed(
    tmp_path: Path, docker_tool: DockerTool
) -> None:
    spec = sourced(tmp_path)
    (tmp_path / "project/src/main.rs").write_text("fn main() { dirty() }\n")
    fake = FakeDocker()
    with pytest.raises(BuildSpecError) as error:
        spec.run(grant(), tmp_path / "dist", docker=docker_tool, runner=fake)
    assert error.value.code == "source-identity"
    assert not any(argv[1:3] == ["buildx", "build"] for argv in fake.calls)


# --- user Dockerfile mode ----------------------------------------------------------

USER_DOCKERFILE = """\
ARG RUST_IMAGE
ARG RUNTIME_IMAGE
FROM ${RUST_IMAGE} AS build
WORKDIR /src
COPY . .
RUN --mount=type=cache,target=/usr/local/cargo/registry cargo build --release
FROM ${RUNTIME_IMAGE} AS runtime
COPY --from=build /src/target/release/svc /usr/local/bin/svc
"""


def dockerfile_doc(text: str = USER_DOCKERFILE) -> dict[str, Any]:
    doc = document()
    doc["build"] = {
        "platforms": ["linux/arm64"],
        "dockerfile": "Containerfile",
        "context": "app",
        "builder_arg": "RUST_IMAGE",
        "args": {"PROFILE": "release"},
    }
    doc["images"] = {"RUNTIME_IMAGE": {"image": "debian:bookworm-slim", "digest": BASE}}
    doc["context"]["app"]["include"] = [
        *doc["context"]["app"]["include"],
        "Containerfile",
    ]
    del doc["cache"]
    doc["output"] = {
        "file": [
            {"stage": "build", "from": "/src/target/release/svc", "to": "bin/svc"}
        ],
        "image": [
            {
                "name": "svc",
                "repository": "example/svc",
                "tag": "dev",
                "target": "runtime",
            }
        ],
    }
    return doc


def test_user_dockerfile_mode(tmp_path: Path, docker_tool: DockerTool) -> None:
    project(tmp_path)
    (tmp_path / "project/Containerfile").write_text(USER_DOCKERFILE)
    spec = BuildSpec.from_dict(dockerfile_doc(), tmp_path)
    plan = spec.plan()
    text = plan.dockerfiles["linux/arm64"]
    assert text.startswith(USER_DOCKERFILE)
    assert (
        'FROM scratch AS piceli-files\nCOPY --from=build ["/src/target/release/svc", "/bin/svc"]'
        in text
    )
    argv = plan.invocations[1]["argv"]
    assert f"RUST_IMAGE=docker.io/library/rust:1@{BUILDER}" in argv
    assert f"RUNTIME_IMAGE=debian:bookworm-slim@{BASE}" in argv
    assert "PROFILE=release" in argv
    assert argv[-1] == "<staging>/contexts/app"
    assert "--build-context" not in argv
    receipt = spec.run(grant(), tmp_path / "o", docker=docker_tool, runner=FakeDocker())
    assert set(receipt.files) == {"bin/svc"}
    assert receipt.to_dict()["base_images"]["RUNTIME_IMAGE"]["digest"] == BASE


@pytest.mark.parametrize(
    "text",
    [
        "# syntax=docker/dockerfile:1\nFROM ${RUST_IMAGE} AS build\nFROM build AS runtime\n",
        "FROM rust:latest AS build\nFROM ${RUST_IMAGE} AS runtime\n",
        "FROM ${RUST_IMAGE} AS build\nFROM ${UNPINNED} AS runtime\n",
        "FROM ${RUST_IMAGE} AS build\nCOPY --from=alpine:3 /x /x\nFROM build AS runtime\n",
        "FROM ${RUST_IMAGE} AS build\nRUN --mount=type=bind,from=busybox,target=/b true\nFROM build AS runtime\n",
        "FROM ${RUST_IMAGE} AS build\nADD https://example.com/x /x\nFROM build AS runtime\n",
        "FROM debian@sha256:" + "d" * 64 + " AS build\nFROM build AS runtime\n",
    ],
)
def test_user_dockerfile_must_be_pinned(tmp_path: Path, text: str) -> None:
    project(tmp_path)
    (tmp_path / "project/Containerfile").write_text(text)
    spec = BuildSpec.from_dict(dockerfile_doc(), tmp_path)
    with pytest.raises(BuildSpecError) as error:
        spec.plan()
    assert error.value.code == "dockerfile-unpinned"


def test_dockerfile_mode_field_rules(tmp_path: Path) -> None:
    doc = dockerfile_doc()
    doc["build"]["args"] = {"RUST_IMAGE": "rust:latest"}
    with pytest.raises(BuildSpecError):
        BuildSpec.from_dict(doc, tmp_path)
    doc = dockerfile_doc()
    doc["build"]["command"] = ["make"]
    with pytest.raises(BuildSpecError):
        BuildSpec.from_dict(doc, tmp_path)


# --- CLI ---------------------------------------------------------------------------


def parse(*argv: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="piceli artifacts")
    add_build_spec_commands(parser.add_subparsers(dest="command"))
    return parser.parse_args(["build-spec", *argv])


def write_spec(tmp_path: Path, doc: dict[str, Any] | None = None) -> Path:
    project(tmp_path)
    doc = doc or document()
    lines = []

    def emit(prefix: str, value: dict[str, Any]) -> None:
        scalars = {
            k: v
            for k, v in value.items()
            if not isinstance(v, dict | list)
            or (isinstance(v, list) and not any(isinstance(i, dict) for i in v))
        }
        if prefix:
            lines.append(f"[{prefix}]")
        for key, item in scalars.items():
            lines.append(f"{key} = {json.dumps(item)}")
        for key, item in value.items():
            name = f"{prefix}.{key}" if prefix else key
            if isinstance(item, dict):
                emit(name, item)
            elif isinstance(item, list) and any(isinstance(i, dict) for i in item):
                for entry in item:
                    lines.append(f"[[{name}]]")
                    for k, v in entry.items():
                        lines.append(f"{k} = {toml_value(v)}")

    emit("", doc)
    path = tmp_path / "build.toml"
    path.write_text("\n".join(lines) + "\n")
    assert tomllib.loads(path.read_text())["name"] == doc["name"]
    return path


def toml_value(value: Any) -> str:
    if isinstance(value, dict):
        return (
            "{ "
            + ", ".join(f'"{k}" = {toml_value(v)}' for k, v in value.items())
            + " }"
        )
    return json.dumps(value)


def test_cli_preview(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    spec = write_spec(tmp_path)
    assert run_build_spec_command(parse("preview", "--spec", str(spec))) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["revision"] == "piceli.build-preview.v1"
    assert preview["contexts"]["app"]["files"] == 3


def test_cli_run_writes_receipt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], docker_tool: DockerTool
) -> None:
    spec = write_spec(tmp_path)
    out = tmp_path / "out" / "receipt.json"
    code = run_build_spec_command(
        parse(
            "run", "--spec", str(spec), "--approve-builder", BUILDER, "--out", str(out)
        ),
        runner=FakeDocker(),
        docker=docker_tool,
    )
    assert code == 0
    receipt = json.loads(out.read_text())
    assert receipt.keys() >= CONTRACT
    assert json.loads(capsys.readouterr().out) == receipt
    assert (tmp_path / "out/bin/svc").is_file()


def test_cli_rejections_use_fixed_codes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], docker_tool: DockerTool
) -> None:
    spec = write_spec(tmp_path)
    out = tmp_path / "receipt.json"
    code = run_build_spec_command(
        parse(
            "run",
            "--spec",
            str(spec),
            "--approve-builder",
            OTHER_BUILDER,
            "--out",
            str(out),
        ),
        runner=FakeDocker(),
        docker=docker_tool,
    )
    captured = capsys.readouterr()
    assert code == 2
    assert json.loads(captured.out) == {
        "state": "rejected",
        "reason": "builder-not-approved",
        "message": ERRORS["builder-not-approved"].title,
    }
    assert "[builder-not-approved]" in captured.err
    assert not out.exists()
    code = run_build_spec_command(
        parse("preview", "--spec", str(tmp_path / "secret-dir" / "missing.toml"))
    )
    captured = capsys.readouterr()
    assert code == 2 and "secret-dir" not in captured.out + captured.err
    assert json.loads(captured.out)["reason"] == "spec-unreadable"


def test_cli_build_failure_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], docker_tool: DockerTool
) -> None:
    spec = write_spec(tmp_path)
    code = run_build_spec_command(
        parse(
            "run",
            "--spec",
            str(spec),
            "--approve-builder",
            BUILDER,
            "--out",
            str(tmp_path / "r.json"),
        ),
        runner=FakeDocker(fail="piceli-files"),
        docker=docker_tool,
    )
    captured = capsys.readouterr()
    error = captured.err
    assert code == 1
    assert error.splitlines()[:2] == [
        "[piceli] 1/2 linux/arm64 files: running",
        "[piceli] 1/2 linux/arm64 files: failed in 0.01s",
    ]
    assert "failed: Build step failed [build-failed]" in error
    body = json.loads(captured.out)
    assert body["state"] == "failed" and body["reason"] == "build-failed"
    for text in (error, captured.out):
        assert "secret-token" not in text and str(tmp_path) not in text


# --- build log, tag templates, smoke ----------------------------------------------


def image_doc(**image: Any) -> dict[str, Any]:
    doc = document()
    doc["output"]["image"][0].update(image)
    return doc


def test_log_streams_during_the_build_and_never_reaches_receipt(
    tmp_path: Path, docker_tool: DockerTool
) -> None:
    spec = make_spec(tmp_path)
    log = tmp_path / "logs" / "build.log"
    fake = FakeDocker()
    seen: list[bytes] = []
    fake.on_build = lambda: seen.append(log.read_bytes())
    progress: list[str] = []
    raw: list[bytes] = []
    receipt = spec.run(
        grant(),
        tmp_path / "o",
        docker=docker_tool,
        runner=fake,
        log=log,
        progress=progress.append,
        raw_output=raw.append,
    )
    # While the second build step runs, the first step's output is on disk.
    assert b"### linux/arm64 files started" in seen[0]
    assert b"### linux/arm64 files succeeded" in seen[1]
    assert b"#1 DONE 0.0s\n" in seen[1]
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    assert progress == [
        "[piceli] 1/2 linux/arm64 files: running",
        "[piceli] 1/2 linux/arm64 files: succeeded in 0.01s",
        "[piceli] 2/2 linux/arm64 image:svc: running",
        "[piceli] 2/2 linux/arm64 image:svc: succeeded in 0.01s",
        "[piceli] drift check: 3 staged file(s) unchanged",
    ]
    assert b"".join(raw).count(b"#1 DONE") == 2
    assert "load build definition" not in receipt.to_json()


def test_cli_progress_modes(
    tmp_path: Path, capfd: pytest.CaptureFixture[str], docker_tool: DockerTool
) -> None:
    spec = write_spec(tmp_path)
    for mode, expect_progress, expect_raw in (
        ("quiet", False, False),
        ("steps", True, False),
        ("plain", True, True),
    ):
        code = run_build_spec_command(
            parse(
                "run",
                "--spec",
                str(spec),
                "--approve-builder",
                BUILDER,
                "--out",
                str(tmp_path / mode / "r.json"),
                "--progress",
                mode,
            ),
            runner=FakeDocker(),
            docker=docker_tool,
        )
        err = capfd.readouterr().err
        assert code == 0
        assert ("[piceli] 1/2" in err) is expect_progress
        assert ("load build definition" in err) is expect_raw


def test_tag_template_resolves_from_image_id(
    tmp_path: Path, docker_tool: DockerTool
) -> None:
    project(tmp_path)
    spec = BuildSpec.from_dict(image_doc(tag="dev-{image_id:12}"), tmp_path)
    plan = spec.plan()
    assert plan.preview()["outputs"]["images"] == {
        "svc": ["example/svc:dev-{image_id:12}"]
    }
    [argv] = [item["argv"] for item in plan.invocations if item["kind"] == "image:svc"]
    assert "example/svc:piceli-pending-<nonce>" in argv
    fake = FakeDocker()
    first = spec.run(grant(), tmp_path / "a", docker=docker_tool, runner=fake)
    image = first.images["svc"]
    assert image["ref"] == f"example/svc:dev-{image['image_id'][7:19]}"
    assert not any(ref.startswith("example/svc:piceli-pending-") for ref in fake.images)
    # Different content gives a different tag; both images stay addressable.
    fake.salt = "changed"
    second = spec.run(grant(), tmp_path / "b", docker=docker_tool, runner=fake)
    other = second.images["svc"]
    assert other["ref"] != image["ref"]
    assert fake.images[image["ref"]]["Id"] == image["image_id"]
    assert fake.images[other["ref"]]["Id"] == other["image_id"]


def test_tag_template_multi_platform_and_full_width(
    tmp_path: Path, docker_tool: DockerTool
) -> None:
    project(tmp_path)
    doc = image_doc(tag="{image_id}")
    doc["build"]["platforms"] = ["linux/arm64", "linux/amd64"]
    spec = BuildSpec.from_dict(doc, tmp_path)
    receipt = spec.run(grant(), tmp_path / "o", docker=docker_tool, runner=FakeDocker())
    for key, arch in (("svc/linux-arm64", "arm64"), ("svc/linux-amd64", "amd64")):
        image = receipt.images[key]
        assert image["ref"] == f"example/svc:{image['image_id'][7:]}-{arch}"


@pytest.mark.parametrize(
    "tag",
    ["{image_id:3}", "{image_id:65}", "{spec}", "{image_id", "dev}", "-{image_id:8}"],
)
def test_invalid_tag_templates_rejected(tmp_path: Path, tag: str) -> None:
    with pytest.raises(BuildSpecError) as error:
        BuildSpec.from_dict(image_doc(tag=tag), tmp_path)
    assert error.value.code == "invalid-spec"


def test_literal_specs_keep_their_digest(tmp_path: Path) -> None:
    # Adding smoke/templates must not move the digest of existing specs.
    spec = make_spec(tmp_path)
    assert "smoke" not in spec.to_dict()["images"][0]
    smoked = BuildSpec.from_dict(
        image_doc(smoke={"command": ["--self-test"]}), tmp_path
    )
    assert smoked.to_dict()["images"][0]["smoke"] == {
        "command": ["--self-test"],
        "expect_exit": 0,
        "timeout_seconds": 60.0,
    }
    assert smoked.spec_sha256 != spec.spec_sha256


def test_smoke_check_runs_isolated_and_is_recorded(
    tmp_path: Path, docker_tool: DockerTool
) -> None:
    project(tmp_path)
    spec = BuildSpec.from_dict(
        image_doc(smoke={"command": ["--self-test"], "timeout_seconds": 30}),
        tmp_path,
    )
    fake = FakeDocker()
    log = tmp_path / "build.log"
    receipt = spec.run(
        grant(), tmp_path / "o", docker=docker_tool, runner=fake, log=log
    ).to_dict()
    [run] = [argv for argv in fake.calls if argv[1] == "run"]
    image_id = receipt["outputs"]["images"]["svc"]["image_id"]
    assert run[-2:] == [image_id, "--self-test"]
    for flag in (
        ["--network", "none"],
        ["--pull", "never"],
        ["--platform", "linux/arm64"],
        ["--cap-drop", "ALL"],
        ["--security-opt", "no-new-privileges"],
    ):
        index = run.index(flag[0])
        assert run[index : index + 2] == flag
    assert "--read-only" in run and "--rm" in run
    assert receipt["steps"][-1] == {
        "platform": "linux/arm64",
        "kind": "smoke:svc",
        "state": "succeeded",
        "exit_code": 0,
        "seconds": 0.01,
    }
    assert b"self-test output" in log.read_bytes()
    assert "self-test output" not in json.dumps(receipt)


def test_smoke_expected_nonzero_exit(tmp_path: Path, docker_tool: DockerTool) -> None:
    project(tmp_path)
    spec = BuildSpec.from_dict(
        image_doc(smoke={"command": [], "expect_exit": 3}), tmp_path
    )
    fake = FakeDocker()
    fake.smoke_exit = 3
    receipt = spec.run(grant(), tmp_path / "o", docker=docker_tool, runner=fake)
    assert receipt.to_dict()["steps"][-1]["state"] == "succeeded"
    [run] = [argv for argv in fake.calls if argv[1] == "run"]
    assert run[-1].startswith("sha256:")


def test_failing_smoke_check_rejects_the_receipt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], docker_tool: DockerTool
) -> None:
    doc = image_doc(smoke={"command": ["--self-test"]})
    path = write_spec(tmp_path, doc)
    fake = FakeDocker()
    fake.smoke_exit = 1
    out = tmp_path / "r.json"
    code = run_build_spec_command(
        parse(
            "run", "--spec", str(path), "--approve-builder", BUILDER, "--out", str(out)
        ),
        runner=fake,
        docker=docker_tool,
    )
    body = json.loads(capsys.readouterr().out)
    assert code == 1 and not out.exists()
    assert body["state"] == "failed" and body["reason"] == "smoke-failed"
    assert body["steps"][-1]["kind"] == "smoke:svc"
    assert body["steps"][-1]["state"] == "failed"
    assert not (tmp_path / "bin/svc").exists()


def test_smoke_timeout_removes_the_container(
    tmp_path: Path, docker_tool: DockerTool
) -> None:
    project(tmp_path)
    spec = BuildSpec.from_dict(image_doc(smoke={"command": ["--hang"]}), tmp_path)
    fake = FakeDocker()
    fake.smoke_state = "timed-out"
    with pytest.raises(BuildSpecError) as error:
        spec.run(grant(), tmp_path / "o", docker=docker_tool, runner=fake)
    assert error.value.code == "smoke-timed-out"
    [run] = [argv for argv in fake.calls if argv[1] == "run"]
    name = run[run.index("--name") + 1]
    assert [argv for argv in fake.calls if argv[1] == "rm"] == [
        [str(docker_tool.tool.path), "rm", "--force", name]
    ]


@pytest.mark.parametrize(
    "smoke",
    [
        {"command": "--self-test"},
        {"command": ["--self-test"], "expect_exit": 256},
        {"command": ["--self-test"], "expect_exit": True},
        {"command": ["--self-test"], "timeout_seconds": 0},
        {"command": ["--self-test"], "timeout_seconds": 601},
        {"command": ["--self-test"], "network": "default"},
    ],
)
def test_invalid_smoke_rejected(tmp_path: Path, smoke: dict[str, Any]) -> None:
    with pytest.raises(BuildSpecError) as error:
        BuildSpec.from_dict(image_doc(smoke=smoke), tmp_path)
    assert error.value.code == "invalid-spec"


# --- opt-in real docker ------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("PICELI_DOCKER_TESTS") != "1",
    reason="set PICELI_DOCKER_TESTS=1 to run real docker buildx builds",
)
@pytest.mark.timeout(1800)
def test_rust_hello_example_is_reproducible(tmp_path: Path) -> None:
    from piceli.artifacts.build_spec import _default_runner

    spec = BuildSpec.from_toml(EXAMPLE / "build.toml")
    unrelated = EXAMPLE / "UNRELATED-EDIT.md"
    edits: list[str] = []

    def editing_runner(argv, cwd, limits, environment, expires_at, **kw):  # type: ignore[no-untyped-def]
        # An unrelated file of the same source changes while the build runs.
        if argv[1:3] == ["buildx", "build"] and not edits:
            unrelated.write_text("edited during the build\n")
            edits.append("edited")
        return _default_runner(argv, cwd, limits, environment, expires_at, **kw)

    log = tmp_path / "build.log"
    try:
        first = spec.run(
            BuildGrant(spec.builder.digest, time.time() + 1800),
            tmp_path / "a",
            runner=editing_runner,
            log=log,
        ).to_dict()
    finally:
        unrelated.unlink(missing_ok=True)
    assert first["sources_changed_during_build"] == ["piceli"]
    assert b"### linux/arm64 image:rust-hello succeeded" in log.read_bytes()
    second = spec.run(
        BuildGrant(spec.builder.digest, time.time() + 1800), tmp_path / "b"
    ).to_dict()
    assert first["outputs"] == second["outputs"]
    image = first["outputs"]["images"]["rust-hello"]
    assert image["platform"] == "linux/arm64"
    assert image["ref"] == f"piceli-examples/rust-hello:{image['image_id'][7:19]}"
    assert first["steps"][-1]["kind"] == "smoke:rust-hello"
    assert first["steps"][-1]["state"] == "succeeded"
    smoke = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            image["ref"],
            "--self-test",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert smoke.returncode == 0 and "self-test ok" in smoke.stdout
    # A smoke check that expects another exit code rejects the build.
    [declared] = spec.images
    assert declared.smoke is not None
    failing = dataclasses.replace(
        spec,
        images=(
            dataclasses.replace(
                declared, smoke=dataclasses.replace(declared.smoke, expect_exit=3)
            ),
        ),
        origin=None,
    )
    with pytest.raises(BuildSpecError) as error:
        failing.run(BuildGrant(spec.builder.digest, time.time() + 1800), tmp_path / "c")
    assert error.value.code == "smoke-failed"
