"""Roll a pipeline's release back to the previous one, in two approved steps.

    python scripts/rollback.py MODULE:ATTR [--to previous|RELEASE] [--env NAME]
    python scripts/rollback.py MODULE:ATTR --approve <plan hash> [--to …] [--env NAME]

Without ``--approve`` it runs ``piceli release rollback previous --spec
MODULE:ATTR``: the rollback is planned against the live cluster (nothing is
applied), and the plan's changes and hash are printed for the owner (exit 3).
After the owner approves that hash, run it again with ``--approve <hash>``.
A rollback restores what the release declares, never data.
"""

from __future__ import annotations

import argparse
import json
import sys

from _piceli import APPROVAL, OK, run, show_failure


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pipeline", help="MODULE:ATTR (or release.toml)")
    parser.add_argument("--to", default="previous", help="release to restore")
    parser.add_argument(
        "--approve", metavar="HASH", help="the plan hash the owner approved"
    )
    parser.add_argument(
        "--env", help="environment of the pipeline (the owner names it)"
    )
    args = parser.parse_args()
    extra = ["--env", args.env] if args.env else []
    if args.approve:
        extra += ["--approve", args.approve]
    status, result, _ = run(
        "release", "rollback", args.to, "--spec", args.pipeline, *extra
    )
    if status == APPROVAL:
        for action in result.get("actions", ()):
            if action["operation"] != "no-op":
                print(f"  {action['operation']} {action['kind']}/{action['name']}")
        print(
            json.dumps(
                {
                    "state": "approval-required",
                    "release": result.get("release"),
                    "plan_hash": result.get("plan_hash"),
                }
            )
        )
        print("show this rollback to the owner; after they approve, run:")
        print(
            f"  python scripts/rollback.py {args.pipeline} --to {args.to} "
            f"--approve {result.get('plan_hash')}"
        )
        return status
    print(
        json.dumps(
            {
                "state": result.get("state") or result.get("release_state"),
                "release": result.get("release"),
                "reason": result.get("reason"),
            }
        )
    )
    if status != OK:
        show_failure(result)
    return status


if __name__ == "__main__":
    sys.exit(main())
