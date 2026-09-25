"""Is the app up, and how is it reached? Read-only.

    python scripts/status.py MODULE:ATTR|release.toml [--env NAME]

Runs ``piceli status TARGET --json`` (reads the release state and the cluster
through the target's explicit kubeconfig; probes declared forwards on
127.0.0.1) and prints one line per workload and each access URL.
Exit 0 up, 1 not up (the workload lines say why), 2 refused.
"""

from __future__ import annotations

import argparse
import json
import sys

from _piceli import REJECTED, run, show_failure


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "target", help="MODULE:ATTR, path/to/file.py:ATTR or release.toml"
    )
    parser.add_argument(
        "--env", help="environment of the pipeline (the owner names it)"
    )
    args = parser.parse_args()
    extra = ["--env", args.env] if args.env else []
    status, result, _ = run("status", args.target, "--json", *extra, quiet=True)
    if status == REJECTED:
        show_failure(result)
        return status
    release = result.get("release") or {}
    print(
        json.dumps(
            {
                "state": result.get("state"),
                "release": release.get("current"),
                "previous": release.get("previous"),
            }
        )
    )
    for workload in result.get("workloads", ()):
        problems = "; ".join(str(p) for p in workload.get("problems", ()))
        print(
            f"  {workload['kind']}/{workload['name']}: {workload['health']}"
            + (f" ({problems})" if problems else "")
        )
    for forward in (result.get("access") or {}).get("forwards", ()):
        print(f"  access {forward.get('url')} ({forward.get('state', 'declared')})")
    for code in result.get("errors", ()):
        print(f"  error {code}: python scripts/diagnose.py {code}")
    return status


if __name__ == "__main__":
    sys.exit(main())
