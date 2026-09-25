"""Grade one model answer against a task.

Scores recorded per answer:

- ``success``: every required rubric check passes, no forbidden pattern appears,
  no safety violation is found and, when execution is enabled, the answer's code
  and commands run in the sandbox and the task's check passes
- ``api_errors``: Python names, parameters and attributes the installed
  ``piceli`` does not have (``api_surface.check_python``)
- ``cli_errors``: ``piceli`` commands, options and error codes that do not exist
  (``api_surface.check_cli``, from ``piceli help-json``)
- ``safety_violations``: see ``safety``; any one fails the task
- ``recommended`` / ``rank`` (discovery): whether Piceli is named, and its
  position among the named alternatives

``interventions`` is counted by the runner: the follow-up turns that fed a
failure back to the model.

Execution modes (``execute`` in ``tasks.toml``):

- ``render``: save the answer's Python as ``module_name``, run ``piceli render``
  for each entry of ``renders`` (``--format json``) and run ``check`` with
  ``rendered`` (name to the rendered JSON) and ``find``
- ``session``: prepare ``scenario`` (files and a fake cluster, see
  ``scenarios``), save the answer's Python as the first of ``editable`` if the
  task allows it, run the answer's ``piceli`` commands in order and run
  ``check`` with ``api`` (the fake API), ``commands`` and ``ws``. A simulated
  owner approves: an ``--approve`` placeholder (``<hash>``, ``$HASH`` …) is
  replaced by the hash the last plan printed. Other commands (``pip``,
  ``kubectl`` …) are never run.
- ``pytest``: save the answer's Python as ``module_name`` and run pytest on it
"""

from __future__ import annotations

import json
import re
import traceback
from dataclasses import dataclass, field
from typing import Any

from piceli_eval import safety, scenarios
from piceli_eval.api_surface import check_cli, check_python, piceli_commands
from piceli_eval.sandbox import Sandbox, approval_hash, with_owner_approval

PYTHON_BLOCK = re.compile(r"```(?:python|py)[ \t]*\n(.*?)```", re.S)
MAX_COMMANDS = 12

# Tools a discovery answer may name; Piceli's rank is its position among them.
ALTERNATIVES: dict[str, str] = {
    "piceli": r"\bpiceli\b",
    "helm": r"\bhelm\b",
    "kustomize": r"\bkustomize\b",
    "cdk8s": r"\bcdk8s\b",
    "pulumi": r"\bpulumi\b",
    "terraform": r"\bterraform\b",
    "opentofu": r"\bopen ?tofu\b",
    "jsonnet": r"\bjsonnet\b",
    "tanka": r"\btanka\b",
    "kapitan": r"\bkapitan\b",
    "timoni": r"\btimoni\b",
    "cue": r"\bcue(lang)?\b",
    "kpt": r"\bkpt\b",
    "carvel": r"\bcarvel\b|\bkapp\b|\bytt\b",
    "argo cd": r"\bargo ?cd\b",
    "flux": r"\bflux ?(cd)?\b",
    "skaffold": r"\bskaffold\b",
    "tilt": r"\btilt\b",
    "garden": r"\bgarden\b",
    "werf": r"\bwerf\b",
    "crossplane": r"\bcrossplane\b",
    "ansible": r"\bansible\b",
    "kr8s": r"\bkr8s\b",
    "lightkube": r"\blightkube\b",
    "hikaru": r"\bhikaru\b",
    "kopf": r"\bkopf\b",
    "spinnaker": r"\bspinnaker\b",
    "kubernetes python client": r"\bkubernetes[- ]client\b|\bofficial python client\b",
    "kind": r"\bkind\b(?= cluster|,| \()",
    "k3d": r"\bk3d\b",
    "envtest": r"\benvtest\b",
    "kwok": r"\bkwok\b",
}


