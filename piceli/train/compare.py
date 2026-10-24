"""The old command and the train side by side: ``piceli train --compare`` (0.28.0).

Before switching, a project runs its old merge-and-test command and the
train on the same branches and compares them. :func:`run_old` runs the old
command first, in a scratch clone of the repository (its branches pinned at
the tips the train pinned; no remote, so nothing it does reaches the real
repository or its remote), and times it, phase by phase, from its output.
The train then runs without ``--promote``: nothing is tagged, moved or
pushed. :func:`compare` puts the two side by side (``piceli.train-compare.v1``)
and :func:`render_table` prints it.

What is counted the same way on both sides: the wall time, the tests run
and failed (from cargo, pytest and jest summary lines in the output or the
step logs). The old command's phases start at lines that look like markers
(``==> NAME``, ``::phase NAME``, or a ``bash -x`` trace of a test or build
tool: ``+ cargo test``); its retries are the lines that say retry.

Importing this module is side-effect free.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

COMPARE_SCHEMA = "piceli.train-compare.v1"
#: Seconds the old command may take.
OLD_TIMEOUT = 4 * 3600.0
#: A line that starts a phase of the old command (group ``name``).
PHASE_MARKER = (
    r"^\s*(?:\d\d:\d\d:\d\d\s+)?(?:==>\s*(?P<name>\S.*?)\s*$"
    r"|::phase::?\s*(?P<phase>\S.*?)\s*$"
    r"|\++\s+(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*(?P<tool>(?:cargo|pytest|npm|pnpm|yarn|"
    r"make|docker|go|just|tox|nox)(?:\s+[A-Za-z][\w:.-]*)?))"
)
_CARGO = re.compile(r"test result: \w+\. (\d+) passed; (\d+) failed")
_PYTEST = re.compile(r"^=+ (.*\b(?:passed|failed)\b.*) in [\d.]+s", re.MULTILINE)
_PYTEST_PART = re.compile(r"(\d+) (passed|failed|error|errors)")
_JEST = re.compile(r"^Tests:\s+(.*\btotal)", re.MULTILINE)
_JEST_PART = re.compile(r"(\d+) (failed|total)")
_RETRY = re.compile(r"\bretr(?:y|ying|ied|ies)\b", re.IGNORECASE)
_NAME = re.compile(r"[^a-z0-9_.-]+")


def count_tests(text: str) -> tuple[int, int]:
    """``(tests run, tests failed)`` from cargo, pytest and jest summaries."""
    run = failed = 0
    for passed, bad in _CARGO.findall(text):
        run += int(passed) + int(bad)
        failed += int(bad)
    for summary in _PYTEST.findall(text):
        for number, word in _PYTEST_PART.findall(summary):
            run += int(number)
            if word != "passed":
                failed += int(number)
    for summary in _JEST.findall(text):
        parts = {word: int(n) for n, word in _JEST_PART.findall(summary)}
        run += parts.get("total", 0)
        failed += parts.get("failed", 0)
    return run, failed


def _phase_name(text: str) -> str:
    return _NAME.sub("-", text.lower()).strip("-.")[:40] or "phase"


def old_phases(
    lines: Sequence[tuple[float, str]],
    *,
    end: float,
    marker: str = PHASE_MARKER,
) -> list[dict[str, Any]]:
    """The old command's phases from its timed output lines (seconds since
    it started): ``setup`` until the first marker, then one per marker
    (consecutive markers of the same name are one phase)."""
    pattern = re.compile(marker)
    starts: list[tuple[float, str]] = [(0.0, "setup")]
    for at, text in lines:
        match = pattern.search(text)
        if not match:
            continue
        groups = [g for g in match.groups() if g] if match.groups() else []
        name = _phase_name(groups[0] if groups else match.group(0))
        if name != starts[-1][1]:
            starts.append((at, name))
    phases = []
    for index, (at, name) in enumerate(starts):
        until = starts[index + 1][0] if index + 1 < len(starts) else end
        phases.append({"name": name, "duration": round(max(until - at, 0.0), 3)})
    return phases


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        timeout=600,
        stdin=subprocess.DEVNULL,
    )


def scratch_clone(repo: Path, into: Path, base: str, tips: Mapping[str, str]) -> Path:
    """A clone of ``repo`` (sharing its objects) with every local branch,
    the candidates at ``tips``, ``base`` checked out and no remote."""
    checkout = into / "checkout"
    _git(into, "clone", "-q", "--shared", "--no-checkout", str(repo), str(checkout))
    _git(
        checkout,
        "fetch",
        "-q",
        "--update-head-ok",
        "origin",
        "+refs/heads/*:refs/heads/*",
    )
    _git(checkout, "remote", "remove", "origin")
    for branch, tip in tips.items():
        _git(checkout, "update-ref", f"refs/heads/{branch}", tip)
    _git(checkout, "checkout", "-q", base)
    return checkout


def run_old(
    command: str,
    *,
    repo: Path,
    base: str,
    tips: Mapping[str, str],
    marker: str = PHASE_MARKER,
    timeout: float = OLD_TIMEOUT,
    echo: Any = None,
) -> dict[str, Any]:
    """Run the old command in a scratch clone (removed afterwards).

    ``{branches}`` in the command is replaced by the candidate branches
    (shell-quoted) and ``{repo}`` by the scratch clone; the command runs
    there with ``bash -c`` (``PICELI_COMPARE_BRANCHES`` and
    ``PICELI_COMPARE_CHECKOUT`` are set too). ``echo`` gets each output line.
    """
    branches = list(tips)
    with tempfile.TemporaryDirectory(prefix="piceli-compare-") as scratch:
        checkout = scratch_clone(repo, Path(scratch), base, tips)
        text = command.replace("{branches}", shlex.join(branches)).replace(
            "{repo}", shlex.quote(str(checkout))
        )
        env = {
            **os.environ,
            "PICELI_COMPARE_BRANCHES": " ".join(branches),
            "PICELI_COMPARE_CHECKOUT": str(checkout),
        }
        shell = shutil.which("bash") or "/bin/sh"
        started = time.monotonic()
        lines: list[tuple[float, str]] = []
        process = subprocess.Popen(
            [shell, "-c", text],
            cwd=checkout,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            errors="replace",
            start_new_session=True,
        )
        timed_out = threading.Event()

        def stop() -> None:
            timed_out.set()
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass

        timer = threading.Timer(timeout, stop)
        timer.start()
        try:
            assert process.stdout is not None
            for raw in process.stdout:
                line = raw.rstrip("\n")
                lines.append((time.monotonic() - started, line))
                if echo is not None:
                    echo(line)
            code = process.wait()
        finally:
            timer.cancel()
            if process.poll() is None:
                stop()
                process.wait()
        wall = time.monotonic() - started
    output = "\n".join(line for _, line in lines)
    tests, failed = count_tests(output)
    return {
        "command": text,
        "checkout": str(checkout),
        "exit_code": code,
        "timed_out": timed_out.is_set(),
        "verdict": "pass" if code == 0 and not timed_out.is_set() else "fail",
        "wall": round(wall, 3),
        "phases": old_phases(lines, end=wall, marker=marker),
        "tests": tests,
        "failed_tests": failed,
        "retries": sum(1 for _, line in lines if _RETRY.search(line)),
        "output": output,
    }


def train_side(result: Mapping[str, Any]) -> dict[str, Any]:
    """The train's numbers from its result object (``piceli.train.v1``)."""
    trials = [t for t in result.get("trials", []) if not t.get("discarded")]
    totals: dict[str, float] = {}
    tests = failed = retries = 0
    for trial in trials:
        for step in trial.get("steps", []):
            name = str(step["name"])
            totals[name] = totals.get(name, 0.0) + float(step.get("duration") or 0)
            detail = step.get("detail") or {}
            retries += int(detail.get("retried") or 0)
            log = step.get("log")
            if log and Path(log).is_file():
                found = count_tests(Path(log).read_text(errors="replace"))
                tests += found[0]
                failed += found[1]
    wall = float(result.get("duration") or 0)
    other = max(wall - sum(totals.values()), 0.0)
    phases = [
        {"name": name, "duration": round(seconds, 3)}
        for name, seconds in totals.items()
    ] + [{"name": "other", "duration": round(other, 3)}]
    failures = [
        {
            "branch": branch["branch"],
            "step": branch.get("step"),
            "reason": branch.get("reason"),
        }
        for branch in result.get("branches", [])
        if branch.get("verdict") == "ejected"
    ]
    return {
        "run": result.get("run"),
        "record": result.get("record"),
        "verdict": "pass" if result.get("ok") else "fail",
        "wall": round(wall, 3),
        "phases": phases,
        "tests": tests,
        "failed_tests": failed,
        "retries": retries,
        "flaky": len(result.get("flaky") or []),
        "failures": failures,
        "trials": len(trials),
    }


