"""Kill tests: ``release apply`` and ``release rollback`` killed at every step.

``piceli release apply`` runs in its own process against the fake API. The
fake server SIGKILLs it at an exact step of the execution (see
``FakeAPI.intercept``), for every write the execution sends:

* ``before-write``: the journal holds the action's intent, the write was
  never applied;
* ``after-write``: the server applied the write, the client never saw the
  response (the same client state as a crash between response and receipt);
* ``after-response``: the next request after the answered write (the
  receipt is recorded; readiness polling or the next action was starting).

Then the documented recovery must converge: ``release resume`` for an
interrupted apply of a new release, re-running ``release rollback`` for an
interrupted rollback (re-apply executions are not resumable). Convergence
means: the recovery succeeds, the live objects are exactly the release's,
the release is selected, and a new plan is all ``no-op``.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli.release import app
from piceli.testing import FakeAPI, serve
from tests.acceptance.fake_api import TARGET

DIGEST_1 = "sha256:" + "1" * 64
DIGEST_2 = "sha256:" + "2" * 64

COMPOSITION = """
from piceli.k8s.ops.plan import DeploymentComponent, DeploymentComposition, ResourceIntent


def build(ctx):
    meta = lambda name: {"name": name, "namespace": ctx.namespace}
    config = ResourceIntent.from_manifest(
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta("settings"),
         "data": {"mode": str(ctx.values["mode"])}}
    )
    token = ResourceIntent.from_manifest(
        {"apiVersion": "v1", "kind": "Secret", "metadata": meta("credential"),
         "data": {"password": "<private>"}}
    ).with_secret("/data/password", ctx.secret("password"))
    worker = ResourceIntent.from_manifest(
        {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta("worker"),
         "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "worker"}},
                  "template": {"metadata": {"labels": {"app": "worker"}},
                               "spec": {"containers": [
                                   {"name": "worker", "image": ctx.image("api")}]}}}}
    )
    extra = ()
    if ctx.values["feature"] == "on":
        extra = (ResourceIntent.from_manifest(
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta("feature"),
             "data": {"enabled": "true"}}
        ),)
    return DeploymentComposition((
        DeploymentComponent("config", (config, token, *extra)),
        DeploymentComponent("worker", (worker,), dependencies=("config",)),
    ))
"""

PHASES = ("before-write", "after-write", "after-response")
# Writes of a first apply (three creates) and of a rollback from a release
# with one more ConfigMap (two updates, the ConfigMap and the Deployment,
# then the prune delete; the Secret is unchanged).
APPLY_WRITES = 3
ROLLBACK_WRITES = 3

pytestmark = pytest.mark.timeout(180)


def _spec(
    directory: Path,
    url: str,
    digest: str,
    mode: str,
    feature: str = "off",
    *,
    state: str = "local",
    state_dir: str = "state",
) -> Path:
    (directory / "kubeconfig").write_text(
        textwrap.dedent(
            f"""
            apiVersion: v1
            kind: Config
            clusters: [{{name: fake, cluster: {{server: "{url}"}}}}]
            users: [{{name: nobody, user: {{}}}}]
            contexts: [{{name: fake, context: {{cluster: fake, user: nobody}}}}]
            """
        )
    )
    (directory / "compose.py").write_text(COMPOSITION)
    path = directory / "release.toml"
    path.write_text(
        textwrap.dedent(
            f"""
            [target]
            kubeconfig = "kubeconfig"
            context = "fake"
            namespace = "{TARGET.namespace}"
            cluster_uid = "cluster-uid"
            transport = "loopback-http"

            [release]
            name = "app"
            owner = "acceptance-owner"
            field_manager = "piceli-acceptance"
            composition = "compose.py:build"
            state_dir = "{state_dir}"
            state = "{state}"
            state_lease_seconds = 5
            prune = true

            [execution]
            max_seconds = 60
            readiness_seconds = 5
            poll_seconds = 0.05
            write_settle_seconds = 0.01

            [images]
            api = "registry.example/app/api@{digest}"

            [secrets.password]
            type = "random"
            bytes = 24

            [values]
            mode = "{mode}"
            feature = "{feature}"
            """
        )
    )
    return path


def _run(spec: Path, *args: str) -> tuple[int, dict[str, Any]]:
    result = CliRunner().invoke(app, [*args, "--spec", str(spec)])
    return result.exit_code, json.loads(result.stdout or "{}")


def _write(request: dict[str, Any]) -> bool:
    """A write to the release's objects (not to shared state: Leases, state Secrets)."""
    path = request["path"]
    return (
        request["method"] in {"POST", "PATCH", "DELETE"}
        and request["query"].get("dryRun") != ["All"]
        and "/leases" not in path
        and "piceli-state" not in path
        and not _state_body(request)
    )


