"""The agent skill (``skills/piceli``) runs end to end against the fake API (M4.2).

``scripts/skill_check.py`` copies the skill alone and runs every command of
its SKILL.md walkthrough with this interpreter's piceli; CI runs the same
check against the built wheel. A failing step names the command.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _harness():
    spec = importlib.util.spec_from_file_location(
        "skill_check", ROOT / "scripts" / "skill_check.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["skill_check"] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.timeout(600)
def test_skill_walkthrough_runs_from_a_copy_of_the_skill() -> None:
    log = _harness().fake_check()
    assert any("--approve-if-policy" in line for line in log)
    assert any("rollback.py" in line and "--approve" in line for line in log)


def test_front_matter_and_snippets_are_checked() -> None:
    harness = _harness()
    text = (ROOT / "skills" / "piceli" / "SKILL.md").read_text()
    assert harness.front_matter_problems(text, "0.8.3") == []
    assert harness.front_matter_problems(text, "0.9.0") != []
    assert harness.front_matter_problems("no front matter", "0.8.0") != []
    assert harness.snippet_problems("```python\nnot_in_the_file()\n```", "x = 1") != []


def test_the_skill_follows_the_agent_safety_rules() -> None:
    """No ambient kubeconfig, no auto-approve, no exec opt-in, no secret reveal."""
    skill = ROOT / "skills" / "piceli"
    for path in [*skill.rglob("*.py")]:
        code = path.read_text()
        for forbidden in ("--auto-approve", "--allow-exec", "--reveal", "allow_exec"):
            assert forbidden not in code, (path, forbidden)
        for ambient in (
            "expanduser(",
            "Path.home(",
            "load_kube_config",
            '"KUBECONFIG"',
        ):
            assert ambient not in code, (path, ambient)


def test_diagnose_prints_the_causes_of_a_workload_that_cannot_start(
    tmp_path: Path,
) -> None:
    """``pipeline-apply-crashloop``: one line per cause, no log lines."""
    result = {
        "state": "failed",
        "stage": "apply",
        "reason": "pipeline-apply-crashloop",
        "diagnosis": {
            "schema": "piceli.diagnosis.v1",
            "code": "apply-crashloop",
            "workloads": [
                {
                    "kind": "Deployment",
                    "name": "web",
                    "fatal": True,
                    "causes": [
                        {
                            "pod": "web-1",
                            "container": "web",
                            "reason": "CrashLoopBackOff",
                            "exit_code": 1,
                            "restarts": 3,
                            "logs": ["fatal: cannot open catalog"],
                            "events": [],
                        }
                    ],
                }
            ],
        },
        "summary": {"json": "state/runs/r1/summary.json", "markdown": "x.md"},
    }
    saved = tmp_path / "result.json"
    saved.write_text(json.dumps(result))
    script = ROOT / "skills" / "piceli" / "scripts" / "diagnose.py"
    done = subprocess.run(
        [sys.executable, str(script), "--result", str(saved)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "pipeline-apply-crashloop:" in done.stdout
    assert (
        "cause Deployment/web container web: CrashLoopBackOff, exit 1, 3 restart(s)"
        in done.stdout
    )
    assert "run summary: state/runs/r1/summary.json" in done.stdout
    assert "cannot open catalog" not in done.stdout
