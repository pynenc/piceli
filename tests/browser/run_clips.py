"""Record short UI journeys against the fake API and publish them as clips.

Playwright videos are converted to animated WebP (with a GIF fallback) of at
most 1.5 MB each, plus PNG screenshots, into an ignored output directory with
an ``index.html`` gallery. Everything else (videos, browser state, the fake
cluster) lives in a temporary directory removed on exit.
"""

from __future__ import annotations

import html
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
from contextlib import suppress
from datetime import datetime
from pathlib import Path

MAX_BYTES = 1_500_000


def _convert(video: Path, target: Path) -> list[Path]:
    """Return the WebP (and GIF fallback) made from ``video``, each within budget."""
    made: list[Path] = []
    for fps, width in ((10, 960), (8, 800), (6, 640)):
        scale = f"fps={fps},scale={width}:-1:flags=lanczos"
        webp = target.with_suffix(".webp")
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-vf", scale,
             "-c:v", "libwebp_anim", "-loop", "0", "-q:v", "55", str(webp)],
            check=True,
        )
        gif = target.with_suffix(".gif")
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-vf",
             f"{scale},split[a][b];[a]palettegen=max_colors=96[p];[b][p]paletteuse",
             str(gif)],
            check=True,
        )
        if webp.stat().st_size <= MAX_BYTES and gif.stat().st_size <= MAX_BYTES:
            return [webp, gif]
        made = [webp, gif]
    for path in made:  # still too large at the smallest setting: keep nothing
        path.unlink()
    raise SystemExit(f"{target.name}: clip exceeds {MAX_BYTES} bytes at the smallest size")


def main() -> int:
    root = Path(__file__).resolve().parents[2]
    out = Path(os.environ.get("PICELI_UI_CLIPS_DIR", root / ".ui-clips"))
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is required to convert the recorded videos")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run = out / stamp
    run.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="piceli-clips-") as temporary:
        env = {
            **os.environ,
            "PICELI_UI_TEST_OUTPUT": temporary,
            "PICELI_UI_CLIPS_SHOTS": str(run),
            "TMPDIR": temporary,
            "PICELI_UI_LAUNCH_TOKEN": secrets.token_urlsafe(32),
        }
        process = subprocess.Popen(
            ["npm", "exec", "--no", "--", "playwright", "test", "--config",
             "../tests/browser/clips.config.cjs"],
            cwd=root / "ui", env=env, start_new_session=True,
        )
        try:
            code = process.wait()
        finally:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        if code != 0:
            return code
        for video in sorted(Path(temporary).rglob("*.webm")):
            name = video.parent.name.split("-chromium")[0]
            name = name.removeprefix("clips-")
            _convert(video, run / name)
    items = []
    for path in sorted(run.glob("*.webp")):
        gif = path.with_suffix(".gif")
        items.append(
            f"<figure><picture><source srcset='{path.name}' type='image/webp'>"
            f"<img src='{gif.name}' alt='{html.escape(path.stem)}' width='640'></picture>"
            f"<figcaption>{html.escape(path.stem)}: {path.stat().st_size // 1024} KB WebP, "
            f"{gif.stat().st_size // 1024} KB GIF</figcaption></figure>"
        )
    (run / "index.html").write_text(
        "<!doctype html><meta charset=utf-8><title>Piceli UI clips</title>"
        f"<h1>Piceli UI clips {stamp}</h1>{''.join(items)}"
    )
    (out / "index.html").write_text(
        f"<!doctype html><meta charset=utf-8><meta http-equiv=refresh content='0;url={stamp}/index.html'>"
    )
    print(f"clips and gallery: {run}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
