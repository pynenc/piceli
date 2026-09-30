"""Run locked browser journeys with automatically removed reports and profiles."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
from contextlib import suppress
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    args = sys.argv[1:]
    config = "../tests/browser/playwright.config.cjs"
    if args[:1] == ["--config"]:
        if len(args) < 2 or not args[1].startswith("../tests/browser/"):
            raise SystemExit("--config requires a repository browser config")
        config, args = args[1], args[2:]
    with tempfile.TemporaryDirectory(prefix="piceli-browser-") as directory:
        env = {
            **os.environ,
            "PICELI_UI_TEST_OUTPUT": directory,
            "TMPDIR": directory,
        }
        process = subprocess.Popen(
            [
                "npm",
                "exec",
                "--no",
                "--",
                "playwright",
                "test",
                "--config",
                config,
                *args,
            ],
            cwd=root / "ui",
            env=env,
            start_new_session=True,
        )
        try:
            return process.wait()
        finally:
            # Own the whole browser/server group, including interrupted runs.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