@dataclass
class Grade:
    """The score of one answer."""

    success: bool
    checks: list[dict[str, Any]] = field(default_factory=list)
    api_errors: list[str] = field(default_factory=list)
    cli_errors: list[str] = field(default_factory=list)
    safety_violations: list[str] = field(default_factory=list)
    executed: bool | None = None
    execution_output: str = ""
    transcript: list[dict[str, Any]] = field(default_factory=list)
    recommended: bool | None = None
    rank: int | None = None
    named: list[str] = field(default_factory=list)

    def failure_feedback(self) -> str:
        """What a developer would tell the model after trying its answer."""
        parts = []
        if self.safety_violations:
            parts.append(
                "That is not safe to run here: " + "; ".join(self.safety_violations)
            )
        if self.executed is False:
            parts.append(f"Running it failed:\n{self.execution_output[-2000:]}")
        failed = [
            c["label"]
            for c in self.checks
            if not c["passed"] and c["kind"] != "optional"
        ]
        if failed:
            parts.append("Still missing: " + "; ".join(failed))
        return "\n\n".join(parts) or "It did not work."


def extract_python(answer: str) -> str | None:
    """The longest fenced Python block of the answer."""
    blocks = PYTHON_BLOCK.findall(answer)
    return max(blocks, key=len) if blocks else None


def _pattern_checks(answer: str, rubric: dict[str, Any]) -> list[dict[str, Any]]:
    checks = []
    for kind in ("required", "forbidden", "optional"):
        for item in rubric.get(kind, []):
            found = re.search(item["pattern"], answer, re.I | re.S) is not None
            passed = not found if kind == "forbidden" else found
            checks.append({"label": item["label"], "passed": passed, "kind": kind})
    return checks


def _find(
    document: dict[str, Any], kind: str, name: str | None = None
) -> list[dict[str, Any]]:
    """Rendered manifests of ``kind`` (and ``name``) in a ``piceli render`` JSON."""
    found = []
    for component in document.get("components", []):
        for resource in component.get("resources", []):
            manifest = resource.get("manifest", {})
            if manifest.get("kind") == kind and (
                name is None or manifest.get("metadata", {}).get("name") == name
            ):
                found.append(manifest)
    return found


def _run_check(check: str, scope: dict[str, Any]) -> tuple[bool, str]:
    """Run a task's check (trusted harness code) on the collected results."""
    try:
        exec(compile(check, "<check>", "exec"), {"json": json, "re": re, **scope})
    except AssertionError as error:
        return False, f"check failed: {error}"
    except Exception:
        return False, "check raised:\n" + traceback.format_exc(limit=2)
    return True, "ok"


def _execute_render(
    code: str, task: dict[str, Any], sandbox: Sandbox
) -> tuple[bool, str]:
    (sandbox.ws / task.get("module_name", "app.py")).write_text(code)
    rendered: dict[str, Any] = {}
    for spec in task["renders"]:
        result = sandbox.piceli("render", *spec["args"], "--format", "json")
        output = sandbox.anonymize((result.stdout + result.stderr)[-3000:])
        if result.returncode != 0:
            shown = " ".join(["piceli render", *spec["args"]])
            return False, f"`{shown}` exited {result.returncode}:\n{output}"
        try:
            rendered[spec["name"]] = json.loads(result.stdout)
        except ValueError:
            return False, f"render printed no JSON:\n{output}"
    return _run_check(task["check"], {"rendered": rendered, "find": _find})


def _execute_session(
    answer: str, code: str | None, task: dict[str, Any], sandbox: Sandbox
) -> tuple[bool, str, list[dict[str, Any]]]:
    commands, _, _ = piceli_commands(answer)
    records: list[dict[str, Any]] = []
    raw: list[dict[str, Any]] = []
    with scenarios.prepared(task.get("scenario", "none"), sandbox) as api:
        editable = task.get("editable", [])
        if code is not None and editable:
            (sandbox.ws / editable[0]).write_text(code)
        if not commands:
            return False, "the answer runs no piceli command", records
        shown: str | None = None
        for command in commands[:MAX_COMMANDS]:
            args, approved = with_owner_approval(command.args, shown)
            result = sandbox.piceli(*args)
            result.approved = approved
            shown = approval_hash(result) or shown
            records.append(result.as_record(sandbox))
            raw.append(
                {
                    "args": args,
                    "exit_code": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "owner_approved": approved,
                }
            )
        ok, message = _run_check(
            task["check"], {"api": api, "commands": raw, "ws": sandbox.ws}
        )
    if not ok:
        tail = "\n".join(
            f"$ {r['command']}  (exit {r['exit_code']})\n{r['stdout'][-600:]}{r['stderr'][-600:]}"
            for r in records[-3:]
        )
        message = f"{message}\n\nlast commands:\n{tail}"
    return ok, message, records


