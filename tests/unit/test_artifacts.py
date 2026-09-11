"""Offline artifact and authorized process tests, without an infrastructure client."""
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import threading
import time

import pytest

from piceli.artifacts import (
    ArtifactFile,
    BuildPlan,
    DockerArchiveOciBuilder,
    OciBuilder,
    RunnableBuild,
    SourcePin,
    inspect_oci,
    inspect_runnable_oci,
    unpack_oci_archive,
)
from piceli.artifacts.local_import import DockerLocalImporter, LocalImportGrant
from piceli.artifacts.process import (
    BuildCommand,
    ExecutionGrant,
    ProcessLimits,
    ToolPin,
)


def plan_at(tmp_path):
    source = tmp_path / "app"
    source.write_bytes(b"#!/bin/sh\nexit 0\n")
    pin = SourcePin.capture(tmp_path, "app", public=True)
    return BuildPlan(
        "linux/arm64", (ArtifactFile(pin, "bin/app", True),), ("/bin/app",)
    )


def test_deterministic_oci_bytes_and_public_receipts(tmp_path):
    plan = plan_at(tmp_path)
    one = OciBuilder().build(plan, tmp_path, tmp_path / "one")
    two = OciBuilder().build(plan, tmp_path, tmp_path / "two")
    assert one == two
    assert inspect_oci(tmp_path / "one").manifest_digest == one.manifest_digest
    first = {
        str(p.relative_to(tmp_path / "one")): p.read_bytes()
        for p in (tmp_path / "one").rglob("*")
        if p.is_file()
    }
    second = {
        str(p.relative_to(tmp_path / "two")): p.read_bytes()
        for p in (tmp_path / "two").rglob("*")
        if p.is_file()
    }
    assert first == second
    assert one.plan_hash == plan.plan_hash
    assert one.summary()["pushed"] is False
    with pytest.raises(ValueError):
        OciBuilder().build(plan, tmp_path, tmp_path / "one")