def _state_body(request: dict[str, Any]) -> bool:
    body = request.get("body")
    labels = (body or {}).get("metadata", {}).get("labels") or {}
    return isinstance(body, dict) and "piceli.io/state" in labels


class Killer:
    """Kill the client process at write ``index`` (1-based), at ``phase``."""

    def __init__(self, index: int, phase: str) -> None:
        self.index, self.phase = index, phase
        self.writes = 0
        self.armed = False
        self.pid: int | None = None
        self.killed = threading.Event()
        self.lock = threading.Lock()

    def __call__(self, request: dict[str, Any], stage: str) -> bool:
        with self.lock:
            if self.killed.is_set() or self.pid is None:
                return False
            if self.armed and stage == "received":
                return self._kill()
            if not _write(request):
                return False
            if stage == "received":
                self.writes += 1
            if self.writes != self.index:
                return False
            if self.phase == "before-write" and stage == "received":
                return self._kill()
            if self.phase == "after-write" and stage == "committed":
                return self._kill()
            if self.phase == "after-response" and stage == "committed":
                self.armed = True
            return False

    def _kill(self) -> bool:
        assert self.pid is not None
        os.kill(self.pid, signal.SIGKILL)
        self.killed.set()
        return True


def _killed_apply(
    api: FakeAPI, spec: Path, command: list[str], plan_hash: str, killer: Killer
) -> None:
    api.intercept = killer
    env = {key: value for key, value in os.environ.items() if key != "KUBECONFIG"}
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "piceli",
            "release",
            *command,
            "--approve",
            plan_hash,
            "--spec",
            str(spec),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    with killer.lock:
        killer.pid = process.pid
    try:
        process.communicate(timeout=120)
    finally:
        api.intercept = None
    assert killer.killed.is_set(), "the kill point was never reached"
    assert process.returncode == -signal.SIGKILL


def _live(api: FakeAPI) -> dict[str, Any]:
    return {
        "mode": api.objects[("ConfigMap", "settings")]["data"]["mode"],
        "image": api.objects[("Deployment", "worker")]["spec"]["template"]["spec"][
            "containers"
        ][0]["image"],
        "secret": api.objects[("Secret", "credential")]["data"]["password"],
    }


def _converged(
    api: FakeAPI, spec: Path, release: str, digest: str, mode: str, *plan: str
) -> dict[str, Any]:
    live = _live(api)
    assert live["mode"] == mode
    assert live["image"].endswith(digest)
    code, status = _run(spec, "status")
    assert code == 0, status
    code, planned = _run(spec, *(plan or ("plan",)))
    assert code in {0, 3}, planned
    assert planned["release"] == release
    assert set(planned["summary"]) == {"no-op"}, planned["diffs"]
    return live


