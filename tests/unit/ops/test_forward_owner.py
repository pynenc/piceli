"""A forward follows the live owner of its Service or Deployment (no cluster).

The supervised "kubectl" records the target it was started with, then idles.
The resolver stands in for the API: the test changes which pods are Ready, as
a rollout does.
"""

from __future__ import annotations

import sys
import textwrap
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from piceli.k8s.access import LivePodResolver
from piceli.k8s.observe import ForwardSupervisor
from piceli.k8s.ui_config import UiShortcut

RECORDING_KUBECTL = textwrap.dedent(
    """\
    import os, sys, time

    with open(os.environ["TARGETS"], "a") as out:
        out.write(sys.argv[sys.argv.index("port-forward") + 1] + "\\n")
    time.sleep(600)
    """
)


def _wait(predicate: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


@pytest.fixture
def kubectl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    script = tmp_path / "kubectl"
    script.write_text(f"#!{sys.executable}\n{RECORDING_KUBECTL}")
    script.chmod(0o755)
    targets = tmp_path / "targets"
    monkeypatch.setenv("TARGETS", str(targets))
    return script, targets


def _supervisor(
    tmp_path: Path, script: Path, resolver: Callable[[str, str], list[str]] | None
) -> ForwardSupervisor:
    shortcut = UiShortcut.model_validate(
        {
            "id": "web",
            "label": "web",
            "target": "service/web",
            "local_port": 39123,
            "remote_port": 80,
            "health": {"type": "tcp", "interval": 1},
        }
    )
    return ForwardSupervisor(
        kubeconfig=tmp_path / "kubeconfig",
        context="lab",
        kubectl=str(script),
        shortcuts=[shortcut],
        namespace="demo",
        owner_resolver=resolver,
        owner_interval=0.05,
    )


def test_forward_reconnects_to_the_pod_that_replaces_its_owner(
    tmp_path: Path, kubectl: tuple[Path, Path]
) -> None:
    script, targets = kubectl
    live = ["web-old"]
    supervisor = _supervisor(tmp_path, script, lambda _ns, _target: list(live))
    try:
        supervisor.quick_start("web")
        assert _wait(lambda: targets.exists())
        assert targets.read_text().split() == ["pod/web-old"]
        assert supervisor.statuses()[0].pod == "web-old"

        live[:] = ["web-new", "web-old"]  # both ready: nothing moves
        supervisor.tick()
        time.sleep(0.1)
        supervisor.tick()
        assert targets.read_text().split() == ["pod/web-old"]

        live[:] = ["web-new"]  # the rollout removed the old pod
        assert _wait(
            lambda: (supervisor.tick(), supervisor.statuses()[0].pod)[1] == "web-new"
        )
        assert _wait(
            lambda: targets.read_text().split() == ["pod/web-old", "pod/web-new"]
        )
        assert supervisor.statuses()[0].restarts == 0  # not a failure
        assert supervisor.shortcuts_status("demo")[0]["pod"] == "web-new"
    finally:
        supervisor.close()


def test_forward_keeps_the_saved_target_when_the_owner_is_unknown(
    tmp_path: Path, kubectl: tuple[Path, Path]
) -> None:
    script, targets = kubectl

    def unreadable(_namespace: str, _target: str) -> list[str]:
        raise OSError("api down")

    supervisor = _supervisor(tmp_path, script, unreadable)
    try:
        supervisor.quick_start("web")
        assert _wait(lambda: targets.exists())
        assert targets.read_text().split() == ["service/web"]
        assert supervisor.statuses()[0].pod is None
    finally:
        supervisor.close()


class _Reader:
    def __init__(self, pods: list[dict[str, Any]]) -> None:
        self.pods_ = pods

    def workload(self, kind: str, namespace: str, name: str) -> dict[str, Any]:
        if kind == "Service":
            return {"spec": {"selector": {"app": name}}}
        return {"spec": {"selector": {"matchLabels": {"app": name}}}}

    def pods(self, namespace: str, labels: Any) -> list[dict[str, Any]]:
        return self.pods_


def _pod(
    name: str, created: str, *, ready: bool = True, deleting: bool = False
) -> dict:
    metadata: dict[str, Any] = {"name": name, "creationTimestamp": created}
    if deleting:
        metadata["deletionTimestamp"] = created
    return {
        "metadata": metadata,
        "status": {
            "phase": "Running",
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
        },
    }


def test_resolver_returns_ready_pods_newest_first_and_skips_terminating() -> None:
    pods = [
        _pod("a", "2026-01-01T00:00:01Z"),
        _pod("b", "2026-01-01T00:00:03Z"),
        _pod("c", "2026-01-01T00:00:04Z", ready=False),
        _pod("d", "2026-01-01T00:00:05Z", deleting=True),
    ]
    resolve = LivePodResolver(_Reader(pods))  # type: ignore[arg-type]
    assert resolve("demo", "service/web") == ["b", "a"]
    assert resolve("demo", "deployment/web") == ["b", "a"]
    assert resolve("demo", "pod/web-1") == []
