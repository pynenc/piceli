"""Thin JSON CLI over artifact library calls. Preview does not instantiate clients."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from piceli.artifacts import BuildPlan, OciBuilder, SourcePin, inspect_oci
from piceli.artifacts.local_import import DockerLocalImporter, LocalImportGrant
from piceli.artifacts.process import (
    BuildCommand,
    ExecutionGrant,
    ProcessLimits,
    ToolPin,
)
from piceli.k8s.ops.bounds import strict_json


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="piceli artifacts")
    sub = parser.add_subparsers(dest="command", required=True)
    for action in ("preview", "build"):
        cmd = sub.add_parser(action)
        cmd.add_argument("--plan", type=Path, required=True)
        if action == "build":
            cmd.add_argument("--source-root", type=Path, required=True)
            cmd.add_argument("--output", type=Path, required=True)
    cmd = sub.add_parser("pin")
    cmd.add_argument("--source-root", type=Path, required=True)
    cmd.add_argument("--public-file", required=True)
    for action in ("inspect", "import-local"):
        cmd = sub.add_parser(action)
        cmd.add_argument("--layout", type=Path, required=True)
        if action == "import-local":
            cmd.add_argument("--docker", type=Path, required=True)
            cmd.add_argument("--docker-sha256", required=True)
            cmd.add_argument("--socket", type=Path, required=True)
            cmd.add_argument("--approve-digest", required=True)
    for action in ("preview-command", "execute-command"):
        cmd = sub.add_parser(action)
        cmd.add_argument("--spec", type=Path, required=True)
        if action == "execute-command":
            cmd.add_argument("--source-root", type=Path, required=True)
            cmd.add_argument("--approve-plan", required=True)
            cmd.add_argument("--allow-code-execution", action="store_true")
            cmd.add_argument("--allow-network", action="store_true")
            cmd.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args(arguments)
    try:
        if args.command == "pin":
            result = SourcePin.capture(
                args.source_root, args.public_file, public=True
            ).__dict__
        elif args.command in {"preview", "build"}:
            plan = BuildPlan.from_dict(strict_json(args.plan.read_text(), 1_048_576))
            result = (
                plan.preview()
                if args.command == "preview"
                else OciBuilder().build(plan, args.source_root, args.output).summary()
            )
        elif args.command == "inspect":
            result = inspect_oci(args.layout).summary()
        elif args.command == "import-local":
            importer = DockerLocalImporter(
                ToolPin(args.docker, args.docker_sha256), args.socket
            )
            result = importer.import_image(
                args.layout,
                LocalImportGrant(args.approve_digest, args.socket, time.time() + 60),
            )
        else:
            spec = strict_json(args.spec.read_text(), 1_048_576)
            build = BuildCommand(
                ToolPin(Path(spec["tool"]), spec["tool_sha256"]),
                tuple(spec["arguments"]),
                tuple(SourcePin(**pin) for pin in spec.get("inputs", [])),
            )
            if args.command == "preview-command":
                result = build.preview()
            else:
                result = build.execute(
                    args.source_root,
                    ExecutionGrant(
                        args.approve_plan,
                        time.time() + args.timeout,
                        args.allow_code_execution,
                        args.allow_network,
                    ),
                    limits=ProcessLimits(args.timeout),
                )
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("state", "succeeded") == "succeeded" else 1
    except (ValueError, KeyError, TypeError, OSError, InterruptedError):
        # Do not echo private paths, process output, manifests or attacker-controlled errors.
        print(
            json.dumps(
                {"state": "rejected", "reason": "invalid-or-unavailable-artifact-input"}
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
