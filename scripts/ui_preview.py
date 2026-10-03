"""Build and open the production UI against its disposable showcase service."""

from __future__ import annotations

import argparse
import hashlib
import http.cookiejar
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PORT = 4178


def dependencies_current(ui: Path) -> bool:
    """Recognize npm's existing locked install, including platform optional packages."""
    try:
        expected = json.loads((ui / "package-lock.json").read_text())["packages"]
        installed = json.loads((ui / "node_modules/.package-lock.json").read_text())[
            "packages"
        ]
        if not all(
            (ui / "node_modules/.bin" / name).exists() for name in ("vite", "tsc")
        ):
            return False
        for name, package in expected.items():
            if name and name not in installed and not package.get("optional"):
                return False
        return all(
            name in expected
            and all(
                package.get(field) == expected[name].get(field)
                for field in ("version", "resolved", "integrity")
            )
            for name, package in installed.items()
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


@contextmanager
def owned_process(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    output: BinaryIO | None = None,
) -> Iterator[subprocess.Popen[bytes]]:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        start_new_session=True,
        stdout=output,
        stderr=subprocess.STDOUT if output else None,
    )
    try:
        yield process
    finally:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        # A command may have exited while one of its own children stayed alive.
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def prepare_assets(root: Path) -> None:
    if shutil.which("npm") is None:
        raise RuntimeError(
            "Node.js and npm are required. Install Node.js, then run make ui again."
        )
    ui = root / "ui"
    lock_digest = hashlib.sha256((ui / "package-lock.json").read_bytes()).hexdigest()
    stamp = ui / "node_modules/.piceli-lock.sha256"
    stamped = stamp.exists() and stamp.read_text().strip() == lock_digest
    if not dependencies_current(ui) or (stamp.exists() and not stamped):
        print("Installing the locked frontend dependencies…", flush=True)
        with owned_process(["npm", "ci", "--no-audit", "--no-fund"], cwd=ui) as install:
            if install.wait() != 0:
                raise RuntimeError("Frontend dependency installation failed.")
    stamp.write_text(lock_digest + "\n")
    print("Building the current Piceli application…", flush=True)
    with owned_process(["npm", "run", "build"], cwd=ui) as build:
        if build.wait() != 0:
            raise RuntimeError("Frontend build failed. The preview was not started.")


def available_port(preferred: int | None = None) -> int:
    requested = DEFAULT_PORT if preferred is None else preferred
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", requested))
        except OSError as error:
            if preferred is None:
                listener.bind(("127.0.0.1", 0))
                selected = int(listener.getsockname()[1])
                print(
                    f"Default preview port {DEFAULT_PORT} is busy; using {selected}.",
                    flush=True,
                )
                return selected
            raise RuntimeError(
                f"Preview port {requested} is unavailable. Stop your earlier preview or choose another port: "
                'make ui PICELI_UI_PREVIEW_ARGS="--port 4180"'
            ) from error
        return int(listener.getsockname()[1])


def wait_until_ready(
    process: subprocess.Popen[bytes], url: str, timeout: float
) -> None:
    """Exchange the launch token and verify an authenticated API read before opening."""
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
    )
    parsed = urlsplit(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"The preview service exited during startup ({process.returncode})."
            )
        try:
            with opener.open(url, timeout=1) as response:
                response.read()
            with opener.open(f"{origin}/api/v1/capabilities", timeout=1) as response:
                body = json.load(response)
            if "principal" in body and "actions" in body:
                return
        except (urllib.error.URLError, TimeoutError, ValueError):
            pass
        time.sleep(0.1)
    raise RuntimeError("The preview service did not become ready in time.")


@contextmanager
def preview_server(
    root: Path = ROOT,
    *,
    open_browser: bool = True,
    startup_timeout: float = 30,
    port: int | None = None,
) -> Iterator[tuple[subprocess.Popen[bytes], str]]:
    with tempfile.TemporaryDirectory(prefix="piceli-preview-") as temporary:
        port = available_port(port)
        token = secrets.token_urlsafe(32)
        url = f"http://127.0.0.1:{port}/composition/overview"
        env = {
            **os.environ,
            "TMPDIR": temporary,
            "PICELI_UI_LAUNCH_TOKEN": token,
            "PYTHONPATH": str(root),
        }
        command = [
            sys.executable,
            str(root / "tests/browser/serve_showcase.py"),
            "--port",
            str(port),
            "--preview",
        ]
        log_path = Path(temporary) / "service.log"
        with (
            log_path.open("wb") as log,
            owned_process(command, cwd=root, env=env, output=log) as process,
        ):
            try:
                wait_until_ready(process, url, startup_timeout)
            except RuntimeError:
                print(log_path.read_text(errors="replace"), file=sys.stderr)
                raise
            print(
                f"\nPiceli is ready — current application, disposable example data.\nOpen: {url}\nKeep this terminal open. Ctrl+C stops and removes preview state.\n",
                flush=True,
            )
            opened = True
            if open_browser:
                try:
                    opened = webbrowser.open(url)
                except (webbrowser.Error, OSError):
                    opened = False
            if not opened:
                print(
                    "The browser could not be opened automatically; use the launch URL above.",
                    flush=True,
                )
            yield process, url


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Print the launch URL without opening a browser",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Loopback port (default: 4178, or a free port when busy; 0 picks a free port)",
    )
    options = parser.parse_args()
    if options.port is not None and not 0 <= options.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    previous = signal.getsignal(signal.SIGTERM)

    def interrupted(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        prepare_assets(ROOT)
        open_browser = (
            not options.no_browser
            and os.environ.get("PICELI_UI_OPEN_BROWSER", "1") != "0"
        )
        with preview_server(open_browser=open_browser, port=options.port) as (
            process,
            _url,
        ):
            code = process.wait()
            if code:
                raise RuntimeError(f"The preview service exited ({code}).")
        return 0
    except KeyboardInterrupt:
        print("\nPiceli preview stopped.", flush=True)
        return 0
    except (OSError, RuntimeError) as error:
        print(f"Piceli preview: {error}", file=sys.stderr)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(main())
