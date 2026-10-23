"""Running one step in a checkout: commands, cargo check and cargo test.

:func:`run_step` turns a :class:`~piceli.train.model.Step` into commands for
the :class:`~piceli.train.runner.Runner` and a :class:`StepResult`: verdict
(``passed``, ``failed``, ``timeout``, ``skipped`` or ``reused``), start,
duration, the host it ran on, log path, an output tail, details (the impact
selection, the failed and flaky tests, the content key, where it was
placed) and the flaky tests seen.

A step with ``inputs`` is looked up in the content store first
(:mod:`piceli.train.reuse`): a key that passed before is ``reused``, its
outputs restored, nothing run.

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
from piceli.train.reuse import Store, step_key
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
        return self.verdict in {"passed", "skipped", "reused"}

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
    :param own_env: The ``PICELI_TRAIN_*`` variables (what a build host gets
        with the step's own ``env``).
    :param store: The content store (``None``: no reuse).
    :param origin: Who runs it (``train``, ``run``, ``trial``), recorded with
        a passed step's key.
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
    own_env: Mapping[str, str] = field(default_factory=dict)
    store: Store | None = None
    origin: Mapping[str, Any] = field(default_factory=dict)


def log_path(ctx: StepContext, step: str) -> Path:
    return ctx.log_dir / f"{ctx.prefix}{_UNSAFE.sub('-', step)}.log"


class _Budget:
    """The time left of one step, shared by its commands."""

    def __init__(self, seconds: float) -> None:
        self.deadline = time.monotonic() + seconds

    def left(self) -> float:
        return max(1.0, self.deadline - time.monotonic())


class _Placement:
    """Where a step's commands ran: the last host, and the notes."""

    def __init__(self, default: str) -> None:
        self.host = default
        self.notes: list[str] = []

    def saw(self, done: Completed) -> None:
        self.host = done.host
        for note in done.notes:
            if note not in self.notes:
                self.notes.append(note)


def run_step(step: Step, ctx: StepContext) -> StepResult:
    """Run ``step`` in ``ctx.checkout``; never raises for a failing step."""
    started = time.time()
    clock = time.monotonic()
    log = log_path(ctx, step.name)
    env = {**ctx.env, **dict(step.env)}  # type: ignore[arg-type]
    own = {**ctx.own_env, **dict(step.env)}  # type: ignore[arg-type]
    budget = _Budget(step.timeout)
    placed = _Placement(ctx.runner.host)

    def run(
        argv: Sequence[str], *, stdout: bool = False, outputs: Sequence[str] = ()
    ) -> Completed:
        done = ctx.runner.run(
            Command(
                tuple(argv), ctx.checkout, log, env, budget.left(), stdout=stdout,
                hosts=step.hosts, cargo=step.cargo, outputs=tuple(outputs),
                step_env=own,
            )
        )  # fmt: skip
        if not stdout:
            placed.saw(done)
        return done

    def finish(
        verdict: str, tail: str, detail: dict[str, Any], flaky: list[dict[str, Any]]
    ) -> StepResult:
        if placed.notes:
            detail = {**detail, "placement": placed.notes}
        return StepResult(
            step.name,
            step.kind,
            verdict,
            started,
            time.monotonic() - clock,
            placed.host,
            str(log) if log.exists() else None,
            tail,
            detail,
            [
                {**item, "step": step.name, "branches": list(ctx.branches)}
                for item in flaky
            ],
        )

    selected: list[str] | None = None
    base: dict[str, Any] = {}
    if isinstance(step, CargoCheck | CargoTests):
        selected, base, error = _selection(step.select, ctx, run, log)
        if error:
            return finish("failed", error, base, [])
        if selected == []:
            return finish("skipped", "", {**base, "why": "no package changed"}, [])
    key: str | None = None
    if step.inputs and ctx.store is not None:
        extra = (
            {"selection": selected} if isinstance(step, CargoCheck | CargoTests) else {}
        )
        key, count = step_key(step.to_dict(), step.inputs, ctx.checkout, extra)
        base = {**base, "key": key, "inputs": count}
        found = ctx.store.lookup(key)
        restored: list[str] = []
        if found is not None:
            try:
                restored = ctx.store.restore(found, ctx.checkout)
            except OSError:  # pruned meanwhile: run the step
                found = None
        if found is not None:
            origin = {
                k: found.get(k)
                for k in ("train", "run", "trial", "host", "at", "head")
                if found.get(k) is not None
            }
            with log.open("a", encoding="utf-8") as handle:
                handle.write(
                    f"reused {key} from run {origin.get('run')} "
                    f"(trial {origin.get('trial')}, host {origin.get('host')})\n"
                )
            placed.host = str(found.get("host") or placed.host)
            return finish(
                "reused",
                "",
                {
                    **base,
                    "reused": origin,
                    **({"outputs": restored} if restored else {}),
                },
                [],
            )
    if isinstance(step, CommandStep):
        verdict, tail, detail, flaky = _command(step, run)
    elif isinstance(step, CargoCheck):
        verdict, tail, detail, flaky = _cargo_check(step, selected, run)
    elif isinstance(step, CargoTests):
        verdict, tail, detail, flaky = _cargo_tests(step, selected, run)
    else:  # pragma: no cover - every kind is handled above
        raise TypeError(f"unknown step {step!r}")
    detail = {**base, **detail}
    if key is not None and verdict == "passed" and ctx.store is not None:
        record = ctx.store.save(
            key,
            ctx.checkout,
            step.outputs,
            {
                **dict(ctx.origin),
                "step": step.name,
                "host": placed.host,
                "head": ctx.head,
                "at": now_iso(),
            },
        )
        if record is None:
            detail["stored"] = "an output is missing: not kept"
        else:
            detail["stored"] = len(record.get("outputs") or ())
    return finish(verdict, tail, detail, flaky)


