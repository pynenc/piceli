"""Acceptance: an apply whose new pods cannot start fails at once, with causes.

The fake API creates, for a workload registered with ``FakeAPI.fail_pods``,
what the controllers would (the current ReplicaSet and a failing pod with its
log and events). ``readiness_seconds`` is long, so only the fail-fast path can
finish these applies within the asserted time.
"""

from __future__ import annotations

import json
import time
from typing import Any

from typer.testing import CliRunner

from piceli.k8s.cli import app as cli
from tests.acceptance import test_release_cli

#: The release spec fixture of the release CLI tests (fake API, one Deployment).
release_env = test_release_cli.release_env

LONG_WAIT = "readiness_seconds = 1\n"


def _slow(tmp_path: Any, extra: str = "") -> None:
    spec = tmp_path / "release.toml"
    spec.write_text(
        spec.read_text().replace(LONG_WAIT, "readiness_seconds = 25\n" + extra, 1)
    )


def _run(tmp_path: Any, *args: str) -> tuple[int, dict[str, Any], Any]:
    result = CliRunner().invoke(
        cli, ["release", *args, "--spec", str(tmp_path / "release.toml")]
    )
    payload = json.loads(result.stdout) if result.stdout.strip() else {}
    return result.exit_code, payload, result


def test_crash_loop_fails_in_seconds_with_redacted_causes(release_env) -> None:
    api, tmp_path = release_env
    _slow(tmp_path)
    api.ready = False
    api.fail_pods(
        "worker",
        reason="CrashLoopBackOff",
        exit_code=1,
        restarts=1,
        logs="starting\nconnecting with password=hunter2-not-for-logs\n"
        "fatal: settings file missing\n",
        events=(("BackOff", "Back-off restarting failed container worker"),),
    )

    def leak_the_stored_secret(request: dict[str, Any], phase: str) -> bool:
        # The container prints the generated Secret value itself: it must be
        # replaced because the release's secret store knows it.
        secret = api.objects.get(("Secret", "credential"))
        failure = api.pod_failures["worker"]
        if secret and "leaked" not in failure:
            value = secret["data"]["password"]
            failure["logs"] = f"token is {value}\n" + failure["logs"]
            failure["leaked"] = value
        return False

    api.intercept = leak_the_stored_secret
    started = time.monotonic()
    code, applied, result = _run(tmp_path, "apply", "--auto-approve")
    elapsed = time.monotonic() - started
    assert code == 1, result.stdout + result.stderr
    assert elapsed < 10, elapsed
    assert applied["reason"] == "apply-crashloop"
    execution = applied["execution"]
    assert execution["failure_category"] == "apply-crashloop"
    assert execution["causes"] == [
        {
            "kind": "Deployment",
            "name": "worker",
            "container": "worker",
            "reason": "CrashLoopBackOff",
            "exit_code": 1,
            "restarts": 1,
        }
    ]
    diagnosis = applied["diagnosis"]
    assert diagnosis["schema"] == "piceli.diagnosis.v1"
    assert diagnosis["code"] == "apply-crashloop"
    [workload] = diagnosis["workloads"]
    assert (workload["kind"], workload["name"], workload["fatal"]) == (
        "Deployment",
        "worker",
        True,
    )
    [cause] = workload["causes"]
    assert cause["logs"][-1] == "fatal: settings file missing"
    assert cause["events"][0]["reason"] == "BackOff"
    leaked = api.pod_failures["worker"]["leaked"]
    everything = result.stdout + result.stderr
    assert "hunter2-not-for-logs" not in everything
    assert leaked not in everything
    assert "token is [REDACTED]" in cause["logs"]
    assert "failed: 1 workload not starting (apply-crashloop)" in result.stderr
    assert 'worker  worker  exit 1  "fatal: settings file missing"' in result.stderr

    # The same causes later, from the journal only (no cluster needed).
    api.intercept = None
    execution_id = execution["execution_id"]
    code, found, result = _run(tmp_path, "status", "--run", execution_id[:10])
    assert code == 0, result.stdout + result.stderr
    assert found["execution_id"] == execution_id
    assert found["diagnosis"] == diagnosis
    assert 'exit 1  "fatal: settings file missing"' in result.stderr
    assert leaked not in result.stdout + result.stderr
    explained = CliRunner().invoke(
        cli,
        [
            "explain",
            "--run",
            execution_id,
            "--spec",
            str(tmp_path / "release.toml"),
        ],
    )
    assert explained.exit_code == 0
    assert json.loads(explained.stdout)["diagnosis"] == diagnosis
    # The public history only has the compact causes, never log lines.
    history = (tmp_path / "state" / "history.json").read_text()
    assert "settings file missing" not in history

    code, refused, _ = _run(tmp_path, "status", "--run", "0000000000")
    assert code == 2 and refused["reason"] == "unknown-execution"
    refused_explain = CliRunner().invoke(cli, ["explain", "--run", execution_id])
    assert refused_explain.exit_code == 2
    assert json.loads(refused_explain.stdout)["reason"] == "explain-run-needs-spec"


