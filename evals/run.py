"""Cross-model eval of Piceli: install, implement, operate and discovery tasks.

Examples::

    # validate the harness itself: no keys, no network
    uv run --frozen python evals/run.py run --model mock:reference --execute

    # real models (keys come from the environment and are never printed)
    uv run --frozen python evals/run.py run --model anthropic:MODEL \\
        --model openai:MODEL --model gemini:MODEL --execute --samples 3 \\
        --out evals/results

See evals/README.md for the tasks, the scores and how to record a baseline.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from piceli_eval import api_surface
from piceli_eval.runner import load_tasks, run

KINDS = ["install", "implement", "operate", "discovery"]


def _cmd_run(args: argparse.Namespace) -> int:
    tasks = load_tasks()
    if args.task:
        tasks = [t for t in tasks if t["id"] in set(args.task)]
    if args.kind:
        tasks = [t for t in tasks if t["kind"] in set(args.kind)]
    if not tasks:
        print("no task matches", file=sys.stderr)
        return 2
    modes = {"both": [False, True], "docs": [True], "plain": [False]}[args.context]
    surface = api_surface.load()
    out_dir = Path(args.out) / surface["version"] if args.out else None
    reports = run(
        args.model,
        tasks,
        surface,
        context_modes=modes,
        samples=args.samples,
        run_code=args.execute,
        max_turns=args.max_turns,
        out_dir=out_dir,
    )
    print(json.dumps({r["model"]: r["summary"] for r in reports}, indent=2))
    if not reports:
        print("no model ran (models without a key are skipped)", file=sys.stderr)
    return 0


def _cmd_list(_: argparse.Namespace) -> int:
    for task in load_tasks():
        print(f"{task['id']:<34} {task['kind']:<10} {task.get('execute', 'none')}")
    return 0


def _cmd_api_surface(args: argparse.Namespace) -> int:
    text = api_surface.dumps(api_surface.introspect())
    if args.write:
        api_surface.SNAPSHOT.write_text(text)
        print(f"wrote {api_surface.SNAPSHOT}")
        return 0
    if api_surface.SNAPSHOT.read_text() != text:
        print(
            "api_surface.json differs from the installed piceli; "
            "run `python evals/run.py api-surface --write`",
            file=sys.stderr,
        )
        return 1
    print("api_surface.json matches the installed piceli")
    return 0


def main() -> int:
    """Command line entry point."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="run tasks against models")
    run_parser.add_argument(
        "--model", action="append", required=True, help="provider:model (repeatable)"
    )
    run_parser.add_argument("--task", action="append", help="only this task id")
    run_parser.add_argument("--kind", action="append", choices=KINDS)
    run_parser.add_argument(
        "--context",
        choices=["both", "plain", "docs"],
        default="both",
        help="plain: no Piceli docs; docs: llms.txt and docs/agents.md in the "
        "system prompt (default: both)",
    )
    run_parser.add_argument("--samples", type=int, default=1, help="answers per task")
    run_parser.add_argument(
        "--execute",
        action="store_true",
        help="run the answers' code and piceli commands in the sandbox",
    )
    run_parser.add_argument(
        "--max-turns", type=int, default=3, help="turns per task when it fails"
    )
    run_parser.add_argument("--out", help="write <out>/<piceli version>/<model>.json")
    run_parser.set_defaults(func=_cmd_run)

    list_parser = sub.add_parser("list", help="list the tasks")
    list_parser.set_defaults(func=_cmd_list)

    surface_parser = sub.add_parser(
        "api-surface", help="compare or refresh api_surface.json"
    )
    surface_parser.add_argument("--write", action="store_true")
    surface_parser.set_defaults(func=_cmd_api_surface)

    args = parser.parse_args()
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    sys.exit(main())
