"""Run the HTTPS/OIDC Chromium journey with disposable processes and files."""

from __future__ import annotations

import os
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def _port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="piceli-oidc-browser-") as directory:
        fixture = Path(directory) / "fixture"
        output = Path(directory) / "browser-output"
        temporary = Path(directory) / "tmp"
        for path in (fixture, output, temporary):
            path.mkdir()
        port = _port()
        origin = f"https://127.0.0.1:{port}"
        env = {
            **os.environ,
            "PICELI_OIDC_FIXTURE_DIR": str(fixture),
            "PICELI_OIDC_TEST_URL": origin,
            "PICELI_UI_TEST_OUTPUT": str(output),
            "TMPDIR": str(temporary),
            "PYTHONPATH": str(root),
        }
        server = subprocess.Popen(
            [
                sys.executable,
                "tests/browser/serve_cluster_oidc.py",
                "--port",
                str(port),
            ],
            cwd=root,
            env=env,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if server.poll() is not None:
                    raise RuntimeError("disposable HTTPS OIDC server exited")
                try:
                    with urlopen(
                        origin + "/api/v1/applications",
                        timeout=1,
                        context=ssl._create_unverified_context(),
                    ):
                        break
                except HTTPError as error:
                    if error.code != 403:
                        raise
                    break  # A protected API refusal proves the HTTPS listener is ready.
                except (OSError, URLError):
                    time.sleep(0.1)
            else:
                raise RuntimeError("disposable HTTPS OIDC server did not start")
            browser = subprocess.Popen(
                [
                    "npm",
                    "exec",
                    "--no",
                    "--",
                    "playwright",
                    "test",
                    "--config",
                    "../tests/browser/cluster_oidc.config.cjs",
                    *sys.argv[1:],
                ],
                cwd=root / "ui",
                env=env,
                start_new_session=True,
            )
            try:
                return browser.wait()
            finally:
                _stop(browser)
        finally:
            _stop(server)


if __name__ == "__main__":
    raise SystemExit(main())
