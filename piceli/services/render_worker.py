"""Container entrypoint. Never invoke this module to evaluate code on the host."""

from __future__ import annotations

import contextlib
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from piceli.app.render import load_target, placeholder_inputs, render_target
from piceli.k8s.ops.plan import DeploymentComposition
from piceli.services.evaluation import RenderInputs


def serialize(
    composition: DeploymentComposition, inputs: RenderInputs
) -> dict[str, Any]:
    names = {
        reference: name
        for name, reference in placeholder_inputs(list(inputs.secret_names)).items()
    }
    components = []
    for component in composition.components:
        resources = []
        for resource in component.resources:
            resources.append(
                {
                    "manifest": resource.manifest,
                    "dependencies": [
                        asdict(dependency) for dependency in resource.dependencies
                    ],
                    "bindings": [
                        {
                            "pointer": binding.json_pointer,
                            "input_name": names[binding.reference],
                        }
                        for binding in resource.secret_bindings
                    ],
                }
            )
        components.append(
            {
                "name": component.name,
                "dependencies": list(component.dependencies),
                "resources": resources,
            }
        )
    return {"components": components}


def main() -> int:
    phase = "request"
    try:
        request = json.loads(Path(sys.argv[1]).read_bytes())
        phase = "inputs"
        inputs = RenderInputs.from_dict(request["inputs"])
        context = inputs.context(placeholder_inputs(list(inputs.secret_names)))
        phase = "source"
        with contextlib.redirect_stdout(sys.stderr):
            composition = render_target(
                load_target(request["entrypoint"], Path("/source")), context
            )
            phase = "serialize"
            result = serialize(composition, inputs)
        sys.stdout.write(json.dumps(result, separators=(",", ":"), allow_nan=False))
        return 0
    except BaseException as error:
        # Consumer exception text can contain source literals; it is not a DTO.
        sys.stderr.write(
            "evaluation failed: " + type(error).__name__ + " (" + phase + ")\n"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
