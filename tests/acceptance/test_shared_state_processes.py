"""Acceptance: two real ``piceli deploy`` processes against one (fake) cluster.

Each process is a separate runner (its own state directory) running
``python -m piceli deploy`` against the in-process fake API server. The
pipeline has no build (a pinned image), so no Docker is needed. Deployments
are held not-ready while a process should keep the release lock.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from piceli.testing import TARGET, FakeAPI, serve

IMAGE = "docker.io/library/redis:7.2.3@sha256:" + "d" * 64

MODULE = f"""
import os

from piceli import App, Pipeline, Random, Secrets, Target

target = Target.kubeconfig(
    "kubeconfig", context="fake", namespace="{TARGET.namespace}",
    transport="loopback-http",
)
app = App("shop")
secrets = Secrets(password=Random(24))
credentials = app.secret("credentials", {{"password": secrets.ref("password")}})
app.deployment("web", image="{IMAGE}", env={{"PASSWORD": credentials.key("password")}})
pipeline = Pipeline(
    app, target, secrets=secrets,
    state_dir=os.environ["DEPLOY_STATE_DIR"], state="cluster",
    state_lease_seconds=int(os.environ.get("LEASE", "5")),
    execution={{
        "max_seconds": 120,
        "readiness_seconds": float(os.environ.get("READINESS", "2")),
        "poll_seconds": 0.2,
    }},
)
"""

pytestmark = pytest.mark.timeout(180)


@pytest.fixture
def cluster(tmp_path: Path) -> Iterator[tuple[FakeAPI, Path]]:
    with serve() as (api, url):
        (tmp_path / "kubeconfig").write_text(
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
        (tmp_path / "app.py").write_text(MODULE)
        yield api, tmp_path


def start(
    root: Path, runner: str, *args: str, readiness: float = 2, lease: int = 5
) -> subprocess.Popen[str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PICELI_", "KUBECONFIG"))
    }
    env.update(
        DEPLOY_STATE_DIR=str(root / f"runner-{runner}"),
        READINESS=str(readiness),
        LEASE=str(lease),
        PYTHONPATH=str(Path(__file__).resolve().parents[2]),
    )
    return subprocess.Popen(
        [sys.executable, "-m", "piceli", "deploy", "app.py:pipeline", *args, "--json"],
        cwd=root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


def finish(process: subprocess.Popen[str]) -> tuple[int, dict[str, Any], str]:
    stdout, stderr = process.communicate(timeout=120)
    lines = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    return process.returncode, (lines[-1] if lines else {}), stderr


def holder(api: FakeAPI) -> str | None:
    with api.lock:
        lease = api.objects.get(("Lease", "piceli-lock-shop"))
        return None if lease is None else lease["spec"].get("holderIdentity")


def wait_for(condition: Any, seconds: float = 60) -> None:
    end = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > end:
            raise AssertionError("condition not reached")
        time.sleep(0.05)


def applying(api: FakeAPI, process: subprocess.Popen[str]) -> bool:
    assert process.poll() is None, process.communicate()
    with api.lock:
        created = ("Deployment", "web") in api.objects
    return created and f"/{process.pid}/" in (holder(api) or "")


def test_two_concurrent_deploys_of_one_release_never_interleave(cluster) -> None:
    api, root = cluster
    api.ready = False  # the first deploy waits for readiness, holding the lock
    first = start(root, "a", "--auto-approve", readiness=90)
    wait_for(lambda: applying(api, first))
    second = start(root, "b", "--auto-approve")
    code, body, stderr = finish(second)
    assert code == 2, stderr
    assert body["reason"] == "pipeline-locked"
    assert f"/{first.pid}/" in body["lock"]["holder"]
    assert first.poll() is None  # still applying: refused, not queued behind it
    api.ready = True
    code, body, stderr = finish(first)
    assert code == 0 and body["state"] == "ready", stderr
    assert holder(api) is None  # released when the first run ended
    code, body, stderr = finish(start(root, "b", "--auto-approve"))
    assert code == 0 and body["stages"]["apply"] == "skipped", stderr


def lease_expired(api: FakeAPI) -> bool:
    from piceli.state.cluster import parse_time

    with api.lock:
        spec = api.objects[("Lease", "piceli-lock-shop")]["spec"]
    renewed = parse_time(spec["renewTime"]) or 0
    return renewed + spec["leaseDurationSeconds"] < time.time() - 0.5


def test_a_killed_holder_is_taken_over_after_its_lease_expires(cluster) -> None:
    api, root = cluster
    api.ready = False
    victim = start(root, "a", "--auto-approve", readiness=90, lease=20)
    wait_for(lambda: applying(api, victim))
    time.sleep(0.5)
    victim.kill()  # SIGKILL, no cleanup: the lease stays held
    victim.communicate(timeout=30)
    code, body, stderr = finish(start(root, "b", "--resume", lease=20))
    assert (code, body.get("reason")) == (2, "pipeline-locked"), stderr
    assert f"/{victim.pid}/" in body["lock"]["holder"]
    api.ready = True
    wait_for(lambda: lease_expired(api), seconds=40)  # nobody renews it
    code, body, stderr = finish(start(root, "c", "--resume", lease=20))
    assert code == 0 and body["state"] == "ready", stderr
    taken = stderr.split("took over the expired lock of 'shop' from ", 1)[1]
    assert f"/{victim.pid}/" in taken.split()[0]


def test_an_interrupted_holder_frees_the_lock_and_another_runner_resumes(
    cluster,
) -> None:
    api, root = cluster
    api.ready = False
    victim = start(root, "a", "--auto-approve", readiness=60)
    wait_for(lambda: applying(api, victim))
    victim.send_signal(signal.SIGTERM)  # a cancelled CI job
    code, body, stderr = finish(victim)
    assert code == 1 and body["state"] == "interrupted", stderr
    assert holder(api) is None
    api.ready = True
    code, body, stderr = finish(start(root, "b", "--resume"))
    assert code == 0 and body["state"] == "ready", stderr
    assert body["run_id"]
    secret = api.objects[("Secret", "credentials")]["data"]["password"]
    # The resumed run applied the secret the first runner generated.
    code, body, stderr = finish(start(root, "c", "--auto-approve"))
    assert code == 0 and body["stages"]["apply"] == "skipped", stderr
    assert api.objects[("Secret", "credentials")]["data"]["password"] == secret
