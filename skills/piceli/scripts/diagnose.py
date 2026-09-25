"""Explain an error code and say what to do next. Read-only.

    python scripts/diagnose.py <code>
    python scripts/diagnose.py --result result.json     # a saved JSON result

Every refusal and failure of a piceli command names a fixed code in
``reason`` (``{"state": "rejected", "reason": "<code>"}``). This runs
``piceli explain <code> --json`` and prints its cause, fix and whether the
same command may be retried. Codes never hold secrets or private paths, so
they are safe to report.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from _piceli import parse, show_failure


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("code", nargs="?", help="the error code (the result's reason)")
    parser.add_argument(
        "--result", type=Path, help="a file holding the command's stdout"
    )
    args = parser.parse_args()
    if args.result is not None:
        objects = parse(args.result.read_text())
        result = objects[-1] if objects else {}
    elif args.code:
        result = {"reason": args.code}
    else:
        parser.error("give a code or --result FILE")
    show_failure(result)
    for item in result.get("blocking", ()):  # objects a release plan cannot take
        suggest = " or ".join(item.get("suggest", ())) or "ask the owner"
        print(
            f"  blocking {item['kind']}/{item['name']}: {item['message']} -> {suggest}"
        )
    if result.get("policy"):
        print("  policy: " + json.dumps(result["policy"].get("violations", [])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
