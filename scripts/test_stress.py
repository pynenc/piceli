#!/usr/bin/env python3
"""Run the unit and acceptance suites several times on more workers than cores.

    python scripts/test_stress.py [--rounds 3] [--workers N]

CI runners are small and run several jobs at once, so a test that depends on
timing can pass on a laptop and fail there. Oversubscribing the cores
reproduces that load. Prints every test that failed in any round with its
count and exits 1 if there was one. Stdlib only; run from the repository root.
"""

from __future__ import annotations

import argparse
import collections
import os
import re
import subprocess
import sys

FAILED = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.M)
# --tb=line: one "path:line: Error: message" per failure.
WHY = re.compile(r"^(/\S+:\d+: .*)$", re.M)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--workers", type=int, default=2 * (os.cpu_count() or 2))
    args = parser.parse_args()
    failures: collections.Counter[str] = collections.Counter()
    reasons: dict[str, str] = {}
    for round_ in range(1, args.rounds + 1):
        result = subprocess.run(
            [
                "uv",
                "run",
                "pytest",
                "-q",
                "-n",
                str(args.workers),
                "-p",
                "no:cacheprovider",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        found = FAILED.findall(result.stdout)
        failures.update(set(found))
        for why in WHY.findall(result.stdout):
            reasons[why.split(": ", 1)[0]] = why
        last = result.stdout.strip().splitlines()[-1:] or ["(no output)"]
        print(f"round {round_}/{args.rounds}: {last[0]}", flush=True)
        if result.returncode not in (0, 1):
            print(result.stdout[-3000:] + result.stderr[-3000:], file=sys.stderr)
            return result.returncode
    for name, count in failures.most_common():
        print(f"unreliable: {name} failed in {count} of {args.rounds} rounds")
    for why in reasons.values():
        print(f"  {why[:400]}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
