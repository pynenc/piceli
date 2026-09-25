"""Run a deploy the owner approved, or resume one. Changes the cluster.

    python scripts/deploy.py MODULE:ATTR --approve <combined hash> [--env NAME]
    python scripts/deploy.py MODULE:ATTR --resume [--env NAME]
    python scripts/deploy.py MODULE:ATTR --approve-if-policy [--env NAME]

``--approve`` needs the hash the owner approved (from ``scripts/plan.py``).
``--resume`` continues an interrupted or failed run at its failed stage with
the approval it already had. ``--approve-if-policy`` runs without a hash only
when every action is inside the owner's ``auto_approve`` policy; otherwise it
prints the plan and the approval command for the owner (exit 3).

Prints one line per stage, then the result. On a refusal or failure it
explains the code (``piceli explain``) and says what to do next.
"""

from __future__ import annotations

import argparse
import json
import sys

from _piceli import APPROVAL, OK, run, show_failure


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pipeline", help="MODULE:ATTR or path/to/file.py:ATTR")
    how = parser.add_mutually_exclusive_group(required=True)
    how.add_argument(
        "--approve", metavar="HASH", help="the combined hash the owner approved"
    )
    how.add_argument(
        "--resume", action="store_true", help="continue the latest failed run"
    )
    how.add_argument(
        "--approve-if-policy",
        action="store_true",
        help="run only if the whole plan is inside the owner's auto_approve policy",
    )
    parser.add_argument(
        "--env", help="environment of the pipeline (the owner names it)"
    )
    parser.add_argument(
        "--ref", action="append", default=[], help="SOURCE=SHA as approved"
    )
    args = parser.parse_args()
    extra = [*(["--env", args.env] if args.env else [])]
    for ref in args.ref:
        extra += ["--ref", ref]
    if args.approve:
        extra += ["--approve", args.approve]
    elif args.resume:
        extra += ["--resume"]
    else:
        extra += ["--approve-if-policy"]
    status, result, events = run("deploy", args.pipeline, "--json", *extra)
    for event in events:
        if event.get("event") == "stage" and event["state"] not in {
            "planned",
            "running",
        }:
            print(f"[{event['stage']}] {event['state']}")
    if status == APPROVAL:
        policy = result.get("policy") or {}
        for item in policy.get("violations", ()):
            print(f"outside the owner's policy: {item}")
        print(
            json.dumps(
                {
                    "state": "approval-required",
                    "reason": result.get("reason"),
                    "combined_hash": result.get("combined_hash"),
                }
            )
        )
        print("show the plan above to the owner and ask them to approve that hash")
        return status
    summary = {
        key: result.get(key)
        for key in ("state", "reason", "stage", "release", "run_id", "approved_by")
        if result.get(key) is not None
    }
    print(json.dumps(summary))
    if status != OK:
        show_failure(result)
        if result.get("stage"):
            print(f"after the fix: python scripts/deploy.py {args.pipeline} --resume")
    return status


if __name__ == "__main__":
    sys.exit(main())