def test_planning_import_and_cli_preview_have_no_client_or_process_side_effect(
    tmp_path,
):
    plan = plan_at(tmp_path)
    spec = tmp_path / "plan.json"
    spec.write_text(json.dumps(asdict(plan)))
    script = r"""
import socket,subprocess,sys
def trap(*a,**kw): raise AssertionError("unexpected side effect")
socket.socket=trap
subprocess.Popen=trap
from piceli.artifacts import BuildPlan
from piceli.artifacts.cli import main
from piceli.k8s.ops.executor import PlanExecutor
assert "kubernetes.config" not in sys.modules
assert main(["preview","--plan",sys.argv[1]]) == 0
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(spec)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["network"] is False
    cli = subprocess.run(
        [sys.executable, "-m", "piceli", "artifacts", "preview", "--plan", str(spec)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert cli.returncode == 0, cli.stderr
    assert json.loads(cli.stdout) == plan.preview()


@pytest.mark.parametrize(
    "path",
    [
        "../escape",
        "/absolute",
        ".env",
        ".env.production",
        ".kube/config",
        "secrets/token",
        "cert.key",
        "folder/../app",
        "folder\\app",
    ],
)
def test_private_and_traversal_paths_rejected(path):
    with pytest.raises(ValueError):
        SourcePin(path, "sha256:" + "0" * 64, 0, True)


def test_pin_drift_cancellation_symlinks_and_no_partial_artifacts(tmp_path):
    plan = plan_at(tmp_path)
    (tmp_path / "app").write_text("changed")
    with pytest.raises(ValueError, match="pin|byte"):
        OciBuilder().build(plan, tmp_path, tmp_path / "changed")
    assert not (tmp_path / "changed").exists()
    assert not list(tmp_path.glob(".piceli-oci-*"))
    plan = plan_at(tmp_path)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(InterruptedError):
        OciBuilder().build(plan, tmp_path, tmp_path / "cancelled", cancel=cancel)
    assert not (tmp_path / "cancelled").exists()
    (tmp_path / "link").symlink_to(tmp_path / "app")
    with pytest.raises((ValueError, OSError)):
        SourcePin.capture(tmp_path, "link", public=True)
    (tmp_path / "linked-dir").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        SourcePin.capture(tmp_path, "linked-dir/app", public=True)
    with pytest.raises(ValueError):
        SourcePin.capture(tmp_path, "app")


def test_inspection_rejects_tampered_blobs_platform_and_limits(tmp_path):
    plan = plan_at(tmp_path)
    receipt = OciBuilder().build(plan, tmp_path, tmp_path / "image")
    with pytest.raises(ValueError):
        inspect_oci(tmp_path / "image", max_bytes=1)
    blob = tmp_path / "image/blobs/sha256" / receipt.layers[0][7:]
    raw = blob.read_bytes()
    blob.write_bytes(raw[:-1] + b"x")
    with pytest.raises(ValueError, match="digest"):
        inspect_oci(tmp_path / "image")
    with pytest.raises(ValueError):
        replace(plan, platform="linux/unknown")
    with pytest.raises(ValueError):
        replace(plan, revision="main")
    with pytest.raises(ValueError):
        replace(plan, files=plan.files * 2)


def oci_archive(tmp_path: Path) -> tuple[Path, str]:
    layout = tmp_path / "layout"
    receipt = OciBuilder().build(plan_at(tmp_path), tmp_path, layout)
    archive_path = tmp_path / "image.tar"
    with tarfile.open(archive_path, "w", format=tarfile.USTAR_FORMAT) as archive:
        for path in sorted(layout.rglob("*")):
            archive.add(path, arcname=path.relative_to(layout), recursive=False)
    return archive_path, receipt.manifest_digest


def docker_archive(tmp_path: Path) -> tuple[Path, str]:
    layout = tmp_path / "base-layout"
    receipt = OciBuilder().build(plan_at(tmp_path), tmp_path, layout)
    config_name = receipt.config_digest[7:] + ".json"
    layer_names = [value[7:] + "/layer.tar" for value in receipt.layers]
    staging = tmp_path / "docker-save"
    staging.mkdir()
    (staging / config_name).write_bytes(
        (layout / "blobs/sha256" / receipt.config_digest[7:]).read_bytes()
    )
    for digest_value, name in zip(receipt.layers, layer_names):
        destination = staging / name
        destination.parent.mkdir()
        destination.write_bytes(
            (layout / "blobs/sha256" / digest_value[7:]).read_bytes()
        )
    (staging / "manifest.json").write_text(
        json.dumps([{"Config": config_name, "RepoTags": [], "Layers": layer_names}])
    )
    archive_path = tmp_path / "base.tar"
    with tarfile.open(archive_path, "w", format=tarfile.USTAR_FORMAT) as archive:
        for path in sorted(staging.rglob("*")):
            archive.add(path, arcname=path.relative_to(staging), recursive=False)
    return archive_path, receipt.config_digest


def test_runnable_archive_build_is_explicit_bounded_and_platform_checked(tmp_path):
    source, _ = oci_archive(tmp_path)
    output = tmp_path / "built.tar"
    code = f"import shutil; shutil.copyfile({str(source)!r}, {str(output)!r})"
    build = RunnableBuild(command(code), output, "linux/arm64")
    result = build.execute(tmp_path, grant(build.command))
    assert result["state"] == "succeeded"
    assert result["platform"] == "linux/arm64"
    assert result["entrypoint"] == ("/bin/app",)
    assert result["runtime_ready"] is False
    assert result["pushed"] is False

    wrong_output = tmp_path / "wrong.tar"
    wrong_command = command(
        f"import shutil; shutil.copyfile({str(source)!r}, {str(wrong_output)!r})"
    )
    with pytest.raises(ValueError, match="platform"):
        RunnableBuild(wrong_command, wrong_output, "linux/amd64").execute(
            tmp_path, grant(wrong_command)
        )


def test_runnable_cancellation_discards_partial_and_archive_rejects_links(tmp_path):
    output = tmp_path / "partial.tar"
    cmd = command(
        f"import pathlib,time; pathlib.Path({str(output)!r}).write_bytes(b'x'); time.sleep(10)"
    )
    cancel = threading.Event()
    timer = threading.Timer(0.1, cancel.set)
    timer.start()
    try:
        result = RunnableBuild(cmd, output, "linux/arm64").execute(
            tmp_path, grant(cmd), cancel=cancel
        )
    finally:
        timer.join()
    assert result["state"] == "cancelled"
    assert not output.exists()

    unsafe = tmp_path / "unsafe.tar"
    with tarfile.open(unsafe, "w") as archive:
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        archive.addfile(info)
    with pytest.raises(ValueError, match="unsafe"):
        unpack_oci_archive(unsafe, tmp_path / "unsafe-layout")


def test_runnable_inspection_requires_matching_architecture(tmp_path):
    source, _ = oci_archive(tmp_path)
    layout = tmp_path / "unpacked"
    unpack_oci_archive(source, layout)
    assert inspect_runnable_oci(layout).entrypoint == ("/bin/app",)
    with pytest.raises(ValueError, match="platform"):
        inspect_runnable_oci(layout, expected_platform="linux/amd64")


def test_pinned_docker_archive_preserves_runtime_closure_and_adds_app_layer(tmp_path):
    archive_path, config_digest = docker_archive(tmp_path)
    plan = plan_at(tmp_path)
    receipt = DockerArchiveOciBuilder().build(
        plan,
        tmp_path,
        archive_path,
        config_digest,
        tmp_path / "composed",
    )
    assert receipt.oci.platform == plan.platform
    assert receipt.oci.plan_hash == plan.plan_hash
    assert receipt.entrypoint == plan.entrypoint
    assert len(receipt.oci.layers) == 2
    assert receipt.summary()["runtime_ready"] is False
    with pytest.raises(ValueError, match="identity"):
        DockerArchiveOciBuilder().build(
            plan,
            tmp_path,
            archive_path,
            "sha256:" + "0" * 64,
            tmp_path / "wrong-base",
        )


def command(code):
    return BuildCommand(ToolPin.capture(Path(sys.executable)), ("-c", code))


def grant(cmd):
    return ExecutionGrant(cmd.plan_hash, time.time() + 30, True, True)


def test_exact_grant_tool_and_inputs_required_before_execution(tmp_path):
    cmd = command("open('built','w').write('public-output')")
    for authority in [
        ExecutionGrant(cmd.plan_hash, time.time() + 30),
        replace(grant(cmd), plan_hash="sha256:" + "0" * 64),
        replace(grant(cmd), expires_at=1),
        replace(grant(cmd), allow_network=False),
    ]:
        with pytest.raises(ValueError):
            cmd.execute(tmp_path, authority)
    assert not (tmp_path / "built").exists()
    receipt = cmd.execute(tmp_path, grant(cmd))
    assert receipt["state"] == "succeeded"
    assert (tmp_path / "built").read_text() == "public-output"
    tool = ToolPin(cmd.tool.path, "sha256:" + "0" * 64)
    bad = BuildCommand(tool, cmd.arguments)
    with pytest.raises(ValueError, match="tool pin"):
        bad.execute(tmp_path, grant(bad))


@pytest.mark.parametrize(
    "code,limits,state",
    [
        ("raise SystemExit(7)", ProcessLimits(1), "failed"),
        ("import time; time.sleep(10)", ProcessLimits(0.15), "timed-out"),
        ("import os; os.write(1,b'x'*10000)", ProcessLimits(1, 10), "output-limit"),
    ],
)
def test_bounded_process_failures(tmp_path, code, limits, state):
    cmd = command(code)
    started = time.monotonic()
    receipt = cmd.execute(tmp_path, grant(cmd), limits=limits)
    assert receipt["state"] == state
    assert time.monotonic() - started < 2


def test_cancellation_kills_process_group_and_output_never_leaks(tmp_path, monkeypatch):
    secret = "NEVER-IN-PUBLIC-RECEIPT-7b1c"
    monkeypatch.setenv("BUILD_SECRET", secret)
    cmd = command("import os; print(os.getenv('BUILD_SECRET','not-inherited'))")
    assert secret not in json.dumps(cmd.execute(tmp_path, grant(cmd)))
    # A tool can print private data; no content or content digest is retained.
    (tmp_path / "private-input").write_text(secret)
    cmd = command("print(open('private-input').read())")
    result = cmd.execute(tmp_path, grant(cmd))
    assert secret not in json.dumps(result)
    assert hashlib.sha256(secret.encode()).hexdigest() not in json.dumps(result)
    cmd = command("import time; time.sleep(10)")
    cancel = threading.Event()
    timer = threading.Timer(0.1, cancel.set)
    timer.start()
    try:
        assert cmd.execute(tmp_path, grant(cmd), cancel=cancel)["state"] == "cancelled"
    finally:
        timer.join()


def test_import_requires_exact_manifest_socket_and_explicit_expiry(tmp_path):
    receipt = OciBuilder().build(plan_at(tmp_path), tmp_path, tmp_path / "oci")
    importer = DockerLocalImporter(
        ToolPin.capture(Path(sys.executable)), tmp_path / "not-a-socket"
    )
    for authority in [
        LocalImportGrant("sha256:" + "0" * 64, importer.socket, time.time() + 30),
        LocalImportGrant(receipt.manifest_digest, importer.socket, 1),
    ]:
        with pytest.raises(ValueError):
            importer.import_image(tmp_path / "oci", authority)
