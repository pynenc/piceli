"""Running one step in a checkout: commands, cargo check and cargo test.

:func:`run_step` turns a :class:`~piceli.train.model.Step` into commands for
the :class:`~piceli.train.runner.Runner` and a :class:`StepResult`: verdict
(``passed``, ``failed``, ``timeout`` or ``skipped``), start, duration, host,
log path, an output tail, details (the impact selection, the failed and
flaky tests) and the flaky tests seen.

Importing this module is side-effect free.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from piceli.train import cargo, impact
from piceli.train.model import CargoCheck, CargoTests, CommandStep, Impact, Step
from piceli.train.runner import Command, Completed, EventSink, Runner, now_iso

#: Lines of a failed step's output kept in the result.
TAIL_LINES = 20
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass
class StepResult:
    """What one step did (``to_dict`` is its JSON)."""

    name: str
    kind: str
    verdict: str
    started: float
    duration: float
    host: str
    log: str | None = None
    tail: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    flaky: list[dict[str, Any]] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.verdict in {"passed", "skipped"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "verdict": self.verdict,
            "started": now_iso(self.started),
            "duration": round(self.duration, 3),
            "host": self.host,
            "log": self.log,
            **({"tail": self.tail} if self.tail and not self.passed else {}),
            **({"detail": self.detail} if self.detail else {}),
            **({"flaky": self.flaky} if self.flaky else {}),
        }


@dataclass
class StepContext:
    """Where and on what a step runs.

    :param checkout: The merged tree.
    :param base: The train's head the candidates were merged onto.
    :param head: The commit under test.
    :param branches: The candidates in this run.
    :param changed: Paths that differ between ``base`` and ``head``.
    :param env: The environment every command starts from (with the
        ``PICELI_TRAIN_*`` variables).
    :param log_dir: Where step logs go.
    :param prefix: Log file name prefix (the trial).
    """

    checkout: Path
    base: str
    head: str
    branches: Sequence[str]
    changed: Sequence[str]
    env: Mapping[str, str]
    log_dir: Path
    prefix: str
    runner: Runner
    events: EventSink


def log_path(ctx: StepContext, step: str) -> Path:
    return ctx.log_dir / f"{ctx.prefix}{_UNSAFE.sub('-', step)}.log"


class _Budget:
    """The time left of one step, shared by its commands."""

    def __init__(self, seconds: float) -> None:
        self.deadline = time.monotonic() + seconds

    def left(self) -> float:
        return max(1.0, self.deadline - time.monotonic())


def run_step(step: Step, ctx: StepContext) -> StepResult:
    """Run ``step`` in ``ctx.checkout``; never raises for a failing step."""
    started = time.time()
    clock = time.monotonic()
    log = log_path(ctx, step.name)
    env = {**ctx.env, **dict(step.env)}  # type: ignore[arg-type]
    budget = _Budget(step.timeout)

    def run(argv: Sequence[str], *, stdout: bool = False) -> Completed:
        return ctx.runner.run(
            Command(tuple(argv), ctx.checkout, log, env, budget.left(), stdout=stdout)
        )

    if isinstance(step, CommandStep):
        verdict, tail, detail, flaky = _command(step, run)
    elif isinstance(step, CargoCheck):
        verdict, tail, detail, flaky = _cargo_check(step, ctx, run, log)
    elif isinstance(step, CargoTests):
        verdict, tail, detail, flaky = _cargo_tests(step, ctx, run, log)
    else:  # pragma: no cover - every kind is handled above
        raise TypeError(f"unknown step {step!r}")
    return StepResult(
        step.name,
        step.kind,
        verdict,
        started,
        time.monotonic() - clock,
        ctx.runner.host,
        str(log) if log.exists() else None,
        tail,
        detail,
        [{**item, "step": step.name, "branches": list(ctx.branches)} for item in flaky],
    )


_Outcome = tuple[str, str, dict[str, Any], list[dict[str, Any]]]


def _tail(text: str, lines: int = TAIL_LINES) -> str:
    return "\n".join(text.rstrip("\n").splitlines()[-lines:])


def _verdict(done: Completed) -> str:
    if done.ok:
        return "passed"
    return "timeout" if done.error == "timeout" else "failed"


def _command(step: CommandStep, run: Any) -> _Outcome:
    done: Completed = run(step.argv)
    detail = {"exit_code": done.exit_code} if not done.ok else {}
    return _verdict(done), _tail(done.output), detail, []


def _selection(
    select: Impact | str, ctx: StepContext, run: Any, log: Path
) -> tuple[list[str] | None, dict[str, Any], str | None]:
    """``(packages or None for all, detail, error tail)``."""
    if select == "all":
        return None, {"selection": "all"}, None
    assert isinstance(select, Impact)
    done: Completed = run(
        ["cargo", "metadata", "--format-version", "1", "--no-deps"], stdout=True
    )
    if not done.ok:
        return (
            None,
            {},
            "cargo metadata failed" + (f" ({done.error})" if done.error else ""),
        )
    try:
        metadata = json.loads(done.output)
    except ValueError:
        return None, {}, "cargo metadata printed no JSON"
    reasons: list[str] = []
    selected = impact.select(ctx.changed, metadata, select.roots, reasons.append)
    with log.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(reasons) + ("\n" if reasons else ""))
    return selected, {"selection": "all" if selected is None else selected}, None


def _cargo_check(step: CargoCheck, ctx: StepContext, run: Any, log: Path) -> _Outcome:
    selected, detail, error = _selection(step.select, ctx, run, log)
    if error:
        return "failed", error, detail, []
    if selected == []:
        return "skipped", "", {**detail, "why": "no package changed"}, []
    done: Completed = run(
        ["cargo", "check", *impact.cargo_arguments(selected), *step.args]
    )
    if not done.ok:
        detail["exit_code"] = done.exit_code
    return _verdict(done), _tail(done.output), detail, []


def _cargo_tests(step: CargoTests, ctx: StepContext, run: Any, log: Path) -> _Outcome:
    selected, detail, error = _selection(step.select, ctx, run, log)
    if error:
        return "failed", error, detail, []
    if selected == []:
        return "skipped", "", {**detail, "why": "no package changed"}, []
    cargo_args, binary_args = cargo.split_args(step.args)
    argv = [
        "cargo",
        "test",
        "--no-fail-fast",
        *impact.cargo_arguments(selected),
        *cargo_args,
    ]
    if binary_args:
        argv += ["--", *binary_args]
    done: Completed = run(argv)
    if done.ok:
        return "passed", "", detail, []
    if done.error:
        return _verdict(done), _tail(done.output), detail, []
    report = cargo.parse(done.output)
    failed = {item.target: list(item.tests) for item in report.failed}
    detail["failed"] = [
        {"target": target, "tests": tests} for target, tests in failed.items()
    ]
    if step.retry == 0 or not report.retryable:
        return "failed", _tail(done.output), detail, []
    remaining = {item.target: list(item.tests) for item in report.failed}
    targets = {item.target: item for item in report.failed}
    attempts = 0
    while remaining and attempts < step.retry:
        attempts += 1
        still: dict[str, list[str]] = {}
        for target, names in remaining.items():
            again: Completed = run(cargo.retry_argv(targets[target], step.args, names))
            if again.ok:
                continue
            if again.error:
                return _verdict(again), _tail(again.output), detail, []
            rerun = cargo.parse(again.output)
            left = [name for item in rerun.failed for name in item.tests]
            # A retry that failed without naming a test keeps them all failing.
            still[target] = [n for n in names if n in left] or names
        remaining = still
    detail["retried"] = attempts
    flaky = [
        {"target": target, "test": name, "tail": report.output.get((target, name), "")}
        for target, names in failed.items()
        for name in names
        if name not in remaining.get(target, [])
    ]
    if flaky:
        detail["flaky"] = [f"{item['target']} {item['test']}" for item in flaky]
    if remaining:
        detail["failed"] = [
            {"target": target, "tests": tests} for target, tests in remaining.items()
        ]
        tails = [
            f"---- {target} {name}\n{report.output.get((target, name), '')}"
            for target, names in remaining.items()
            for name in names
        ]
        return "failed", _tail("\n".join(tails), 40), detail, flaky
    detail.pop("failed", None)
    return "passed", "", detail, flaky
