"""Container entrypoint. Never invoke this module to evaluate code on the host."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import Any

from piceli.app.render import load_target, placeholder_inputs, render_app_target
from piceli.k8s.ops.plan import DeploymentComposition
from piceli.services.evaluation import RenderInputs


def _copy_approved_source(
    source: Path, destination: Path, expected: dict[str, str]
) -> None:
    """Freeze the checked ConfigMap bytes before importing consumer code."""
    for name, digest in expected.items():
        relative = PurePosixPath(name)
        if (
            not name
            or relative.is_absolute()
            or ".." in relative.parts
            or str(relative) != name
            or not isinstance(digest, str)
            or len(digest) != 64
        ):
            raise ValueError("invalid source digest")
        content = (source / name).read_bytes()
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("source digest mismatch")
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


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
        with contextlib.ExitStack() as cleanup:
            source_root = Path("/source")
            raw_request = Path(sys.argv[1]).read_bytes()
            expected_request = os.environ.get("PICELI_RENDER_REQUEST_SHA256")
            expected_source = os.environ.get("PICELI_RENDER_SOURCE_HASHES")
            if expected_request is not None or expected_source is not None:
                if not expected_request or expected_source is None:
                    raise ValueError("incomplete render digest")
                if hashlib.sha256(raw_request).hexdigest() != expected_request:
                    raise ValueError("request digest mismatch")
                hashes = json.loads(expected_source)
                if not isinstance(hashes, dict) or not all(
                    isinstance(key, str) for key in hashes
                ):
                    raise ValueError("invalid source digests")
                temporary = Path(
                    cleanup.enter_context(tempfile.TemporaryDirectory(dir="/tmp"))
                )
                source_root = temporary / "source"
                _copy_approved_source(Path("/source"), source_root, hashes)
            request = json.loads(raw_request)
            phase = "inputs"
            inputs = RenderInputs.from_dict(request["inputs"])
            context = inputs.context(placeholder_inputs(list(inputs.secret_names)))
            phase = "source"
            with contextlib.redirect_stdout(sys.stderr):
                composition, app = render_app_target(
                    load_target(request["entrypoint"], source_root), context
                )
                phase = "serialize"
                result = serialize(composition, inputs)
                # Only the count leaves the container: the service refuses to plan
                # an app whose pre-rollout checks the UI deploy path cannot run.
                result["pre_rollout_checks"] = len(app.pre_rollouts) if app else 0
        output = json.dumps(result, separators=(",", ":"), allow_nan=False)
        if os.environ.get("PICELI_RENDER_RESULT_LINE") == "1":
            sys.stdout.write("PICELI_RENDER_RESULT_V1:" + output + "\n")
        else:
            sys.stdout.write(output)
        return 0
    except BaseException as error:
        # Consumer exception text can contain source literals; it is not a DTO.
        sys.stderr.write(
            "evaluation failed: " + type(error).__name__ + " (" + phase + ")\n"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
