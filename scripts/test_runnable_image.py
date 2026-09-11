"""LC-06-R: offline pinned runtime closure, explicit import/run and real Poet."""
# Sibling acceptance helpers are deliberately added before imports.
# ruff: noqa: E402
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "rustvello/scripts"))

from ih_fixture_pins import verify_ih_inputs
from test_rustvello_poet import attributes, load_lc03b, query_records
from piceli.artifacts import (
    ArtifactFile,
    BuildCommand,
    BuildPlan,
    DockerArchiveOciBuilder,
    DockerLocalRunner,
    ExecutionGrant,
    LocalRunGrant,
    ProcessLimits,
    SourcePin,
    ToolPin,
    inspect_runnable_oci,
)
from piceli.artifacts.local_import import DockerLocalImporter, LocalImportGrant
from piceli.telemetry import OperationTelemetry, OtlpOptions

IMAGE = (
    "postgres@sha256:67f41722b7a8cbdb868a44a4995c846eddfdc2973bccb291ce937dce88ad5675"
)
BASE_CONFIG = "sha256:651ddcd1c769a6d94cf4aaca9756f2abb6acd65e712f4a386a2ff4e730586d5b"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sources() -> dict[str, str]:
    names = (
        subprocess.check_output(
            ["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=ROOT, timeout=10
        )
        .decode()
        .split("\0")
    )
    return {
        name: sha(ROOT / name)
        for name in sorted(set(names))
        if name and (ROOT / name).is_file()
    }


def verify_consumer_inputs() -> dict[str, object]:
    path = ROOT / "docs/schemas/piceli-runnable-image-v1.json"
    profile = json.loads(path.read_text())
    assert profile["revision"] == "piceli.runnable-image.v1"
    assert profile["base_image"] == IMAGE
    assert profile["base_config_digest"] == BASE_CONFIG
    for relative, expected in profile["source_pins"].items():
        assert (
            sha(ROOT / relative) == expected
        ), f"LC-06-R consumer pin drift: {relative}"
    return {"path": str(path.relative_to(ROOT)), "sha256": sha(path)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ih-workspace", type=Path, required=True)
    parser.add_argument("--docker", type=Path, required=True)
    parser.add_argument("--docker-socket", type=Path, required=True)
    args = parser.parse_args()
    pins, helper = verify_ih_inputs(args.ih_workspace)
    consumer_inputs = verify_consumer_inputs()
    lc03b = load_lc03b(args.ih_workspace / "scripts/test_otlp_process.py", helper)
    poet_binary = args.ih_workspace / "repos/infinite-haiku/target/debug/poet"
    poet_pin = sha(poet_binary)
    tool = ToolPin.capture(args.docker)
    (ROOT / "target").mkdir(exist_ok=True)
    artifact = Path(tempfile.mkdtemp(prefix="piceli-lc06r-", dir=ROOT / "target"))
    before = sources()
    (artifact / "sources.json").write_text(json.dumps(before, indent=2) + "\n")
    evidence: dict[str, object] = {
        "profile": "piceli.runnable-image.v1",
        "qualified": False,
        "artifact_dir": str(artifact),
        "ih_inputs": pins,
        "consumer_inputs": consumer_inputs,
        "poet_binary_sha256": poet_pin,
        "base_image": IMAGE,
        "base_config_digest": BASE_CONFIG,
        "deployment": False,
        "push": False,
        "timings": {},
    }
    poet = lc03b.PoetProcess(poet_binary, artifact)
    telemetry: OperationTelemetry | None = None
    owned_images: list[str] = []
    started_all = time.monotonic()

    def docker(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(tool.path), "--host", f"unix://{args.docker_socket}", *arguments],
            capture_output=True,
            text=True,
            timeout=30,
            check=check,
        )

    try:
        image = json.loads(docker("image", "inspect", IMAGE).stdout)[0]
        assert image["Id"] == BASE_CONFIG
        assert f"{image['Os']}/{image['Architecture']}" == "linux/arm64"
        poet.start(tenant="piceli-lc06r")
        telemetry = OperationTelemetry(
            OtlpOptions(poet.endpoint, poet.token, request_seconds=2)
        )
        start_ns = time.time_ns() - 1_000_000_000

        base_archive = artifact / "base.tar"
        save = BuildCommand(
            tool,
            (
                "--host",
                f"unix://{args.docker_socket}",
                "image",
                "save",
                "--output",
                str(base_archive),
                IMAGE,
            ),
        )
        with telemetry.operation(
            "build", "runtime-base", plan_hash=save.plan_hash
        ) as operation:
            saved = save.execute(
                ROOT,
                ExecutionGrant(save.plan_hash, time.time() + 60, True, True),
                limits=ProcessLimits(60, 1_048_576),
            )
            operation.state = saved["state"]
            assert saved["state"] == "succeeded"

        fixture = ROOT / "tests/fixtures/runnable-image"
        source = SourcePin.capture(fixture, "readiness.sh", public=True)
        plan = BuildPlan(
            "linux/arm64",
            (ArtifactFile(source, "opt/piceli/readiness", True),),
            ("/opt/piceli/readiness",),
            user="65532:65532",
        )
        layout = artifact / "oci"
        with telemetry.operation("build", "runnable-image", plan_hash=plan.plan_hash):
            receipt = DockerArchiveOciBuilder().build(
                plan, fixture, base_archive, BASE_CONFIG, layout
            )
            assert (
                inspect_runnable_oci(layout, expected_platform="linux/arm64") == receipt
            )
        with telemetry.operation("inspect", "runnable-image", plan_hash=plan.plan_hash):
            try:
                inspect_runnable_oci(layout, expected_platform="linux/amd64")
            except ValueError:
                pass
            else:
                raise AssertionError("architecture mismatch was accepted")

        importer = DockerLocalImporter(tool, args.docker_socket)
        with telemetry.operation(
            "import", "runnable-image", plan_hash=plan.plan_hash
        ) as operation:
            imported = importer.import_runnable_image(
                layout,
                LocalImportGrant(
                    receipt.oci.manifest_digest, args.docker_socket, time.time() + 60
                ),
                limits=ProcessLimits(60),
            )
            operation.state = imported["state"]
            assert imported["imported"] and not imported["pushed"]
        image_id = imported["image_id"]
        owned_images.append(image_id)
        runner = DockerLocalRunner(tool, args.docker_socket)
        inspected = runner.inspect_image(image_id, "linux/arm64")
        grant = LocalRunGrant(
            image_id, "linux/arm64", args.docker_socket, time.time() + 60
        )
        with telemetry.operation(
            "run", "runnable-image", plan_hash=plan.plan_hash
        ) as operation:
            ran = runner.run_image(grant, limits=ProcessLimits(10, 4096))
            operation.state = ran["state"]
            assert ran["runtime_ready"] and ran["state"] == "succeeded"

        cancel = threading.Event()
        timer = threading.Timer(2.0, cancel.set)
        timer.start()
        try:
            with telemetry.operation(
                "run", "cancelled-image", plan_hash=plan.plan_hash
            ) as operation:
                cancelled = runner.run_image(
                    grant,
                    arguments=("sleep",),
                    limits=ProcessLimits(10, 4096),
                    cancel=cancel,
                )
                operation.state = cancelled["state"]
                assert cancelled["state"] == "cancelled"
                assert cancelled["seconds"] > 0.1
        finally:
            timer.join()
        assert not docker(
            "ps", "--filter", "name=piceli-runnable-", "--quiet"
        ).stdout.strip()

        broken_source = SourcePin.capture(fixture, "missing-runtime.sh", public=True)
        broken_plan = BuildPlan(
            "linux/arm64",
            (ArtifactFile(broken_source, "opt/piceli/missing", True),),
            ("/opt/piceli/missing",),
            user="65532:65532",
        )
        broken_layout = artifact / "broken-oci"
        broken = DockerArchiveOciBuilder().build(
            broken_plan, fixture, base_archive, BASE_CONFIG, broken_layout
        )
        broken_import = importer.import_runnable_image(
            broken_layout,
            LocalImportGrant(
                broken.oci.manifest_digest, args.docker_socket, time.time() + 60
            ),
        )
        owned_images.append(broken_import["image_id"])
        broken_run = runner.run_image(
            LocalRunGrant(
                broken_import["image_id"],
                "linux/arm64",
                args.docker_socket,
                time.time() + 30,
            )
        )
        assert broken_run["state"] == "failed" and not broken_run["runtime_ready"]

        assert telemetry.flush(5)
        export = telemetry.shutdown(5)
        assert export["drained"] and export["pending"] == export["dropped"] == 0
        end_ns = time.time_ns() + 1_000_000_000
        records = query_records(lc03b, poet, start_ns, end_ns)
        assert Counter(record["signal"] for record in records) == {
            "traces": export["accepted"],
            "logs": export["accepted"],
        }
        projected = [attributes(record) for record in records]
        assert {row["piceli.operation.phase"] for row in projected} >= {
            "build",
            "inspect",
            "import",
            "run",
        }
        encoded = json.dumps(records, sort_keys=True)
        assert poet.token not in encoded and str(args.docker_socket) not in encoded
        (artifact / "records.json").write_text(encoded + "\n")
        poet.stop(crash=True)
        poet.start(tenant="piceli-lc06r")
        assert query_records(lc03b, poet, start_ns, end_ns) == records

        assert sha(poet_binary) == poet_pin
        assert sources() == before, "Piceli sources changed during acceptance"
        tool.verify()
        evidence.update(
            {
                "qualified": True,
                "base_archive_bytes": base_archive.stat().st_size,
                "oci": receipt.summary(),
                "local_import": imported,
                "local_inspection": inspected,
                "runtime": ran,
                "cancelled_runtime": cancelled,
                "dependency_failure": broken_run,
                "export": export,
                "record_count": len(records),
                "records_sha256": sha(artifact / "records.json"),
                "source_manifest_sha256": sha(artifact / "sources.json"),
            }
        )
    finally:
        if telemetry is not None:
            telemetry.shutdown(2)
        poet.stop()
        for image_id in owned_images:
            docker("image", "rm", "--force", image_id, check=False)
        evidence["total_seconds"] = time.monotonic() - started_all
        (artifact / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
        print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()
