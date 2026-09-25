import difflib
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from kubernetes import config as kube_config


class AmbientClusterAccessError(RuntimeError):
    """Raised when a non-integration test tries to load an ambient kube config."""


def _refuse_ambient_config(*args: Any, **kwargs: Any) -> None:
    raise AmbientClusterAccessError(
        "unit/acceptance tests must not load an ambient kubeconfig or in-cluster "
        "config; mock the client or use the fake API (integration tests are exempt)"
    )


@pytest.fixture(autouse=True)
def _no_foreign_process_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail a test that signals init's group or the test runner's own group.

    A mocked ``Popen`` has a ``pid`` that converts to ``1``; ``os.killpg(1,
    SIGTERM)`` is refused on a laptop but kills the CI job's processes.
    """
    real_killpg = os.killpg

    def guarded(pgid: int, signum: int) -> None:
        if pgid <= 1 or pgid == os.getpgrp():
            raise AssertionError(f"test tried to signal process group {pgid}")
        real_killpg(pgid, signum)

    monkeypatch.setattr(os, "killpg", guarded)


@pytest.fixture(autouse=True)
def _no_ambient_cluster(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    """Guarantee that only tests under tests/integration can reach a real cluster.

    The developer's current kube context may point at a shared or production
    cluster; an unmocked code path must fail loudly instead of reaching it.
    """
    if "integration" in Path(str(request.node.path)).parts:
        yield
        return
    missing = tmp_path_factory.getbasetemp() / "no-ambient-kubeconfig"
    monkeypatch.setenv("KUBECONFIG", str(missing))
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.delenv("KUBERNETES_SERVICE_PORT", raising=False)
    monkeypatch.setattr(kube_config, "load_kube_config", _refuse_ambient_config)
    monkeypatch.setattr(kube_config, "load_incluster_config", _refuse_ambient_config)
    yield


def pytest_assertrepr_compare(op: str, left: Any, right: Any) -> list[str] | None:
    if isinstance(left, dict) and isinstance(right, dict) and op == "==":
        # Serialize the dictionaries to JSON formatted strings, sorted to ensure consistency
        left_str = json.dumps(left, sort_keys=True, indent=2).splitlines(keepends=True)
        right_str = json.dumps(right, sort_keys=True, indent=2).splitlines(
            keepends=True
        )
        # Generate the unified diff
        diff = list(
            difflib.unified_diff(left_str, right_str, fromfile="left", tofile="right")
        )
        if diff:
            return [
                "Dictionaries do not match:",
                *diff,
                # "left:",
                # *left_str,
                # "right:",
                # *right_str,
            ]
    return None


RESOURCE_JSON = '{"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": "tasker-scheduler"}, "spec": {"template": {"metadata": {"name": "tasker-scheduler"}, "spec": {"containers": [{"command": ["sh", "-c", "echo \'scheduler\' && sleep 30"], "image": "busybox", "name": "tasker-scheduler"}], "restartPolicy": "Never"}}}}'


@pytest.fixture
def resource_dict() -> dict:
    """Parse JSON string to a Python dictionary."""
    return json.loads(RESOURCE_JSON)
