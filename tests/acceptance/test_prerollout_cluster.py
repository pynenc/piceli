"""Acceptance: the pre-rollout cluster part against the fake API.

A fake clock drives the waits, so a timeout costs no real time. Every test
ends with the check Job gone: a check never leaves objects behind.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from piceli.pipeline.errors import PipelineError
from piceli.pipeline.prerollout_cluster import PreRolloutCluster, Unreadable
from piceli.testing import TARGET, manifest


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def job(name: str = "db-cfg-abc", timeout: int = 10) -> dict[str, Any]:
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": TARGET.namespace,
            "labels": {
                "piceli.io/pre-rollout": "config",
                "piceli.io/pre-rollout-app": "shop",
            },
        },
        "spec": {
            "activeDeadlineSeconds": timeout,
            "template": {
                "metadata": {"labels": {"piceli.io/pre-rollout": "config"}},
                "spec": {"containers": [{"name": "check", "image": "x"}]},
            },
        },
    }


@pytest.fixture
def cluster(local_api):
    api, provider = local_api
    clock = Clock()
    yield (
        api,
        PreRolloutCluster(
            provider.client,
            TARGET.namespace,
            poll_seconds=1,
            mount_grace_seconds=5,
            clock=clock,
            sleep=clock.sleep,
        ),
        clock,
    )


def leftovers(api: Any) -> list[tuple[str, str]]:
    return [key for key in api.objects if key[0] in {"Job", "Pod"}]


def test_a_passing_job_is_removed_and_a_failing_one_keeps_only_a_scrubbed_tail(
    cluster,
) -> None:
    api, runner, _ = cluster
    secret = manifest("Secret", "db-config")
    secret["data"] = {"token": base64.b64encode(b"s3cr3t-value").decode()}
    api.put(secret)
    api.job_result("ok-", logs="fine\n")
    assert runner.run_job(job("ok-1")).state == "passed"
    assert leftovers(api) == []

    api.job_result(
        "bad-",
        exit_code=2,
        logs="password=hunter22\nvalue s3cr3t-value here\n"
        "key AKIAIOSFODNN7EXAMPLE12345 used\nplain line\n",
    )
    values = runner.secret_values(["db-config", "missing"])
    assert values == ["s3cr3t-value"]
    outcome = runner.run_job(job("bad-1"), redact=values)
    assert (outcome.state, outcome.exit_code, outcome.cleaned) == ("failed", 2, True)
    assert "hunter22" not in outcome.log_tail
    assert "s3cr3t-value" not in outcome.log_tail
    assert "AKIAIOSFODNN7EXAMPLE12345" not in outcome.log_tail
    assert "plain line" in outcome.log_tail
    assert leftovers(api) == []


def test_a_job_that_never_finishes_times_out_and_is_removed(cluster) -> None:
    api, runner, clock = cluster
    api.job_result("slow-", waiting="ContainerCreating")
    outcome = runner.run_job(job("slow-1", timeout=10))
    assert outcome.state == "timeout"
    assert clock.now >= 40  # the Job's deadline plus the deployer's slack
    assert leftovers(api) == []


def test_a_pod_stuck_on_a_mount_fails_as_not_startable_after_the_grace(cluster) -> None:
    api, runner, clock = cluster
    api.job_result(
        "mnt-",
        waiting="ContainerCreating",
        events=(("FailedMount", 'MountVolume.SetUp failed: secret "x" not found'),),
    )
    outcome = runner.run_job(job("mnt-1", timeout=300))
    assert (outcome.state, outcome.category, outcome.reason) == (
        "not-startable",
        "mount",
        "FailedMount",
    )
    assert 5 <= clock.now < 30  # after the grace, not after the timeout
    assert leftovers(api) == []


def test_a_fatal_waiting_reason_fails_at_once(cluster) -> None:
    api, runner, clock = cluster
    api.job_result("img-", waiting="ImagePullBackOff")
    outcome = runner.run_job(job("img-1"))
    assert (outcome.state, outcome.category) == ("not-startable", "image")
    assert clock.now == 0


def test_an_interrupt_still_removes_the_job(cluster) -> None:
    api, runner, _ = cluster
    api.job_result("int-", waiting="ContainerCreating")

    def interrupt(_: float) -> None:
        raise KeyboardInterrupt

    runner.sleep = interrupt
    with pytest.raises(KeyboardInterrupt):
        runner.run_job(job("int-1"))
    assert leftovers(api) == []


def test_a_refused_create_is_a_registered_failure(cluster) -> None:
    api, runner, _ = cluster
    api.inject("POST", "/jobs", status=403)
    with pytest.raises(PipelineError) as raised:
        runner.run_job(job("deny-1"))
    assert raised.value.code == "prerollout-unavailable" and raised.value.failed


def test_stale_check_jobs_of_the_app_are_removed(cluster) -> None:
    api, runner, _ = cluster
    stale = job("old-1")
    stale["metadata"]["uid"] = "u"
    api.put(stale)
    other = job("other-app")
    other["metadata"]["labels"]["piceli.io/pre-rollout-app"] = "other"
    api.put(other)
    assert runner.remove_stale("shop") == 1
    assert [key for key in api.objects if key[0] == "Job"] == [("Job", "other-app")]


def test_reads_report_absent_keys_and_unreadable_objects(cluster) -> None:
    api, runner, _ = cluster
    assert runner.read_object("Secret", "absent") is None
    api.put(manifest("ConfigMap", "cfg"))
    assert runner.read_object("ConfigMap", "cfg") == {"mode"}
    api.inject("GET", "/secrets/locked", status=403)
    with pytest.raises(Unreadable):
        runner.read_object("Secret", "locked")
    claim = manifest("PersistentVolumeClaim", "data-db-0")
    claim["spec"] = {"accessModes": ["ReadWriteOnce"]}
    api.put(claim)
    assert runner.read_claim("data-db-0") == {
        "phase": "Bound",
        "modes": ["ReadWriteOnce"],
    }
    assert runner.read_claim("nope") is None
