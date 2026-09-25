"""Plan a pipeline deploy and print what the owner must review. Changes nothing.

    python scripts/plan.py MODULE:ATTR [--env NAME] [--ref SOURCE=REV]

Runs ``piceli deploy MODULE:ATTR --plan --json`` (reads sources and the
cluster, writes only the pipeline's state directory), then prints the review
summary: builds, deliveries, every release change (adopt, replace, delete and
cluster-scoped ones called out), and the exact approval command. Show all of
it to the owner and wait for them to approve **that** hash.

Exit 0 planned; otherwise the command's exit code, with the code explained.
"""

from __future__ import annotations

import argparse
import json
import sys

from _piceli import OK, run, show_failure

ATTENTION = {"adopt", "replace", "delete"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pipeline", help="MODULE:ATTR or path/to/file.py:ATTR")
    parser.add_argument(
        "--env", help="environment of the pipeline (the owner names it)"
    )
    parser.add_argument(
        "--ref", action="append", default=[], help="SOURCE=REV to build"
    )
    args = parser.parse_args()
    extra = [*(["--env", args.env] if args.env else [])]
    for ref in args.ref:
        extra += ["--ref", ref]
    status, result, _ = run("deploy", args.pipeline, "--plan", "--json", *extra)
    if status != OK or result.get("state") != "planned":
        show_failure(result)
        return status or 1
    stages = result["stages"]
    release = stages["plan"]
    changes = release.get("changes")
    if release.get("state") == "pending":  # images not built yet: a preview
        changes = release["preview"]["changes"]
        print(
            "release changes (preview with placeholder images, not approvable alone):"
        )
    else:
        print("release changes:")
    for change in changes or ():
        flags = []
        if change["operation"] in ATTENTION:
            flags.append("NEEDS ATTENTION")
        if change.get("cluster_scoped"):
            flags.append("cluster-scoped")
        note = f"  [{', '.join(flags)}]" if flags else ""
        print(f"  {change['operation']} {change['kind']}/{change['name']}{note}")
    if not changes:
        print("  none")
    approve = [
        "piceli",
        "deploy",
        args.pipeline,
        *extra,
        "--approve",
        result["combined_hash"],
    ]
    print(
        json.dumps(
            {
                "state": "planned",
                "combined_hash": result["combined_hash"],
                "approve_with": " ".join(approve),
            }
        )
    )
    print(
        "show this plan to the owner; run the approval command only after they approve it"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
