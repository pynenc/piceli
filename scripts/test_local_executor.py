"""Run the local deployment acceptance and retain portable, secret-free evidence."""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    output = ROOT / "target/local-executor"
    output.mkdir(parents=True, exist_ok=True)
    (output / "results.xml").unlink(missing_ok=True)
    started = time.monotonic()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/unit",
            "tests/acceptance",
            "-q",
            "--tb=short",
            f"--junitxml={output / 'results.xml'}",
        ],
        cwd=ROOT,
        check=False,
    )
    totals = {key: 0 for key in ("tests", "failures", "errors", "skipped")}
    if (output / "results.xml").exists():
        for suite in ET.parse(output / "results.xml").iter("testsuite"):
            for key in totals:
                totals[key] += int(suite.get(key, "0"))
    paths = sorted(
        {
            *ROOT.glob("piceli/k8s/ops/*.py"),
            *ROOT.glob("piceli/artifacts/*.py"),
            *ROOT.glob("tests/acceptance/*.py"),
            *ROOT.glob("tests/unit/ops/deploy/test_*.py"),
            ROOT / "piceli/telemetry.py",
            ROOT / "scripts/test_local_tooling.py",
            ROOT / "tests/unit/test_artifacts.py",
            ROOT / "docs/schemas/piceli-discovery-v2.manifest.json",
            ROOT / "tests/local-requirements.txt",
            ROOT / "tests/local-requirements.lock",
        }
    )
    evidence = {
        "schema_version": 1,
        "delivery_ids": ["LC-06-D", "LC-06-T", "LC-06-X"],
        "status": "passed" if result.returncode == 0 else "failed",
        "scope": "unit and fault-injected loopback; no live-cluster qualification",
        "python": platform.python_version(),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        **totals,
        "inputs": {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths
        },
    }
    path = output / "evidence.json"
    path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    print(f"Local executor evidence: {path}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
