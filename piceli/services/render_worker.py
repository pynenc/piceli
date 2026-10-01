"""Container entrypoint. Never invoke this module to evaluate code on the host."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import socket
import sys
import tempfile
import time
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


def _wait_for_egress_denial(raw: str) -> None:
    """Keep consumer code unimported until the renderer's CNI policy settles.

    Some CNIs attach a new Job Pod before programming its NetworkPolicy. A
    short-lived renderer must never use that initial open interval. The
    installed API endpoint is an explicit, normally reachable canary; the
    second probe checks the outside path. Neither sends a credential.
    """
    endpoint = json.loads(raw)
    if (
        not isinstance(endpoint, list)
        or len(endpoint) != 2
        or not isinstance(endpoint[0], str)
        or not endpoint[0]
        or not isinstance(endpoint[1], int)
        or not 1 <= endpoint[1] <= 65535
    ):
        raise ValueError("invalid egress probe")
    started = time.monotonic()
    denied_in_row = 0
    while time.monotonic() - started < 45:
        denied = True
        for address, port in (tuple(endpoint), ("1.1.1.1", 443)):
            try:
                with socket.create_connection((address, port), timeout=0.5):
                    denied = False
            except OSError:
                pass
        denied_in_row = denied_in_row + 1 if denied else 0
        if denied_in_row >= 3 and time.monotonic() - started >= 12:
            return
        time.sleep(0.5)
    raise RuntimeError("renderer egress policy did not become effective")


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
            egress_probe = os.environ.get("PICELI_RENDER_EGRESS_PROBE")
            if egress_probe is not None:
                phase = "egress"
                _wait_for_egress_denial(egress_probe)
            phase = "source"
            with contextlib.redirect_stdout(sys.stderr):
                composition, app = render_app_target(
                    load_target(request["entrypoint"], source_root), context
                )
                phase = "serialize"
                result = serialize(composition, inputs)
                if app and app.pre_rollouts:
                    result["pre_rollouts"] = [
                        item.model_dump(mode="json") for item in app.pre_rollouts
                    ]
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
