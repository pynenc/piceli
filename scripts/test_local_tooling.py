"""LC-06-T: actual Piceli artifacts/executor and the read-only IH Poet fixture.

Uses only a disposable loopback API, owned Poet storage and an explicitly named
local Docker socket. Imports an owned format-test image, never runs or pushes it.
"""
# The pinned sibling fixtures are made importable before these acceptance imports.
# ruff: noqa: E402
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "rustvello/scripts"))

from ih_fixture_pins import verify_ih_inputs
from test_rustvello_poet import attributes, load_lc03b, query_records
from piceli.artifacts import (
    ArtifactFile,
    BuildPlan,
    OciBuilder,
    SourcePin,
    ToolPin,
    inspect_oci,
)
from piceli.artifacts.local_import import DockerLocalImporter, LocalImportGrant
from piceli.k8s.ops.executor import ExecutionLimits
from piceli.k8s.ops.plan import ResourceIntent
from piceli.telemetry import OperationTelemetry, OtlpOptions
from tests.acceptance.fake_api import TARGET, manifest, provider_at, serve
from tests.acceptance.test_local_executor import executor, mutations, prepare


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_pins() -> dict[str, str]:
    files = (
        subprocess.check_output(
            ["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=ROOT, timeout=10
        )
        .decode()
        .split("\0")
    )
    return {
        name: sha(ROOT / name)
        for name in sorted(set(files))
        if name and (ROOT / name).is_file()
    }


def verify_consumer_inputs() -> dict[str, object]:
    path = ROOT / "docs/schemas/piceli-local-tooling-v2.json"
    profile = json.loads(path.read_text())
    assert profile["revision"] == "piceli.local-tooling.v2"
    verified = {}
    for relative, expected in profile["source_pins"].items():
        source = ROOT / relative
        actual = sha(source)
        assert actual == expected, f"LC-06-T consumer pin drift: {relative}"
        verified[relative] = actual
    discovery = profile["discovery"]
    assert sha(ROOT / discovery["path"]) == discovery["sha256"]
    return {
        "path": str(path.relative_to(ROOT)),
        "sha256": sha(path),
        "verified_files": verified,
    }


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
    artifact = Path(tempfile.mkdtemp(prefix="piceli-lc06t-", dir=ROOT / "target"))
    before = source_pins()
    (artifact / "sources.json").write_text(json.dumps(before, indent=2) + "\n")
    evidence = {
        "profile": "piceli.local-tooling.v2",
        "qualified": False,
        "artifact_dir": str(artifact),
        "ih_inputs": pins,
        "consumer_inputs": consumer_inputs,
        "poet_binary_sha256": poet_pin,
        "docker_sha256": tool.sha256,
        "deployment": False,
        "push": False,
        "timings": {},
    }
    poet = lc03b.PoetProcess(poet_binary, artifact)
    telemetry = None
    owned_image = None
    started_all = time.monotonic()

    def docker(*arguments: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(tool.path), "--host", f"unix://{args.docker_socket}", *arguments],
            check=check,
            capture_output=True,
            text=True,
            timeout=30,
        )

    try:
        started = time.monotonic()
        tests = subprocess.run(
            [sys.executable, "scripts/test_local_executor.py"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=180,
        )
        (artifact / "tests.log").write_text(tests.stdout + tests.stderr)
        assert (
            tests.returncode == 0
        ), f"unit/fault API gate failed: {artifact / 'tests.log'}"
        evidence["unit_acceptance"] = json.loads(
            (ROOT / "target/local-executor/evidence.json").read_text()
        )
        evidence["timings"]["tests_seconds"] = time.monotonic() - started
        started = time.monotonic()
        poet.start(tenant="piceli-lc06t")
        evidence["timings"]["poet_start_seconds"] = time.monotonic() - started
        telemetry = OperationTelemetry(
            OtlpOptions(poet.endpoint, poet.token, request_seconds=2)
        )
        start_ns = time.time_ns() - 1_000_000_000
        started = time.monotonic()
        # Format/transport fixture, not a claim that an IH service image is runnable.
        public_source = artifact / "public-source"
        public_source.mkdir()
        (public_source / "entrypoint").write_text(
            "format-only fixture " + uuid.uuid4().hex
        )
        with telemetry.operation("plan", "image"):
            pin = SourcePin.capture(public_source, "entrypoint", public=True)
            plan = BuildPlan(
                "linux/arm64",
                (ArtifactFile(pin, "bin/fixture", True),),
                ("/bin/fixture",),
            )
        (artifact / "build-plan.json").write_text(
            json.dumps(asdict(plan), indent=2) + "\n"
        )
        with telemetry.operation("build", "image", plan_hash=plan.plan_hash):
            receipt = OciBuilder().build(plan, public_source, artifact / "oci")
            second = OciBuilder().build(plan, public_source, artifact / "oci-replay")
            assert receipt == second
            assert (
                inspect_oci(artifact / "oci").manifest_digest == receipt.manifest_digest
            )
        assert (
            docker("image", "inspect", receipt.config_digest, check=False).returncode
            != 0
        )
        owned_image = receipt.config_digest
        with telemetry.operation(
            "import", "image", plan_hash=plan.plan_hash
        ) as operation:
            imported = DockerLocalImporter(tool, args.docker_socket).import_image(
                artifact / "oci",
                LocalImportGrant(
                    receipt.manifest_digest, args.docker_socket, time.time() + 30
                ),
            )
            operation.state = imported["state"]
            assert imported["imported"] and not imported["pushed"]
            image = json.loads(docker("image", "inspect", owned_image).stdout)[0]
            assert image["Id"] == receipt.config_digest
            assert image["Os"] + "/" + image["Architecture"] == receipt.platform
        evidence["oci"] = receipt.summary()
        evidence["local_import"] = imported
        evidence["runnable_service_claim"] = False

        private_value = "cGljZWxpLXByaXZhdGUtZml4dHVyZS12YWx1ZQ=="
        with serve() as (api, url):
            provider = provider_at(url)
            run = executor(
                provider,
                artifact,
                telemetry=telemetry,
                limits=ExecutionLimits(
                    max_seconds=3, readiness_seconds=0.15, poll_seconds=0.02
                ),
            )
            try:
                with telemetry.operation("plan", "lost-reply"):
                    plan, snapshot, grant = prepare(provider, [manifest()])
                api.inject("POST", "/configmaps", disconnect_after=True, dry_run=False)
                assert (
                    run.run("lost-reply", plan, snapshot, grant)["state"] == "blocked"
                )
                run.journal.close()
                run.secrets.close()
                run = executor(
                    provider,
                    artifact,
                    telemetry=telemetry,
                    limits=ExecutionLimits(
                        max_seconds=3, readiness_seconds=0.15, poll_seconds=0.02
                    ),
                )
                assert (
                    run.run("lost-reply", plan, snapshot, grant, resume=True)["state"]
                    == "ready"
                )
                assert (
                    len(mutations(api)) == 1
                ), "resume must observe a committed write, not repeat it"
                compensated = run.compensate("lost-reply", plan, snapshot, grant)
                assert compensated["state"] == "compensated-with-retention"
                evidence["compensation"] = compensated

                plan, snapshot, grant = prepare(
                    provider, [manifest("ConfigMap", "denied")]
                )
                count = len(mutations(api))
                api.inject("POST", "/configmaps", status=403, dry_run=True)
                assert run.run("denied", plan, snapshot, grant)["state"] == "failed"
                assert len(mutations(api)) == count

                api.ready = False
                plan, snapshot, grant = prepare(
                    provider, [manifest("Deployment", "worker")]
                )
                assert run.run("readiness", plan, snapshot, grant)["state"] == "failed"
                assert run.cancel("readiness")["state"] == "cancelled"
                assert (
                    run.run("readiness", plan, snapshot, grant)["state"] == "cancelled"
                )
                api.ready = True
                count = len(mutations(api))
                assert (
                    run.run("readiness", plan, snapshot, grant, resume=True)["state"]
                    == "ready"
                )
                assert len(mutations(api)) == count

                private = run.secrets.put(TARGET, private_value)
                intent = ResourceIntent.from_manifest(
                    manifest("Secret", "credentials")
                ).with_secret("/data/password", private)
                plan, snapshot, grant = prepare(provider, [intent])
                assert run.run("secret", plan, snapshot, grant)["state"] == "ready"
                assert (
                    api.objects[("Secret", "credentials")]["data"]["password"]
                    == private_value
                )
                assert private_value not in json.dumps(run.journal.summary("secret"))
                evidence["mutations"] = len(mutations(api))
            finally:
                run.journal.close()
                run.secrets.close()
                provider.client.close()
        assert telemetry.flush(5)
        stats = telemetry.shutdown(5)
        assert stats["drained"] and stats["pending"] == stats["dropped"] == 0
        for signal in stats["signals"].values():
            assert signal["attempted"] == signal["acknowledged"] == stats["accepted"]
            assert signal["unknown"] == signal["rejected"] == signal["not_sent"] == 0
        evidence["export"] = stats
        evidence["timings"]["operations_export_seconds"] = time.monotonic() - started
        end_ns = time.time_ns() + 1_000_000_000
        records = query_records(lc03b, poet, start_ns, end_ns)
        assert len(records) == stats["accepted"] * 2
        assert Counter(row["signal"] for row in records) == {
            "traces": stats["accepted"],
            "logs": stats["accepted"],
        }
        projected = [attributes(row) for row in records]
        pairs = {
            (row["piceli.operation.phase"], row["piceli.operation.state"])
            for row in projected
        }
        assert {
            ("plan", "succeeded"),
            ("build", "succeeded"),
            ("import", "succeeded"),
            ("apply", "blocked"),
            ("apply", "failed"),
            ("resume", "ready"),
            ("cancel", "cancelled"),
            ("compensate", "compensated-with-retention"),
        } <= pairs
        for row in records:
            attrs = attributes(row)
            assert attrs["piceli.mapping.revision"] == "piceli.operation-otel.v1"
            assert not row.get("invocation_id") and not row.get("task_id")
            if row["signal"] == "logs":
                assert any(
                    span["signal"] == "traces"
                    and span["trace_id"] == row["trace_id"]
                    and span["span_id"] == row["span_id"]
                    for span in records
                )
        encoded = json.dumps(records, sort_keys=True)
        for secret in (
            private_value,
            private.version,
            poet.token,
            "server-password",
            hashlib.sha256(private_value.encode()).hexdigest(),
        ):
            assert secret not in encoded
        (artifact / "records.json").write_text(encoded + "\n")
        evidence["record_count"] = len(records)
        evidence["records_sha256"] = sha(artifact / "records.json")
        poet.stop(crash=True)
        started = time.monotonic()
        poet.start(tenant="piceli-lc06t")
        reopened = query_records(lc03b, poet, start_ns, end_ns)
        assert reopened == records
        evidence["timings"]["poet_restart_query_seconds"] = time.monotonic() - started
        assert sha(poet_binary) == poet_pin
        assert source_pins() == before, "Piceli sources changed during acceptance"
        tool.verify()
        evidence["source_manifest_sha256"] = sha(artifact / "sources.json")
        evidence["qualified"] = True
    finally:
        if telemetry is not None:
            telemetry.shutdown(2)
        poet.stop()
        if owned_image is not None:
            docker("image", "rm", owned_image, check=False)
        evidence["total_seconds"] = time.monotonic() - started_all
        (artifact / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
        print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()