def compare(
    branches: Sequence[str], old: Mapping[str, Any], train: Mapping[str, Any]
) -> dict[str, Any]:
    """The comparison object (``piceli.train-compare.v1``)."""
    return {
        "schema": COMPARE_SCHEMA,
        "branches": list(branches),
        "old": {key: value for key, value in old.items() if key != "output"},
        "train": dict(train),
        "agree": old["verdict"] == train["verdict"],
        "speedup": (
            round(float(old["wall"]) / float(train["wall"]), 2)
            if float(train["wall"]) > 0
            else None
        ),
    }


def _seconds(value: Any) -> str:
    return "-" if value is None else f"{float(value):.1f} s"


def render_table(body: Mapping[str, Any]) -> str:
    """The comparison as a plain-text table (old command | train)."""
    old, new = body["old"], body["train"]
    rows: list[tuple[str, str, str]] = [
        ("", "old command", "train"),
        ("wall time", _seconds(old["wall"]), _seconds(new["wall"])),
        ("time per phase", "", ""),
    ]
    old_by = {p["name"]: p["duration"] for p in old.get("phases", [])}
    new_by = {p["name"]: p["duration"] for p in new.get("phases", [])}
    for name in list(old_by) + [n for n in new_by if n not in old_by]:
        rows.append(
            (
                name,
                _seconds(old_by[name]) if name in old_by else "",
                _seconds(new_by[name]) if name in new_by else "",
            )
        )
    failures = ", ".join(
        f"{f['branch']} ({f['step']})" for f in new.get("failures", [])
    )
    rows += [
        ("tests run", str(old.get("tests", 0)), str(new.get("tests", 0))),
        (
            "tests failed",
            str(old.get("failed_tests", 0)),
            str(new.get("failed_tests", 0)),
        ),
        (
            "failures found",
            "none" if old.get("exit_code") == 0 else f"exit {old.get('exit_code')}",
            failures or "none",
        ),
        ("retries", str(old.get("retries", 0)), str(new.get("retries", 0))),
        ("verdict", str(old["verdict"]), str(new["verdict"])),
        ("agree", "", "yes" if body["agree"] else "no"),
    ]
    width = max(len(r[0]) for r in rows) + 2
    second = max(len(r[1]) for r in rows) + 2
    return "\n".join(
        f"{label:<{width}}{left:<{second}}{right}".rstrip()
        for label, left, right in rows
    )
