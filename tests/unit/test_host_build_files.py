"""``[[output.files]]`` (0.39): plain files from a host build, no image needed."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Any

import pytest

from piceli.artifacts.build_spec import BuildSpecError
from piceli.artifacts.cli import run_publish_files_command
from piceli.artifacts.host_build import HostBuildGrant, HostBuildSpec
from piceli.artifacts.multi_platform import MultiPlatformHostBuild
from piceli.artifacts.publish import PublishError
from piceli.artifacts.publish_files import FilesPlan
from piceli.pipeline.errors import PipelineError
from tests.unit.host_build_support import FACTS_AMD64
from tests.unit.test_host_build import Recorder

FILES_TOML = """
revision = "piceli.host-build.v1"
name = "tools"

[build]
tools = ["sh"]
platforms = ["linux/amd64", "linux/arm64"]
commands = [
  ["sh", "-c", "d=\\"$CARGO_TARGET_DIR/{rust_arch}/release\\" && mkdir -p \\"$d\\" && echo \\"app-cli {arch}\\" > \\"$d/app-cli\\" && echo \\"app-agent {arch}\\" > \\"$d/app-agent\\" && chmod 755 \\"$d/app-cli\\""],
]

[context.src]
path = "src"
include = ["*.txt"]

[[output.files]]
name = "bin"
target_files = { "{rust_arch}/release/app-cli" = "app-cli-{arch}", "{rust_arch}/release/app-agent" = "app-agent-{arch}" }
"""


def project(root: Path, text: str = FILES_TOML) -> Path:
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.txt").write_text("v1\n")
    path = root / "host-build.toml"
    path.write_text(text)
    return path


def build(spec: HostBuildSpec, out: Path) -> dict[str, Any]:
    item = MultiPlatformHostBuild(spec)
    grant = HostBuildGrant(item.plan().plan_hash, time.time() + 600)
    return item.run(grant, out, runner=Recorder()).to_dict()


def test_two_binaries_for_two_platforms_are_listed_with_sha256_and_size(
    tmp_path: Path,
) -> None:
    spec = HostBuildSpec.from_toml(project(tmp_path / "p")).with_cache_dir(
        tmp_path / "cache"
    )
    out = tmp_path / "out"
    receipt = build(spec, out)
    outputs = receipt["outputs"]
    assert outputs["images"] == {}
    details = outputs["file_details"]
    assert sorted(details) == [
        "linux-amd64/files/bin/app-agent-amd64",
        "linux-amd64/files/bin/app-cli-amd64",
        "linux-arm64/files/bin/app-agent-arm64",
        "linux-arm64/files/bin/app-cli-arm64",
    ]
    for path, entry in details.items():
        body = (out / path).read_bytes()
        sha = "sha256:" + hashlib.sha256(body).hexdigest()
        assert entry["sha256"] == sha == outputs["files"][path]
        assert entry["size"] == len(body)
        assert entry["output"] == "bin"
        assert entry["platform"] == "linux/" + entry["file"].rsplit("-", 1)[1]
        assert body.decode().endswith(entry["platform"].split("/")[1] + "\n")
    assert (out / "linux-arm64/files/bin/app-cli-arm64").stat().st_mode & 0o111


def test_publish_files_takes_the_receipts_files(tmp_path: Path) -> None:
    spec = HostBuildSpec.from_toml(project(tmp_path / "p")).with_cache_dir(
        tmp_path / "cache"
    )
    out = tmp_path / "out"
    receipt = out / "receipt.json"
    receipt.write_text(json.dumps(build(spec, out)))
    plan = FilesPlan.from_receipt(
        receipt, "oci://127.0.0.1:5000/example/tools", "1.0.0", "application/x.t"
    )
    assert [item.name for item in plan.files] == [
        "app-agent-amd64",
        "app-agent-arm64",
        "app-cli-amd64",
        "app-cli-arm64",
    ]
    details = json.loads(receipt.read_text())["outputs"]["file_details"]
    assert {item.sha256 for item in plan.files} == {
        entry["sha256"] for entry in details.values()
    }
    # A file changed after the build is refused.
    (out / "linux-amd64/files/bin/app-cli-amd64").write_text("other\n")
    with pytest.raises(PublishError) as error:
        FilesPlan.from_receipt(
            receipt, "oci://127.0.0.1:5000/example/tools", "1.0.0", "application/x.t"
        )
    assert error.value.code == "publish-input-changed"
    # The CLI: files or --receipt, not both.
    args = argparse.Namespace(
        files=[receipt],
        receipt=receipt,
        output_dir=None,
        to="oci://127.0.0.1:5000/example/tools",
        tag="1.0.0",
        artifact_type="application/x.t",
        move_tag=False,
        approve=None,
    )
    assert run_publish_files_command(args) == 2


def test_files_change_the_plan_only_of_specs_that_declare_them(
    tmp_path: Path,
) -> None:
    from tests.unit.host_build_support import HOST_TOML

    image_only = HOST_TOML.replace("@PORT@", "5000").replace(
        "@BASE@", "sha256:" + "0" * 64
    )
    path = project(tmp_path / "a", image_only)
    before = HostBuildSpec.from_toml(path)
    assert "files" not in before.to_dict()
    with_files = image_only + (
        '\n[[output.files]]\nname = "bin"\n'
        'target_files = { "{rust_arch}/release/web" = "web-{arch}" }\n'
    )
    path.write_text(with_files)
    after = HostBuildSpec.from_toml(path)
    assert after.images == before.images
    assert after.spec_sha256 != before.spec_sha256
    plan = after.for_node(FACTS_AMD64).plan()
    assert plan.files[0].target_files == (("x86_64/release/web", "web-amd64"),)
    assert plan.preview()["files"] == {"bin": ["files/bin/web-amd64"]}


@pytest.mark.parametrize(
    "table",
    [
        'name = "bin"\ntarget_files = { "a" = "dir/x" }',
        'name = "bin"\ntarget_files = { "../a" = "x" }',
        'name = "bin"\ntarget_files = { "a" = "x", "b" = "x" }',
        'name = "bin"\ntarget_files = {}',
        'name = "bin"\ntarget_files = { "a" = "x" }\nrepository = "r"',
    ],
)
def test_output_files_are_validated(tmp_path: Path, table: str) -> None:
    text = FILES_TOML.split("[[output.files]]")[0] + "[[output.files]]\n" + table
    with pytest.raises(BuildSpecError):
        HostBuildSpec.from_toml(project(tmp_path / "p", text))


def test_deploy_paths_refuse_a_spec_without_images(tmp_path: Path) -> None:
    from piceli.infra.pipelines import load_spec
    from piceli.pipeline.model import load_host_spec

    path = project(tmp_path / "p")
    with pytest.raises(PipelineError) as error:
        load_host_spec(path)
    assert "no [[output.image]]" in str(error.value)
    with pytest.raises(Exception, match=r"output\.files"):
        load_spec(path.read_bytes(), "builds/host-build.toml", repo="infra")
