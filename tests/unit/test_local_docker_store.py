"""Local import and run of OCI artifacts with either Docker image store.

A fake ``docker`` CLI (a script) loads, inspects, saves and runs images the
way Docker 29 does: with the containerd image store, ``docker image load``
names the image by a manifest digest it writes itself, and the config digest
is no reference ``docker image inspect`` or ``docker run`` accepts.
"""

from __future__ import annotations

import json
import socket
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from piceli.artifacts import ArtifactFile, BuildPlan, OciBuilder, SourcePin
from piceli.artifacts.local_import import DockerLocalImporter, LocalImportGrant
from piceli.artifacts.process import ToolPin
from piceli.artifacts.runnable import DockerLocalRunner, LocalRunGrant
from tests.unit.containerd_store import containerd_save

FAKE_DOCKER = """\
#!{python}
import hashlib, json, shutil, sys, tarfile
from pathlib import Path

STATE = Path({state!r})
state = json.loads(STATE.read_text())
args = sys.argv[3:]  # after --host unix://...
images = state["images"]

def save():
    STATE.write_text(json.dumps(state))

if args[:2] == ["image", "load"]:
    source = args[3]
    with tarfile.open(source) as archive:
        (entry,) = json.load(archive.extractfile("manifest.json"))
        config = archive.extractfile(entry["Config"]).read()
    digest = "sha256:" + hashlib.sha256(config).hexdigest()
    parsed = json.loads(config)
    local = digest
    image = {{"Os": parsed["os"], "Architecture": parsed["architecture"]}}
    if state["store"] == "containerd":
        local = "sha256:" + hashlib.sha256(b"manifest" + config).hexdigest()
        image["Descriptor"] = {{
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": local,
        }}
    kept = STATE.parent / (local[7:] + ".tar")
    shutil.copyfile(source, kept)
    images[local] = {{"Id": local, **image, "archive": str(kept)}}
    save()
    print("Loaded image ID: " + local)
elif args[:2] == ["image", "inspect"]:
    image = images.get(args[2])
    if image is None:
        print("Error: No such image", file=sys.stderr)
        sys.exit(1)
    print(json.dumps([{{k: v for k, v in image.items() if k != "archive"}}]))
elif args[:2] == ["image", "save"]:
    shutil.copyfile(images[args[4]]["archive"], args[3])
elif args[:1] == ["run"]:
    state["runs"].append(args)
    save()
elif args[:2] == ["container", "rm"]:
    pass
else:
    sys.exit(2)
"""


class FakeEngine:
    def __init__(self, directory: Path, store: str) -> None:
        self.state = directory / "state.json"
        self.state.write_text(json.dumps({"store": store, "images": {}, "runs": []}))
        script = directory / "docker"
        script.write_text(
            FAKE_DOCKER.format(python=sys.executable, state=str(self.state))
        )
        script.chmod(0o755)
        self.tool = ToolPin.capture(script)
        self.socket = directory / "docker.sock"
        self._listener = socket.socket(socket.AF_UNIX)
        self._listener.bind(str(self.socket))

    def read(self) -> dict:
        return json.loads(self.state.read_text())

    def close(self) -> None:
        self._listener.close()


@pytest.fixture(params=["classic", "containerd"])
def engine(request: pytest.FixtureRequest) -> Iterator[FakeEngine]:
    # A short directory: a Unix socket path is limited to about 100 bytes.
    with tempfile.TemporaryDirectory(prefix="pcl-", dir="/tmp") as directory:
        fake = FakeEngine(Path(directory), request.param)
        try:
            yield fake
        finally:
            fake.close()


def layout(tmp_path: Path) -> tuple[Path, str, str]:
    source = tmp_path / "app"
    source.write_bytes(b"#!/bin/sh\nexit 0\n")
    pin = SourcePin.capture(tmp_path, "app", public=True)
    plan = BuildPlan(
        "linux/arm64", (ArtifactFile(pin, "bin/app", True),), ("/bin/app",)
    )
    receipt = OciBuilder().build(plan, tmp_path, tmp_path / "oci")
    return tmp_path / "oci", receipt.manifest_digest, receipt.config_digest