_Outcome = tuple[str, str, dict[str, Any], list[dict[str, Any]]]


def _tail(text: str, lines: int = TAIL_LINES) -> str:
    return "\n".join(text.rstrip("\n").splitlines()[-lines:])


def _verdict(done: Completed) -> str:
    if done.ok:
        return "passed"
    return "timeout" if done.error == "timeout" else "failed"


def _command(step: CommandStep, run: Any) -> _Outcome:
    done: Completed = run(step.argv, outputs=step.outputs)
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
    selected = impact.select(
        ctx.changed,
        metadata,
        select.roots,
        reasons.append,
        dev_targets=select.dev_targets,
    )
    with log.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(reasons) + ("\n" if reasons else ""))
    return selected, {"selection": "all" if selected is None else selected}, None


def _cargo_check(step: CargoCheck, selected: list[str] | None, run: Any) -> _Outcome:
    detail: dict[str, Any] = {}
    done: Completed = run(
        ["cargo", "check", *impact.cargo_arguments(selected), *step.args],
        outputs=step.outputs,
    )
    if not done.ok:
        detail["exit_code"] = done.exit_code
    return _verdict(done), _tail(done.output), detail, []


def _cargo_tests(step: CargoTests, selected: list[str] | None, run: Any) -> _Outcome:
    detail: dict[str, Any] = {}
    cargo_args, binary_args = cargo.split_args(step.args)
    report = cargo.CargoReport()
    outputs: list[str] = []
    failed_any = False
    invocations = impact.cargo_test_arguments(selected)
    for index, arguments in enumerate(invocations):
        argv = ["cargo", "test", "--no-fail-fast", *arguments, *cargo_args]
        if binary_args:
            argv += ["--", *binary_args]
        last = index + 1 == len(invocations)
        done: Completed = run(argv, outputs=step.outputs if last else ())
        if done.ok:
            continue
        if done.error:
            return _verdict(done), _tail(done.output), detail, []
        failed_any = True
        outputs.append(done.output)
        found = cargo.parse(done.output)
        if not found.failed:  # failed without naming a target: not retryable
            found.failed.append(cargo.FailedTarget(" ".join(arguments)))
        report.failed += found.failed
        report.output.update(found.output)
    if not failed_any:
        return "passed", "", detail, []
    text = "\n".join(outputs)
    failed = {item.target: list(item.tests) for item in report.failed}
    detail["failed"] = [
        {"target": target, "tests": tests} for target, tests in failed.items()
    ]
    if step.retry == 0 or not report.retryable:
        return "failed", _tail(text), detail, []
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