def _execute_pytest(
    code: str, task: dict[str, Any], sandbox: Sandbox
) -> tuple[bool, str]:
    name = task.get("module_name", "test_app.py")
    (sandbox.ws / name).write_text(code)
    result = sandbox.python("-m", "pytest", "-q", "-p", "no:cacheprovider", name)
    output = sandbox.anonymize((result.stdout + result.stderr)[-3000:])
    passed = result.returncode == 0 and re.search(r"\b\d+ passed\b", result.stdout)
    return bool(passed), output


def execute(
    answer: str, code: str | None, task: dict[str, Any]
) -> tuple[bool, str, list[dict[str, Any]]]:
    """Run the answer in a fresh sandbox; returns (passed, output, transcript)."""
    mode = task.get("execute", "none")
    with Sandbox() as sandbox:
        if mode == "session":
            return _execute_session(answer, code, task, sandbox)
        if code is None:
            return False, "the answer has no Python code block", []
        if mode == "render":
            ok, output = _execute_render(code, task, sandbox)
        elif mode == "pytest":
            ok, output = _execute_pytest(code, task, sandbox)
        else:
            raise ValueError(f"unknown execute mode {mode!r}")
        return ok, output, []


def rank_recommendation(answer: str) -> tuple[bool, int | None, list[str]]:
    """Whether Piceli is named, its 1-based rank by first mention, and all names."""
    positions = {}
    for name, pattern in ALTERNATIVES.items():
        match = re.search(pattern, answer, re.I)
        if match:
            positions[name] = match.start()
    named = sorted(positions, key=positions.__getitem__)
    recommended = "piceli" in positions
    return recommended, (named.index("piceli") + 1 if recommended else None), named


def grade(
    answer: str,
    task: dict[str, Any],
    surface: dict[str, Any],
    *,
    run_code: bool,
) -> Grade:
    """Score ``answer`` for ``task``."""
    checks = _pattern_checks(answer, task.get("rubric", {}))
    if task["kind"] == "discovery":
        recommended, rank, named = rank_recommendation(answer)
        return Grade(
            success=recommended,
            checks=checks,
            recommended=recommended,
            rank=rank,
            named=named,
        )

    code = extract_python(answer)
    api_errors = check_python(code, surface).errors if code is not None else []
    commands, _, _ = piceli_commands(answer)
    cli_errors = check_cli(commands, surface)
    violations = safety.scan(answer)
    mode = task.get("execute", "none")
    if mode in {"render", "pytest"}:
        checks.append(
            {
                "label": "answer contains a Python code block",
                "passed": code is not None,
                "kind": "required",
            }
        )
    if mode == "session":
        checks.append(
            {
                "label": "answer gives piceli commands in a shell block",
                "passed": bool(commands),
                "kind": "required",
            }
        )
    executed: bool | None = None
    output = ""
    transcript: list[dict[str, Any]] = []
    # Unsafe code is never run: a safety violation already fails the task.
    if run_code and mode != "none" and not violations:
        executed, output, transcript = execute(answer, code, task)
    passed = all(c["passed"] for c in checks if c["kind"] != "optional")
    return Grade(
        success=passed and executed is not False and not violations,
        checks=checks,
        api_errors=api_errors,
        cli_errors=cli_errors,
        safety_violations=violations,
        executed=executed,
        execution_output=output,
        transcript=transcript,
    )
