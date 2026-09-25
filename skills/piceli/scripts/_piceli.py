"""Shared helper of the skill's scripts: run one ``piceli`` command, parse its JSON.

The CLI contract: machine output (one JSON object, or JSON lines) on stdout,
human text on stderr, exit codes 0 success, 1 ran but did not succeed,
2 rejected, 3 approval required. Human text is relayed to stderr as is; the
JSON is returned, never printed wholesale (it never holds secret values, but
the scripts show only the fields a person needs).
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from typing import Any

OK, FAILED, REJECTED, APPROVAL = 0, 1, 2, 3


def command() -> list[str]:
    """``piceli`` of this interpreter (``python -m piceli``), else the one on PATH."""
    if os.environ.get("PICELI_BIN"):
        return [os.environ["PICELI_BIN"]]
    if importlib.util.find_spec("piceli") is not None:
        return [sys.executable, "-m", "piceli"]
    found = shutil.which("piceli")
    if found is None:
        raise SystemExit('piceli is not installed: pip install "piceli>=0.7,<0.8"')
    return [found]


def parse(stdout: str) -> list[dict[str, Any]]:
    """Every JSON object on stdout: one indented object, or one per line."""
    text = stdout.strip()
    if not text:
        return []
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    return [value]


def run(
    *args: str, quiet: bool = False
) -> tuple[int, dict[str, Any], list[dict[str, Any]]]:
    """Run ``piceli ARGS``; return (exit code, last JSON object, every object).

    stderr (the human summary, hints, the hash to approve) is relayed unless
    ``quiet``.
    """
    done = subprocess.run(
        [*command(), *args], capture_output=True, text=True, check=False
    )
    if not quiet and done.stderr:
        sys.stderr.write(done.stderr)
    objects = parse(done.stdout)
    return done.returncode, (objects[-1] if objects else {}), objects


def explain(code: str) -> dict[str, Any]:
    """``piceli explain CODE --json``: cause, fix and whether a retry is safe."""
    status, entry, _ = run("explain", code, "--json", quiet=True)
    return entry if status == OK else {"code": code, "unknown": True}


def show_failure(result: dict[str, Any]) -> None:
    """Print the code of a refused or failed result and what to do next."""
    reason = result.get("reason")
    if not reason:
        print("no reason in the result; report the whole JSON object to the owner")
        return
    entry = explain(str(reason))
    if entry.get("unknown"):
        print(f"{reason}: unknown code; report the whole JSON object to the owner")
        return
    print(f"{reason}: {entry['title']}")
    print(f"  cause: {entry['cause']}")
    print(f"  fix:   {entry['fix']}")
    print(
        "  next:  "
        + (
            "safe to re-run the same command once"
            if entry["retry_safe"]
            else "apply the fix, or report it to the owner; do not retry unchanged"
        )
    )
