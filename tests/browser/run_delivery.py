"""Build one isolated renderer, run delivery journeys, remove every artifact."""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import tempfile
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path

from tests.ui_render_support import renderer_image


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    # The server must exit before deleting its image and private configuration.
    with tempfile.TemporaryDirectory(prefix="piceli-delivery-browser-") as temporary:
        directory = Path(temporary)
        with renderer_image() as renderer:
            config = directory / "renderer.json"
            config.write_text(json.dumps(asdict(renderer), default=str))
            config.chmod(0o600)
            env = {
                **os.environ,
                "PICELI_DELIVERY_RENDERER": str(config),
                "PICELI_UI_TEST_OUTPUT": str(directory / "results"),
                "TMPDIR": temporary,
                "PICELI_UI_LAUNCH_TOKEN": secrets.token_urlsafe(32),
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
                    "../tests/browser/delivery.config.cjs",
                    *sys.argv[1:],
                ],
                cwd=root / "ui",
                env=env,
                start_new_session=True,
            )
            try:
                return process.wait()
            finally:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