def test_import_records_the_config_digest_and_the_engine_id(engine, tmp_path):
    path, manifest, config = layout(tmp_path)
    importer = DockerLocalImporter(engine.tool, engine.socket)
    receipt = importer.import_image(
        path, LocalImportGrant(manifest, engine.socket, time.time() + 30)
    )
    assert receipt["imported"] is True
    assert receipt["image_id"] == config
    (loaded,) = engine.read()["images"]
    if engine.read()["store"] == "classic":
        assert loaded == config and "local_id" not in receipt
    else:
        # Docker 29: the engine names the image by its own manifest digest.
        assert receipt["local_id"] == loaded != config
    assert "Loaded image ID" not in json.dumps(receipt)  # no process output


def test_imported_image_runs_by_its_engine_id(engine, tmp_path):
    path, manifest, config = layout(tmp_path)
    imported = DockerLocalImporter(engine.tool, engine.socket).import_image(
        path, LocalImportGrant(manifest, engine.socket, time.time() + 30)
    )
    runner = DockerLocalRunner(engine.tool, engine.socket)
    local_id = imported.get("local_id")
    grant = LocalRunGrant(
        config, "linux/arm64", engine.socket, time.time() + 30, local_id=local_id
    )
    result = runner.run_image(grant, arguments=("--check",))
    assert result["runtime_ready"] is True
    assert result["image_id"] == config
    (run,) = engine.read()["runs"]
    assert run[-2:] == [local_id or config, "--check"]
    inspected = runner.inspect_image(config, "linux/arm64", local_id=local_id)
    assert inspected["image_id"] == config


def test_containerd_store_needs_the_engine_id_to_run(tmp_path):
    with tempfile.TemporaryDirectory(prefix="pcl-", dir="/tmp") as directory:
        engine = FakeEngine(Path(directory), "containerd")
        try:
            path, manifest, config = layout(tmp_path)
            DockerLocalImporter(engine.tool, engine.socket).import_image(
                path, LocalImportGrant(manifest, engine.socket, time.time() + 30)
            )
            runner = DockerLocalRunner(engine.tool, engine.socket)
            # The config digest names no image in the containerd store.
            with pytest.raises(ValueError):
                runner.inspect_image(config, "linux/arm64")
            assert engine.read()["runs"] == []
        finally:
            engine.close()


def test_engine_id_of_another_image_is_refused(tmp_path):
    with tempfile.TemporaryDirectory(prefix="pcl-", dir="/tmp") as directory:
        engine = FakeEngine(Path(directory), "containerd")
        try:
            path, manifest, config = layout(tmp_path)
            DockerLocalImporter(engine.tool, engine.socket).import_image(
                path, LocalImportGrant(manifest, engine.socket, time.time() + 30)
            )
            # Another image, present in the engine under its own ID.
            other = containerd_save(("linux/arm64",), marker=b"other")
            kept = Path(directory) / "other.tar"
            kept.write_bytes(other.archive)
            state = engine.read()
            state["images"][other.local_id] = {
                **other.inspect("linux/arm64"),
                "archive": str(kept),
            }
            engine.state.write_text(json.dumps(state))
            runner = DockerLocalRunner(engine.tool, engine.socket)
            grant = LocalRunGrant(
                config,
                "linux/arm64",
                engine.socket,
                time.time() + 30,
                local_id=other.local_id,
            )
            with pytest.raises(ValueError, match="identity"):
                runner.run_image(grant)
            assert engine.read()["runs"] == []
        finally:
            engine.close()


def test_run_grant_validates_the_engine_id(tmp_path):
    with pytest.raises(ValueError):
        LocalRunGrant(
            "sha256:" + "1" * 64,
            "linux/arm64",
            Path("/run/docker.sock"),
            time.time() + 30,
            local_id="not-a-digest",
        )
