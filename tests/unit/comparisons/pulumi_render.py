"""Run the Pulumi comparison program with Pulumi's own mocks; print its objects.

``python pulumi_render.py <program dir> <stack>`` loads ``Pulumi.<stack>.yaml``
as the stack configuration, runs ``__main__.py`` against
``pulumi.runtime.set_mocks`` (no Pulumi CLI, engine, state backend or cluster)
and prints the inputs of every Kubernetes object as one JSON list. Needs the
packages in ``examples/comparisons/pulumi/requirements.txt``.
"""

from __future__ import annotations

import asyncio
import json
import runpy
import sys
from pathlib import Path
from typing import Any

import pulumi
import yaml
from pulumi.runtime.stack import wait_for_rpcs


class _Recorder(pulumi.runtime.Mocks):
    def __init__(self) -> None:
        self.objects: list[dict[str, Any]] = []

    def new_resource(self, args: pulumi.runtime.MockResourceArgs) -> tuple[str, dict]:
        if args.typ.startswith("kubernetes:"):
            self.objects.append(dict(args.inputs))
        return f"{args.name}-id", dict(args.inputs)

    def call(self, args: pulumi.runtime.MockCallArgs) -> dict:
        return {}


def main(program: Path, stack: str) -> None:
    project = yaml.safe_load((program / "Pulumi.yaml").read_text())["name"]
    config = yaml.safe_load((program / f"Pulumi.{stack}.yaml").read_text())["config"]
    pulumi.runtime.set_all_config(
        {k: v if isinstance(v, str) else json.dumps(v) for k, v in config.items()}
    )
    recorder = _Recorder()
    loop = asyncio.new_event_loop()  # Python 3.14 has no implicit loop
    asyncio.set_event_loop(loop)
    pulumi.runtime.set_mocks(recorder, project=project, stack=stack, preview=False)
    runpy.run_path(str(program / "__main__.py"), run_name="__main__")
    loop.run_until_complete(wait_for_rpcs())
    json.dump(recorder.objects, sys.stdout)


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2])
