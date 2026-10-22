"""Tests stay reliable on busy CI runners (docs/contributing/index.md).

An elapsed-time upper bound under 1 s fails on a loaded runner long before
it catches a real slowdown: wait on an event, a fake clock or a counted call,
or let the broken case take far longer and bound it loosely.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ELAPSED = re.compile(
    r"assert\b[^\n]*(?:monotonic\(\)|perf_counter\(\)|\belapsed\b|\btook\b)[^\n]*<=?\s*([0-9]*\.?[0-9]+)\b"
)
MINIMUM = 1.0


def test_no_sub_second_elapsed_time_bounds() -> None:
    tight = [
        f"{path.relative_to(ROOT)}:{text[: match.start()].count(chr(10)) + 1}"
        for path in sorted((ROOT / "tests").rglob("*.py"))
        if path.name != Path(__file__).name
        for text in [path.read_text(errors="ignore")]
        for match in ELAPSED.finditer(text)
        if float(match.group(1)) < MINIMUM
    ]
    assert not tight, f"elapsed-time bounds under {MINIMUM} s: {tight}"


PROCESS_WAIT = re.compile(r"\.(?:wait|communicate)\(timeout=([0-9]*\.?[0-9]+)\)")
PROCESS_MINIMUM = 30.0


def test_no_short_process_waits() -> None:
    """A process wait is a hang guard: a starved runner can take many seconds
    to start or stop a Python process (0.23.0)."""
    short = [
        f"{path.relative_to(ROOT)}:{text[: match.start()].count(chr(10)) + 1}"
        for path in sorted((ROOT / "tests").rglob("*.py"))
        if path.name != Path(__file__).name
        for text in [path.read_text(errors="ignore")]
        for match in PROCESS_WAIT.finditer(text)
        if float(match.group(1)) < PROCESS_MINIMUM
    ]
    assert not short, f"process waits under {PROCESS_MINIMUM} s: {short}"


STARTUP_WAIT = re.compile(
    r"^\s*deadline = time\.monotonic\(\) \+ ([0-9]*\.?[0-9]+)$", re.M
)


def test_no_short_polling_deadlines() -> None:
    """A loop that waits for a process or thread to get somewhere is a hang
    guard: at least 30 s (0.23.0), it ends as soon as the condition holds."""
    short = [
        f"{path.relative_to(ROOT)}:{text[: match.start()].count(chr(10)) + 1}"
        for path in sorted((ROOT / "tests").rglob("*.py"))
        if path.name != Path(__file__).name
        for text in [path.read_text(errors="ignore")]
        for match in STARTUP_WAIT.finditer(text)
        if float(match.group(1)) < PROCESS_MINIMUM
    ]
    assert not short, f"polling deadlines under {PROCESS_MINIMUM} s: {short}"
