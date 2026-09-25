"""Run tasks against models, grade the answers and summarize the scores."""

from __future__ import annotations

import datetime as dt
import json
import re
import statistics
import sys
import tomllib
from pathlib import Path
from typing import Any

from piceli_eval.grading import grade
from piceli_eval.providers import (
    Message,
    Provider,
    ProviderError,
    ProviderUnavailableError,
    make_provider,
    secret_values,
)
from piceli_eval.scenarios import fixture_text

EVALS = Path(__file__).resolve().parent.parent
ROOT = EVALS.parent
# Bumped whenever a change to the tasks or the grading makes scores incomparable.
HARNESS_VERSION = 1

BASE_SYSTEM = (
    "You are a senior software engineer helping a developer. Answer precisely and "
    "briefly. Put code in fenced code blocks with a language tag."
)
# The agent documentation a coding agent would be given ("docs" context).
CONTEXT_FILES = ("llms.txt", "docs/agents.md")
SKILL_DIR = ROOT / "skills" / "piceli"


def load_tasks(path: Path = EVALS / "tasks.toml") -> list[dict[str, Any]]:
    """The task definitions."""
    tasks: list[dict[str, Any]] = tomllib.loads(path.read_text())["tasks"]
    return tasks


def context_bundle() -> str:
    """The agent docs (and the agent skill, once the repository has one)."""
    parts = [
        f'<file path="{name}">\n{(ROOT / name).read_text()}</file>'
        for name in CONTEXT_FILES
    ]
    if SKILL_DIR.is_dir():
        for path in sorted(SKILL_DIR.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                rel = path.relative_to(ROOT)
                parts.append(f'<file path="{rel}">\n{path.read_text()}</file>')
    return "\n".join(parts)


def system_prompt(with_docs: bool) -> str:
    """The system prompt, with Piceli's agent documentation in ``docs`` mode."""
    if not with_docs:
        return BASE_SYSTEM
    return (
        f"{BASE_SYSTEM}\n\nThe project uses Piceli. Its agent documentation follows; "
        f"use it when it is relevant.\n<docs>\n{context_bundle()}\n</docs>"
    )


def render_prompt(task: dict[str, Any], version: str) -> str:
    """Expand ``{fixture:NAME}``, ``{version}`` and ``{namespace}`` in a prompt."""

    def fixture(match: re.Match[str]) -> str:
        return fixture_text(match[1]).rstrip()

    prompt = re.sub(r"\{fixture:([\w.-]+)\}", fixture, task["prompt"])
    return prompt.replace("{version}", version).strip()


def scrub(text: str) -> str:
    """Remove provider key values from ``text``."""
    for secret in secret_values():
        text = text.replace(secret, "[redacted]")
    return text


def run_task(
    provider: Provider,
    task: dict[str, Any],
    surface: dict[str, Any],
    *,
    with_docs: bool,
    run_code: bool,
    max_turns: int,
) -> dict[str, Any]:
    """One sample of one task: ask, grade, feed failures back up to ``max_turns``."""
    system = system_prompt(with_docs)
    messages = [Message("user", render_prompt(task, surface["version"]))]
    can_retry = run_code and task.get("execute", "none") != "none"
    turns: list[dict[str, Any]] = []
    for turn in range(max_turns if can_retry else 1):
        answer = provider.complete(system, messages, task["id"])
        result = grade(answer, task, surface, run_code=run_code)
        record = {"answer": answer, **result.__dict__}
        turns.append(json.loads(scrub(json.dumps(record))))
        if result.success or turn + 1 == max_turns:
            break
        messages += [
            Message("assistant", answer),
            Message(
                "user",
                result.failure_feedback() + "\nSend the complete corrected answer.",
            ),
        ]
    final = turns[-1]
    return {
        "task": task["id"],
        "kind": task["kind"],
        "with_docs": with_docs,
        "success": final["success"],
        # follow-up turns needed to succeed; an unsolved task counts every turn used
        "interventions": len(turns) - 1 if final["success"] else len(turns),
        # wrong API use in the first answer, before any feedback
        "api_errors": len(turns[0]["api_errors"]),
        "cli_errors": len(turns[0]["cli_errors"]),
        "safety_violations": sorted({v for t in turns for v in t["safety_violations"]}),
        "recommended": final["recommended"],
        "rank": final["rank"],
        "turns": turns,
    }


def _rate(values: list[bool]) -> float | None:
    return round(sum(values) / len(values), 3) if values else None


def _mean(values: list[float]) -> float | None:
    return round(statistics.mean(values), 3) if values else None


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate scores per context mode."""
    summary: dict[str, Any] = {}
    for mode in (False, True):
        rows = [r for r in records if r["with_docs"] is mode]
        if not rows:
            continue
        work = [r for r in rows if r["kind"] != "discovery"]
        discovery = [r for r in rows if r["kind"] == "discovery"]
        ranks = [float(r["rank"]) for r in discovery if r["rank"] is not None]
        per_kind = {
            kind: _rate([r["success"] for r in work if r["kind"] == kind])
            for kind in sorted({r["kind"] for r in work})
        }
        summary["with_docs" if mode else "plain"] = {
            "answers": len(rows),
            "task_success_rate": _rate([r["success"] for r in work]),
            "success_by_kind": per_kind,
            "api_errors_per_task": _mean([r["api_errors"] for r in work]),
            "cli_errors_per_task": _mean([r["cli_errors"] for r in work]),
            "safety_violation_rate": _rate(
                [bool(r["safety_violations"]) for r in work]
            ),
            "interventions_per_task": _mean([r["interventions"] for r in work]),
            "recommendation_rate": _rate([bool(r["recommended"]) for r in discovery]),
            "mean_rank_when_named": round(statistics.mean(ranks), 2) if ranks else None,
        }
    return summary


def run(
    models: list[str],
    tasks: list[dict[str, Any]],
    surface: dict[str, Any],
    *,
    context_modes: list[bool],
    samples: int,
    run_code: bool,
    max_turns: int,
    out_dir: Path | None,
) -> list[dict[str, Any]]:
    """Run every model; a model without credentials is skipped, not failed."""
    reports = []
    for spec in models:
        try:
            provider = make_provider(spec)
        except ProviderUnavailableError as error:
            print(f"skip {spec}: {error}", file=sys.stderr)
            continue
        records = []
        try:
            for with_docs in context_modes:
                for task in tasks:
                    if with_docs and task["kind"] == "discovery":
                        continue  # discovery measures what a model knows unprompted
                    for sample in range(samples):
                        record = run_task(
                            provider,
                            task,
                            surface,
                            with_docs=with_docs,
                            run_code=run_code,
                            max_turns=max_turns,
                        )
                        record["sample"] = sample
                        records.append(record)
                        mark = "ok  " if record["success"] else "FAIL"
                        mode = "docs " if with_docs else "plain"
                        print(
                            f"{mark} {provider.label} [{mode}] {task['id']}#{sample}",
                            file=sys.stderr,
                        )
        except ProviderError as error:
            print(f"error {spec}: {scrub(str(error))}", file=sys.stderr)
            if not records:
                continue
        report: dict[str, Any] = {
            "harness_version": HARNESS_VERSION,
            "model": provider.label,
            "piceli_version": surface["version"],
            "date": dt.date.today().isoformat(),
            "executed_code": run_code,
            "samples": samples,
            "max_turns": max_turns,
        }
        if provider.name == "mock":
            report["measurement"] = (
                "harness validation: a mock model replaying canned answers; "
                "not a measurement of any language model"
            )
        report["summary"] = summarize(records)
        report["records"] = records
        if out_dir is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            name = re.sub(r"[^\w.-]+", "-", provider.label)
            (out_dir / f"{name}.json").write_text(
                scrub(json.dumps(report, indent=1)) + "\n"
            )
        reports.append(report)
    return reports
