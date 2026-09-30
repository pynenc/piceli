"""Hold temporary browser screenshots for visual review, then remove everything."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

CAPTURE = r"""
const { chromium } = require('@playwright/test');
const path = require('node:path');
(async () => {
  const browser = await chromium.launch(process.env.PICELI_BROWSER_EXECUTABLE ? {executablePath:process.env.PICELI_BROWSER_EXECUTABLE} : {});
  const files = [];
  for (const [name,width,height] of [['desktop',1280,800],['tablet',768,1024],['phone',390,844]]) {
    const page = await browser.newPage({viewport:{width,height},reducedMotion:'reduce'});
    await page.goto(process.argv[2] + '/applications/shop/resources');
    await page.getByRole('button',{name:'Inspect Deployment web in piceli-test',exact:true}).waitFor();
    const resource = path.join(process.argv[1],name+'-resources.png');
    await page.screenshot({path:resource,fullPage:true});
    files.push(resource);
    await page.getByRole('button',{name:'Inspect Deployment web in piceli-test',exact:true}).click();
    await page.getByRole('dialog',{name:'Resource details'}).waitFor();
    await page.getByRole('dialog',{name:'Resource details'}).getByRole('heading',{name:'web',exact:true}).waitFor();
    const inspector = path.join(process.argv[1],name+'-inspector.png');
    await page.screenshot({path:inspector,fullPage:true});
    files.push(inspector);
  }
  console.log(JSON.stringify(files));
  await new Promise(resolve => process.stdin.once('data',resolve));
  process.stdin.pause();
  await browser.close();
})().catch(error => { console.error(error); process.exit(1); });
"""


def _stop(process: subprocess.Popen[str]) -> None:
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def _terminate(signum: int, _frame: object) -> None:
    raise SystemExit(128 + signum)


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    port = os.environ.get("PICELI_UI_TEST_PORT", "4177")
    origin = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory(prefix="piceli-ui-review-") as directory:
        env = {**os.environ, "TMPDIR": directory}
        server = subprocess.Popen(
            [sys.executable, "tests/browser/serve_ui.py", "--port", port],
            cwd=root,
            env=env,
            text=True,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 30
            while True:
                try:
                    with urlopen(origin, timeout=1) as response:
                        assert response.status == 200
                    break
                except URLError:
                    if server.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError(
                            "temporary UI server did not start"
                        ) from None
                    time.sleep(0.1)
            browser = subprocess.Popen(
                ["node", "-e", CAPTURE, directory, origin],
                cwd=root / "ui",
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            try:
                assert browser.stdout is not None
                files = json.loads(browser.stdout.readline())
                print(json.dumps({"screenshots": files}), flush=True)
                input(
                    "Press Enter after visual review to remove screenshots and stop servers.\n"
                )
                assert browser.stdin is not None
                browser.stdin.write("\n")
                browser.stdin.flush()
                browser.stdin.close()
                browser.wait(timeout=10)
            finally:
                _stop(browser)
        finally:
            _stop(server)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _terminate)
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130) from None
