"""Two agents start a CPU-heavy gate at once: the runs serialize, both leave a receipt."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from piceli.maintenance import heavy

BUSY = (
    "import time,sys;t=time.time();n=0\n"
    "while time.time()-t<1.0: n+=1\n"
    "open(sys.argv[1],'a').write(f'{t} {time.time()}\\n')"
)


def test_concurrent_heavy_runs_serialize_with_receipts(tmp_path: Path) -> None:
    state = tmp_path / "state"
    log = tmp_path / "windows"
    env = {**os.environ, heavy.DIR_ENV: str(state)}
    env.pop(heavy.HELD_ENV, None)
    procs = [
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "piceli",
                "heavy",
                "run",
                "--name",
                name,
                "--",
                sys.executable,
                "-c",
                BUSY,
                str(log),
            ],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for name in ("agent-a", "agent-b")
    ]
    receipts = []
    for proc in procs:
        out, _ = proc.communicate(timeout=60)
        assert proc.returncode == 0
        receipts.append(json.loads(out.strip().splitlines()[-1]))
    windows = sorted(
        tuple(map(float, line.split())) for line in log.read_text().splitlines()
    )
    (_, first_end), (second_start, _) = windows
    assert first_end <= second_start  # the busy windows never overlap
    assert {r["name"] for r in receipts} == {"agent-a", "agent-b"}
    assert max(r["waited_seconds"] for r in receipts) >= 0.8
    assert all(r["duration_seconds"] >= 1.0 for r in receipts)
    assert len(heavy.HeavyLock(state).recent_receipts()) == 2