def test_uninterrupted_runs_send_the_expected_writes(tmp_path):
    with serve() as (api, url):
        spec = _spec(tmp_path, url, DIGEST_1, "blue")
        code, applied = _run(spec, "apply", "--auto-approve")
        assert code == 0, applied
        writes = [r for r in api.requests if _write(r)]
        assert len(writes) == APPLY_WRITES
        first = applied["release"]
        spec = _spec(tmp_path, url, DIGEST_2, "green", "on")
        code, applied = _run(spec, "apply", "--auto-approve")
        assert code == 0, applied
        assert ("ConfigMap", "feature") in api.objects
        before = len([r for r in api.requests if _write(r)])
        code, rolled = _run(spec, "rollback", first, "--auto-approve")
        assert code == 0 and rolled["selected"] == first, rolled
        assert len([r for r in api.requests if _write(r)]) - before == ROLLBACK_WRITES
        assert ("ConfigMap", "feature") not in api.objects


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("index", range(1, APPLY_WRITES + 1))
def test_killed_apply_resumes_to_the_release(tmp_path, index, phase):
    with serve() as (api, url):
        spec = _spec(tmp_path, url, DIGEST_1, "blue")
        code, planned = _run(spec, "plan")
        assert code == 0, planned
        _killed_apply(api, spec, ["apply"], planned["plan_hash"], Killer(index, phase))

        code, resumed = _run(spec, "resume")
        assert code == 0, resumed
        assert resumed["release_state"] == "ready", resumed
        live = _converged(api, spec, planned["release"], DIGEST_1, "blue")
        # A second resume is a no-op on the cluster.
        writes = len([r for r in api.requests if _write(r)])
        code, again = _run(spec, "resume")
        assert code == 0 and again["release_state"] == "ready", again
        assert len([r for r in api.requests if _write(r)]) == writes
        assert _live(api) == live


@pytest.mark.parametrize("phase", PHASES)
@pytest.mark.parametrize("index", range(1, ROLLBACK_WRITES + 1))
def test_killed_rollback_converges_when_rolled_back_again(tmp_path, index, phase):
    with serve() as (api, url):
        spec = _spec(tmp_path, url, DIGEST_1, "blue")
        code, applied = _run(spec, "apply", "--auto-approve")
        assert code == 0, applied
        first, secret = applied["release"], _live(api)["secret"]
        spec = _spec(tmp_path, url, DIGEST_2, "green", "on")
        code, applied = _run(spec, "apply", "--auto-approve")
        assert code == 0, applied
        assert applied["release"] != first

        code, planned = _run(spec, "rollback", first)
        assert code == 3, planned  # approval required
        _killed_apply(
            api, spec, ["rollback", first], planned["plan_hash"], Killer(index, phase)
        )

        # Re-apply executions are not resumable: roll back again.
        code, refused = _run(spec, "resume")
        assert code == 2 and refused["reason"] == "not-resumable", refused
        code, rolled = _run(spec, "rollback", first, "--auto-approve")
        assert code == 0, rolled
        assert rolled["selected"] == first, rolled
        live = _converged(api, spec, first, DIGEST_1, "blue", "rollback", first)
        # The Secret the releases share was never rewritten.
        assert live["secret"] == secret
        assert ("ConfigMap", "feature") not in api.objects


@pytest.mark.parametrize("index", range(1, APPLY_WRITES + 1))
def test_killed_apply_with_shared_state_resumes_on_another_runner(tmp_path, index):
    """Shared state and resume-resend together.

    The kill lands after the intent (with ``written_at``) was journaled and
    written through to the cluster state, before the write reached the server.
    Another runner (an empty state directory) takes the stale lock over,
    pulls the state and sends the unlanded write again: one write per object.
    """
    with serve() as (api, url):
        spec = _spec(tmp_path, url, DIGEST_1, "blue", state="cluster")
        code, planned = _run(spec, "plan")
        assert code == 0, planned
        _killed_apply(
            api, spec, ["apply"], planned["plan_hash"], Killer(index, "before-write")
        )
        assert len([r for r in api.requests if _write(r)]) == index

        other = _spec(tmp_path, url, DIGEST_1, "blue", state="cluster", state_dir="b")
        deadline = time.monotonic() + 30
        while True:
            code, resumed = _run(other, "resume")
            if code != 2 or resumed.get("reason") not in {
                "release-locked",
                "pipeline-locked",
            }:
                break
            assert time.monotonic() < deadline, resumed
            time.sleep(0.5)
        assert code == 0, resumed
        assert resumed["release_state"] == "ready", resumed
        # Each object written once, the interrupted write sent again.
        assert len([r for r in api.requests if _write(r)]) == APPLY_WRITES + 1
        _converged(api, other, planned["release"], DIGEST_1, "blue")
