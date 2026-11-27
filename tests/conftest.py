import difflib
import json
import os
import shutil
import tempfile
import time
import zlib
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


def _piceli_entries(directory: Path) -> set[str]:
    from piceli.tempfiles import is_temporary

    try:
        return {name for name in os.listdir(directory) if is_temporary(name)}
    except FileNotFoundError:
        return set()


def _leftovers(directory: Path, before: set[str], wait: float = 2.0) -> set[str]:
    """New piceli entries in ``directory``; waits briefly for background threads."""
    deadline = time.monotonic() + wait
    while True:
        left = _piceli_entries(directory) - before
        if not left or time.monotonic() >= deadline:
            return left
        time.sleep(0.05)


@pytest.fixture(scope="session", autouse=True)
def _no_leftover_temporary_files(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Path]:
    """Every temporary file or directory Piceli creates must be gone at the end.

    The session gets a private temporary directory (``TMPDIR`` and
    :data:`tempfile.tempdir`, so child processes use it too): other test runs
    on the machine cannot interfere, and a ``piceli-*`` or ``.piceli-*`` entry
    still there when the session ends fails the run. ``tmp_path`` keeps
    pytest's own base directory (created first, outside it).
    """
    base = tmp_path_factory.getbasetemp()
    root = Path(tempfile.mkdtemp(prefix="pytest-piceli-tmp-")).resolve()
    saved = (tempfile.tempdir, os.environ.get("TMPDIR"))
    tempfile.tempdir = str(root)
    os.environ["TMPDIR"] = str(root)
    try:
        yield root
        from piceli.tempfiles import tracked

        left = sorted(_leftovers(root, set()))
        still = [path for path in tracked() if Path(path).exists()]
        # Hidden work directories next to outputs (``.piceli-oci-*``) in the
        # tests' own tmp_path directories.
        from piceli.tempfiles import is_temporary

        beside = sorted(
            str(Path(folder, name).relative_to(base))
            for folder, folders, files in os.walk(base)
            for name in (*folders, *files)
            if is_temporary(name)
        )
        assert not left and not still and not beside, (
            f"piceli left temporary files behind: {left} {still} {beside}"
        )
    finally:
        tempfile.tempdir = saved[0]
        if saved[1] is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = saved[1]
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="session", autouse=True)
def _fake_image_scanner(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """0.94.0: builds, mirrors and copies scan each image with Grype. The
    suite never downloads it or its database: a stand-in that reports no
    finding is the default (``tests/fake_scanner.py``), and mirrors and
    copies keep their scan cache in the session's temporary space."""
    from tests.fake_scanner import write_scanner

    names = ("PICELI_SCANNER", "PICELI_SCAN_CACHE", "PICELI_SCAN_POLICY")
    saved = {name: os.environ.get(name) for name in names}
    root = tmp_path_factory.mktemp("scanner")
    os.environ["PICELI_SCANNER"] = str(write_scanner(root / "bin"))
    os.environ["PICELI_SCAN_CACHE"] = str(root / "cache")
    os.environ.pop("PICELI_SCAN_POLICY", None)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@pytest.fixture(autouse=True)
def _no_leftover_temporary_files_per_test(
    _no_leftover_temporary_files: Path,
) -> Iterator[None]:
    """Name the test that leaked (the session guard only says that one did)."""
    root = _no_leftover_temporary_files
    before = _piceli_entries(root)
    yield
    left = _leftovers(root, before)
    if left:
        for name in left:  # report once, not again for every later test
            entry = root / name
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
        pytest.fail(f"piceli left temporary files behind: {sorted(left)}")


@pytest.fixture(autouse=True)
def _no_leftover_docker_images() -> Iterator[None]:
    """Remove the images a test built under ``piceli-e2e/`` or
    ``piceli-examples/`` on every outcome, and fail the test when one stays
    (only when real images are built: ``tests/docker_images.py``)."""
    from tests import docker_images

    if not docker_images.enabled():
        yield
        return
    before = docker_images.images()
    try:
        yield
    finally:
        left = docker_images.remove(docker_images.images() - before)
    if left:
        pytest.fail(f"test left Docker images behind: {sorted(left)}")


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


def _tofu_binary() -> str | None:
    found = os.environ.get("PICELI_TOFU") or shutil.which("tofu")
    return found if found and Path(found).is_file() else None


def _keep_shard(config: pytest.Config, items: list[pytest.Item]) -> None:
    """``PICELI_TEST_SHARD=I/N`` keeps the I-th of N parts of the suite.

    CI splits the unit and acceptance tests over N runners. A test belongs to
    the part its node id hashes to (CRC-32, the same in every process and
    xdist worker), so the N parts together run every test exactly once.
    """
    spec = os.environ.get("PICELI_TEST_SHARD", "").strip()
    if not spec:
        return
    try:
        index, count = (int(part) for part in spec.split("/"))
    except ValueError:
        raise pytest.UsageError(
            f"PICELI_TEST_SHARD={spec!r}: expected I/N, e.g. 1/2"
        ) from None
    if not 1 <= index <= count:
        raise pytest.UsageError(f"PICELI_TEST_SHARD={spec!r}: need 1 <= I <= N")
    kept: list[pytest.Item] = []
    dropped: list[pytest.Item] = []
    for item in items:
        part = zlib.crc32(item.nodeid.encode()) % count
        (kept if part == index - 1 else dropped).append(item)
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = kept


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """``tofu`` tests run the real OpenTofu binary, the slowest in the suite:
    only with ``PICELI_TOFU_TESTS=1`` (and a ``tofu`` to run)."""
    _keep_shard(config, items)
    if os.environ.get("PICELI_TOFU_TESTS") != "1":
        reason = "runs the real OpenTofu binary; set PICELI_TOFU_TESTS=1"
    elif _tofu_binary() is None:
        reason = (
            "OpenTofu not found (set PICELI_TOFU or put tofu on PATH, "
            "e.g. nix shell nixpkgs#opentofu)"
        )
    else:
        return
    skip = pytest.mark.skip(reason=reason)
    for item in items:
        if item.get_closest_marker("tofu") is not None:
            item.add_marker(skip)


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