def test_image_pull_failure_names_the_image_without_logs(release_env) -> None:
    api, tmp_path = release_env
    _slow(tmp_path)
    api.ready = False
    api.fail_pods(
        "worker",
        reason="ImagePullBackOff",
        exit_code=None,
        restarts=0,
        message='Back-off pulling image "registry.example/app/missing:1"',
    )
    started = time.monotonic()
    code, applied, result = _run(tmp_path, "apply", "--auto-approve")
    assert code == 1 and time.monotonic() - started < 10
    [cause] = applied["diagnosis"]["workloads"][0]["causes"]
    assert (cause["reason"], cause["exit_code"], cause["logs"]) == (
        "ImagePullBackOff",
        None,
        [],
    )
    assert (
        'worker  worker  ImagePullBackOff  "Back-off pulling image '
        "'registry.example/app/missing:1'\"" in result.stderr
    )
    # An image that never pulled has no log to read.
    assert not any(request["path"].endswith("/log") for request in api.requests)


def test_restarts_over_the_threshold_fail_a_running_container(release_env) -> None:
    api, tmp_path = release_env
    _slow(tmp_path, "crash_restarts = 2\n")
    api.ready = False
    api.fail_pods("worker", reason=None, exit_code=137, restarts=2, logs="oom\n")
    code, applied, _ = _run(tmp_path, "apply", "--auto-approve")
    assert code == 1 and applied["reason"] == "apply-crashloop"
    [cause] = applied["diagnosis"]["workloads"][0]["causes"]
    assert (cause["reason"], cause["exit_code"], cause["restarts"]) == ("Error", 137, 2)


def test_pods_of_an_older_revision_never_fail_the_apply(release_env) -> None:
    api, tmp_path = release_env
    api.ready = False
    # The current revision's pod is only starting; an old one crash-loops.
    api.fail_pods("worker", reason="ContainerCreating", exit_code=None, restarts=0)
    api.put(
        {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {
                "name": "worker-old-x1",
                "labels": {"app": "worker"},
                "ownerReferences": [{"kind": "ReplicaSet", "uid": "old-rs"}],
            },
            "status": {
                "containerStatuses": [
                    {
                        "name": "worker",
                        "restartCount": 9,
                        "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                    }
                ]
            },
        }
    )
    code, applied, _ = _run(tmp_path, "apply", "--auto-approve")
    assert code == 1
    assert applied["execution"]["failure_category"] != "apply-crashloop"
    assert "diagnosis" not in applied  # nothing wrong with the new pods


def test_fail_fast_off_waits_and_still_reports_the_causes(release_env) -> None:
    api, tmp_path = release_env
    spec = tmp_path / "release.toml"
    spec.write_text(
        spec.read_text().replace(LONG_WAIT, LONG_WAIT + "fail_fast = false\n", 1)
    )
    api.ready = False
    api.fail_pods("worker", logs="boom\n")
    code, applied, result = _run(tmp_path, "apply", "--auto-approve")
    assert code == 1
    category = applied["execution"]["failure_category"]
    assert category != "apply-crashloop"
    assert applied["diagnosis"]["code"] == category
    assert applied["diagnosis"]["workloads"][0]["causes"][0]["logs"] == ["boom"]
    assert "1 workload not ready" in result.stderr


def test_deploy_stops_with_one_line_per_failing_workload(shop) -> None:
    from tests.acceptance.test_deploy_pipeline import deploy

    api, tmp_path = shop
    api.ready = False
    api.fail_pods("web", logs="listening\nfatal: cannot open catalog\n")
    api.fail_pods("cache", exit_code=101, logs="config file must be owner-only\n")
    code, events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 1, result.stdout + result.stderr
    final = events[-1]
    assert final["reason"] == "pipeline-apply-crashloop"
    assert final["stage"] == "apply"
    names = sorted(item["name"] for item in final["diagnosis"]["workloads"])
    assert names == ["cache", "web"]
    assert "2 workloads not starting:" in result.stderr
    assert 'cache  cache  exit 101  "config file must be owner-only"' in result.stderr
    assert 'web    web    exit 1    "fatal: cannot open catalog"' in result.stderr
    run_id = final["run_id"]
    assert f"--run {run_id}" in result.stderr
    # The journaled run keeps the compact causes, not the log lines.
    run = (tmp_path / "state" / "runs" / f"{run_id}.json").read_text()
    assert "cannot open catalog" not in run
    assert '"reason": "CrashLoopBackOff"' in run
    # The run summary carries the same compact causes, still without logs.
    summary_dir = tmp_path / "state" / "runs" / run_id
    summary = json.loads((summary_dir / "summary.json").read_text())
    assert summary["failure"]["reason"] == "pipeline-apply-crashloop"
    assert summary["failure"]["category"] == "apply-crashloop"
    causes = summary["failure"]["causes"]
    assert sorted(item["name"] for item in causes) == ["cache", "web"]
    assert {item["reason"] for item in causes} == {"CrashLoopBackOff"}
    markdown = (summary_dir / "summary.md").read_text()
    assert "cannot open catalog" not in markdown + json.dumps(summary)
    assert "container `cache`: `CrashLoopBackOff`, exit 101" in markdown

    status = CliRunner().invoke(
        cli,
        [
            "release",
            "status",
            "--spec",
            str(tmp_path / "app.py:pipeline"),
            "--run",
            run_id,
        ],
    )
    assert status.exit_code == 0, status.stdout + status.stderr
    shown = json.loads(status.stdout)
    assert sorted(item["name"] for item in shown["diagnosis"]["workloads"]) == [
        "cache",
        "web",
    ]
