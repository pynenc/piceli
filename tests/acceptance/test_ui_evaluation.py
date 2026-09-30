"""Opt-in real Docker isolation; never contacts any Kubernetes context."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from piceli.services.evaluation import DockerEvaluator, RenderInputs, SourceSelection
from tests.ui_render_support import renderer_image

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("PICELI_DOCKER_EVALUATION") != "1",
        reason="explicit Docker evaluator acceptance opt-in required",
    ),
    pytest.mark.timeout(300),
]


def test_real_renderer_has_no_credentials_network_or_host_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "source"
    root.mkdir()
    private = tmp_path / "private-credential"
    private.write_text("HOST-PRIVATE-CREDENTIAL")
    monkeypatch.setenv("PICELI_TEST_PRIVATE_TOKEN", "HOST-PRIVATE-TOKEN")
    (root / "compose.py").write_text("""
import os, socket
from pathlib import Path
from piceli.k8s.ops.plan import DeploymentComponent, DeploymentComposition, ResourceIntent

def build(ctx):
    assert "PICELI_TEST_PRIVATE_TOKEN" not in os.environ
    assert not Path("/var/run/docker.sock").exists()
    assert not (Path.home() / ".kube/config").exists()
    assert os.getuid() == 65532
    assert len(Path("/proc/net/route").read_text().splitlines()) == 1
    try:
        socket.create_connection(("192.0.2.1", 80), timeout=0.1)
    except OSError:
        pass
    else:
        raise AssertionError("network egress succeeded")
    try:
        Path("/source/unauthorized").write_text("bad")
    except OSError:
        pass
    else:
        raise AssertionError("source is writable")
    try:
        Path("/unauthorized").write_text("bad")
    except OSError:
        pass
    else:
        raise AssertionError("root is writable")
    resource = ResourceIntent.from_manifest({"apiVersion":"v1","kind":"ConfigMap","metadata":{"name":"checked","namespace":ctx.namespace},"data":{"result":"isolated"}})
    return DeploymentComposition((DeploymentComponent("app", (resource,)),))
""")
    inputs = RenderInputs("demo", {}, {}, (), {})
    with renderer_image() as renderer:
        evaluator = DockerEvaluator(tmp_path / "store", renderer)
        preview = evaluator.preview(
            "app", SourceSelection(root, ("compose.py",), "compose:build"), inputs
        )
        result = evaluator.render(preview.id, preview.digest)
        assert result.components[0]["resources"][0]["manifest"]["data"] == {
            "result": "isolated"
        }
        assert private.read_text() == "HOST-PRIVATE-CREDENTIAL"
        assert not (evaluator.store / preview.id / "source").exists()
        assert "HOST-PRIVATE" not in json.dumps(result.to_dict())


def test_real_renderer_bounds_output_time_and_cancellation(tmp_path: Path) -> None:
    import threading
    from dataclasses import replace

    from piceli.services.evaluation import EvaluationError

    with renderer_image() as renderer:
        for scenario, code in (
            ("timeout", "while True: pass"),
            ("output", "while True: print('x' * 10000, flush=True)"),
            ("cancel", "while True: pass"),
        ):
            directory = tmp_path / scenario
            directory.mkdir()
            root = directory / "source"
            root.mkdir()
            (root / "compose.py").write_text(code)
            bounded = replace(renderer, max_seconds=2, max_output_bytes=64 * 1024)
            evaluator = DockerEvaluator(directory / "store", bounded)
            preview = evaluator.preview(
                "app",
                SourceSelection(root, ("compose.py",), "compose:build"),
                RenderInputs("demo", {}, {}, (), {}),
            )
            cancelled = threading.Event()
            timer = threading.Timer(0.7, cancelled.set)
            if scenario == "cancel":
                timer.start()
            try:
                with pytest.raises(EvaluationError, match="evaluation-renderer"):
                    evaluator.render(preview.id, preview.digest, cancel=cancelled)
            finally:
                timer.cancel()
                if scenario == "cancel":
                    timer.join()
            assert not (evaluator.store / preview.id / "source").exists()
            assert (
                evaluator._docker(
                    [
                        "ps",
                        "--all",
                        "--quiet",
                        "--filter",
                        f"label=piceli.evaluation={preview.id}",
                    ]
                ).strip()
                == b""
            )
