"""0.15.1: a corrupted controller image is caught at start and reported as down.

Reproduces a real incident: one ``.pyc`` of the controller's unpacked image
had different bytes on one node; the controller crash-looped for hours with
an unrelated import error while ``gitops status`` and the UI showed its last
document as current.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from piceli import integrity
from piceli.gitops import liveness
from piceli.gitops.config import ControllerConfig
from piceli.gitops.install import InstallSettings, render_controller

ROOT = Path(__file__).resolve().parents[2]
NOW = 1_800_000_000.0  # 2027-01-15T08:00:00Z
RECENT = "2027-01-15T07:59:30Z"
OLD = "2027-01-15T06:00:00Z"


def _tree(tmp_path: Path) -> tuple[Path, Path]:
    site = tmp_path / "site"
    (site / "pkg" / "__pycache__").mkdir(parents=True)
    (site / "pkg" / "mod.py").write_text("x = 1\n")
    (site / "pkg" / "__pycache__" / "mod.cpython-313.pyc").write_bytes(b"\x00" * 64)
    manifest = tmp_path / "share" / "files.sha256"
    assert integrity.write(manifest, [site]) == 2
    return site, manifest


def test_an_intact_tree_verifies_and_one_altered_byte_does_not(tmp_path: Path) -> None:
    site, manifest = _tree(tmp_path)
    assert integrity.verify(manifest) == []
    pyc = site / "pkg" / "__pycache__" / "mod.cpython-313.pyc"
    data = bytearray(pyc.read_bytes())
    data[10] ^= 0xFF  # same length, one byte different
    pyc.write_bytes(bytes(data))
    assert integrity.verify(manifest) == [str(pyc)]
    (site / "pkg" / "mod.py").unlink()
    assert sorted(integrity.verify(manifest)) == sorted(
        [str(pyc), str(site / "pkg" / "mod.py")]
    )


def test_the_self_check_stops_with_one_clear_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    site, manifest = _tree(tmp_path)
    (site / "pkg" / "mod.py").write_text("x = 2\n")
    log = tmp_path / "termination-log"
    env = {integrity.ENV: str(manifest), "PICELI_TERMINATION_LOG": str(log)}
    with pytest.raises(SystemExit) as stopped:
        integrity.self_check(env)
    assert stopped.value.code == integrity.EXIT_CORRUPTED
    message = log.read_text()
    assert message.startswith(
        "controller image files corrupted on this node; remove the image from "
        "the node's containerd and restart"
    )
    assert str(site / "pkg" / "mod.py") in message
    assert integrity.MESSAGE in capsys.readouterr().err


def test_no_variable_checks_nothing_and_a_missing_manifest_is_skipped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    integrity.self_check({})  # a laptop: nothing to check, nothing printed
    assert capsys.readouterr().err == ""
    integrity.self_check({integrity.ENV: str(tmp_path / "absent")})  # an older image
    assert "self-check skipped" in capsys.readouterr().err


def test_the_piceli_command_checks_before_importing_anything_else(
    tmp_path: Path,
) -> None:
    """The real entry point: corrupted files never get imported first."""
    site, manifest = _tree(tmp_path)
    (site / "pkg" / "mod.py").write_text("tampered\n")
    env = {
        **os.environ,
        integrity.ENV: str(manifest),
        "PICELI_TERMINATION_LOG": str(tmp_path / "log"),
    }
    code = (
        "import sys\n"
        "from piceli.__main__ import main\n"
        "try:\n"
        "    main()\n"
        "finally:\n"
        "    heavy = [m for m in sys.modules if m.startswith(('piceli.k8s', 'kubernetes', 'typer'))]\n"
        "    print('HEAVY', heavy, file=sys.stderr)\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code, "gitops", "run"],
        env=env, capture_output=True, text=True, timeout=60, cwd=ROOT,
    )  # fmt: skip
    assert done.returncode == integrity.EXIT_CORRUPTED, done.stderr
    assert integrity.MESSAGE in done.stderr
    assert "HEAVY []" in done.stderr


def test_the_manifest_of_this_installation_covers_the_controller_imports(
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "files.sha256"
    integrity.write(manifest)
    text = manifest.read_text()
    assert "/json/__init__.py" in text  # the standard library
    assert "/kubernetes/client/api/" in text  # the package that broke on a real node


def test_the_controller_deployment_runs_the_self_check_and_keeps_crash_lines() -> None:
    config = ControllerConfig(
        pipeline="deploy/app.py:pipeline",
        repo="https://example.com/app.git",
        branches=("main",),
    )
    deployment = render_controller(
        config, InstallSettings(image="registry.example/c@sha256:" + "1" * 64)
    )[-1]
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert {"name": integrity.ENV, "value": integrity.MANIFEST} in container["env"]
    assert container["terminationMessagePolicy"] == "FallbackToLogsOnError"


# ---------------------------------------------------------------- liveness


def _pod(**container: Any) -> dict[str, Any]:
    return {
        "metadata": {
            "name": "piceli-gitops-7d9",
            "creationTimestamp": "2027-01-15T07:00:00Z",
        },
        "spec": {"nodeName": "node-a"},
        "status": {
            "phase": "Running",
            "containerStatuses": [{"name": "controller", **container}],
        },
    }


DOCUMENT = {"controller": {"state": "running", "last_poll": OLD, "poll_seconds": 30}}


def test_a_crash_loop_on_corrupted_files_reads_down_with_the_message() -> None:
    pod = liveness.pod_summary([
        _pod(
            ready=False, restartCount=37,
            state={"waiting": {"reason": "CrashLoopBackOff"}},
            lastState={"terminated": {"exitCode": 70, "reason": "Error", "finishedAt": "2027-01-15T07:58:00Z",
                                      "message": f"{integrity.MESSAGE} (1 file(s) differ from the image: /x.pyc)"}},
        )
    ])  # fmt: skip
    assert pod is not None and pod["self_check"] == "files-corrupted"
    assert (pod["restarts"], pod["waiting"], pod["node"]) == (
        37,
        "CrashLoopBackOff",
        "node-a",
    )
    live = liveness.liveness(DOCUMENT, pod, NOW)
    assert live["state"] == "down"
    assert live["message"] == integrity.MESSAGE
    assert live["status_is_stale"] is True
    assert OLD in live["note"]


def test_any_other_crash_shows_its_reason_restarts_and_last_error_line() -> None:
    traceback = 'Traceback (most recent call last):\n  File "x"\nValueError: code: co_varnames is too small\n'
    pod = liveness.pod_summary([
        _pod(ready=False, restartCount=5, state={"waiting": {"reason": "CrashLoopBackOff"}},
             lastState={"terminated": {"exitCode": 1, "reason": "Error", "message": traceback}}),
    ])  # fmt: skip
    live = liveness.liveness(DOCUMENT, pod, NOW)
    assert live["state"] == "down"
    assert live["message"] == (
        "CrashLoopBackOff, 5 restarts; last error: ValueError: code: co_varnames is too small"
    )


def test_running_stale_and_starting() -> None:
    ready = liveness.pod_summary(
        [_pod(ready=True, restartCount=0, state={"running": {}})]
    )
    fresh = {"controller": {**DOCUMENT["controller"], "last_poll": RECENT}}
    assert liveness.liveness(fresh, ready, NOW)["state"] == "running"
    assert liveness.liveness(fresh, ready, NOW)["status_is_stale"] is False
    # Ready but no poll for a long time: the document is old.
    stale = liveness.liveness(DOCUMENT, ready, NOW)
    assert stale["state"] == "stale" and stale["status_is_stale"] is True
    first = liveness.pod_summary(
        [
            _pod(
                ready=False,
                restartCount=0,
                state={"waiting": {"reason": "ContainerCreating"}},
            )
        ]
    )
    assert liveness.liveness(None, first, NOW)["state"] == "down"
    # Pods not readable (RBAC): only the age of the last poll decides.
    assert liveness.liveness(fresh, None, NOW, pod_readable=False)["state"] == "running"
    assert (
        liveness.liveness(DOCUMENT, None, NOW, pod_readable=False)["state"] == "stale"
    )


def test_check_reads_the_controller_pods_through_the_api() -> None:
    asked: list[str] = []

    class Api:
        def call(self, path: str, method: str) -> Any:
            asked.append(path)
            return {
                "items": [
                    _pod(
                        ready=False,
                        restartCount=3,
                        state={"waiting": {"reason": "CrashLoopBackOff"}},
                    )
                ]
            }

    live = liveness.check(Api(), "piceli-system", DOCUMENT, NOW)
    assert live["state"] == "down" and live["pod"]["restarts"] == 3
    assert asked == [
        "/api/v1/namespaces/piceli-system/pods?labelSelector=app.kubernetes.io/name%3Dpiceli-gitops"
    ]

    class Forbidden:
        def call(self, path: str, method: str) -> Any:
            raise RuntimeError("403")

    assert (
        liveness.check(Forbidden(), "piceli-system", DOCUMENT, NOW)["state"] == "stale"
    )
