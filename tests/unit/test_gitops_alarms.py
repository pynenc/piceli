"""Alarms of a composition's GitOps controller (0.28.0).

The event logic is pure (a status document in, the alarms to send out);
the senders are the train's notifiers. No network: ntfy and webhooks are
sent to a loopback receiver, desktop notifications to a recorded argv.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.gitops.alarms import Alarms, AlarmSender, changes, observe
from piceli.gitops.state import DirectoryChannel
from piceli.infra import Controller, Notify
from piceli.k8s.cli import app as cli

NOW = 2_000_000_000.0  # 2033-05-18T03:33:20Z
C1, C2 = "1" * 40, "2" * 40
WAIT = 600.0


def _iso(seconds: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _status(version: str = "0.28.0", **envs: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "piceli.gitops-status.v1",
        "controller": {"version": version, "composition": "shop"},
        "envs": {name: {"branch": name, **record} for name, record in envs.items()},
    }


def _failed(commit: str = C1, reason: str = "component-build-failed") -> dict[str, Any]:
    return {
        "state": "failed",
        "reason": reason,
        "commit": commit,
        "failed_stage": "build",
        "attempts": 5,
        "failure": {"log_tail": "step 3/7\nerror: cargo build failed\n"},
    }


DEPLOYED = {"state": "deployed", "commit": C2, "health": "healthy"}


# ------------------------------------------------------------- events


def test_a_failed_environment_alarms_once_then_recovers_once() -> None:
    memory: dict[str, Any] = {}
    (alarm,) = changes(memory, _status(staging=_failed()), NOW, WAIT)
    assert (alarm.event, alarm.subject, alarm.state) == (
        "env-failed",
        "staging",
        "firing",
    )
    assert alarm.text == (
        "staging failed in stage build (component-build-failed) at 111111111111: "
        "error: cargo build failed. Next: piceli gitops status"
    )
    assert alarm.title == "piceli: staging failed"
    # The same failure at the next polls, and after a restart (memory kept).
    assert changes(memory, _status(staging=_failed()), NOW + 60, WAIT) == []
    kept = json.loads(json.dumps(memory))
    assert changes(kept, _status(staging=_failed()), NOW + 120, WAIT) == []
    # Another commit failing is a new event.
    (again,) = changes(kept, _status(staging=_failed(C2)), NOW + 180, WAIT)
    assert again.commit == C2
    (back,) = changes(kept, _status(staging=DEPLOYED), NOW + 240, WAIT)
    assert (back.state, back.ok, back.title) == (
        "resolved",
        True,
        "piceli: staging recovered",
    )
    assert back.text == (
        "staging recovered (was component-build-failed). Next: piceli gitops status"
    )
    assert changes(kept, _status(staging=DEPLOYED), NOW + 300, WAIT) == []


def test_retries_alarm_from_the_second_failed_attempt_and_degraded_checks() -> None:
    retrying = {**_failed(), "state": "retrying", "attempts": 1}
    assert observe(_status(main=retrying), NOW, WAIT) == {}
    (alarm,) = observe(_status(main={**retrying, "attempts": 2}), NOW, WAIT).values()
    assert alarm.detail is not None and alarm.detail.startswith("attempt 2, retrying")
    degraded = {
        **DEPLOYED,
        "health": "degraded",
        "reason": "pipeline-checks-failed",
        "verification": {
            "failed": [{"check": "home", "code": "check-failed", "detail": "503"}]
        },
    }
    (alarm,) = observe(_status(main=degraded), NOW, WAIT).values()
    assert alarm.reason == "pipeline-checks-failed" and alarm.stage == "checks"
    assert "failing checks: home (503)" in alarm.text


def test_waiting_for_a_build_host_or_an_approval_alarms_after_waiting_after() -> None:
    waiting = {
        "state": "retrying",
        "reason": "build-waiting",
        "commit": C1,
        "waiting_since": _iso(NOW - 300),
    }
    assert observe(_status(staging=waiting), NOW, WAIT) == {}
    (alarm,) = observe(_status(staging=waiting), NOW + 301, WAIT).values()
    assert alarm.text == (
        f"staging waits for a build host since {_iso(NOW - 300)} (build-waiting) "
        "at 111111111111. Next: piceli gitops builds"
    )
    plan = "sha256:" + "d" * 64
    approval = {
        "state": "approval-required",
        "plan_hash": plan,
        "approval_required_since": _iso(NOW - 700),
    }
    (alarm,) = observe(_status(production=approval), NOW, WAIT).values()
    assert alarm.reason == "approval-required"
    assert alarm.next == f"piceli gitops approve production {plan}"
    # The wait ends: one "no longer waiting" message.
    memory: dict[str, Any] = {}
    changes(memory, _status(staging=waiting), NOW + 301, WAIT)
    (done,) = changes(memory, _status(staging=DEPLOYED), NOW + 400, WAIT)
    assert done.title == "piceli: staging no longer waiting"


def test_nix_releases_composition_and_restarts_alarm() -> None:
    status = _status()
    status["nix_releases"] = {
        "web-1": {
            "state": "failed",
            "reason": "gitops-nix-release-failed",
            "tag": "web-v1.2.0",
            "commit": C1,
            "finished_at": _iso(NOW),
            "failure": {
                "host": "builder-1",
                "reason": "build-failed",
                "log_tail": "error: builder for web-1 failed\n",
            },
        },
        "db-1": {
            "state": "waiting-build",
            "tag": "db-v1",
            "requested_at": _iso(NOW - 900),
        },
    }
    status["controller"]["composition_repo"] = {
        "name": "infra",
        "failed": C2,
        "error": "composition-invalid",
        "error_detail": "importing infra.py failed: NameError: name 'x' is not defined",
    }
    memory: dict[str, Any] = {"version": "0.27.0"}
    found = {alarm.key: alarm for alarm in changes(memory, status, NOW, WAIT)}
    assert set(found) == {
        "nix-release-failed:web-1",
        "nix-release-waiting:db-1",
        "composition-failed:infra",
        "controller-restarted:controller",
    }
    assert found["nix-release-failed:web-1"].text == (
        "nix release web-1 failed (gitops-nix-release-failed) at 111111111111: "
        "build host builder-1; build-failed; error: builder for web-1 failed. "
        "Next: piceli gitops builds --state failed"
    )
    assert found["composition-failed:infra"].text == (
        "the composition does not import (composition-invalid) at 222222222222: "
        "importing infra.py failed: NameError: name 'x' is not defined; nothing "
        "deploys. Next: piceli gitops status"
    )
    restarted = found["controller-restarted:controller"]
    assert restarted.ok and restarted.text == (
        "the controller runs piceli 0.28.0 (was 0.27.0) (controller-upgraded). "
        "Next: piceli gitops status"
    )
    assert changes(memory, status, NOW + 60, WAIT) == []  # once each


def test_a_torn_down_environment_resolves_silently() -> None:
    memory: dict[str, Any] = {}
    changes(memory, _status(**{"wp-1": _failed()}), NOW, WAIT)
    assert changes(memory, _status(), NOW + 60, WAIT) == []
    assert memory["open"] == {}


# ------------------------------------------------------------- declaration


def test_the_declaration_is_checked_and_round_trips() -> None:
    alarms = Alarms(
        targets=[
            Notify.ntfy("https://ntfy.example.org/deploys", when="failure"),
            Notify.webhook(url_env="ALARM_WEBHOOK_URL",
                           headers_env={"Authorization": "ALARM_TOKEN"}),
            Notify.desktop(),
        ],
        secret="piceli-alarms",
        waiting_after="15m",
    )  # fmt: skip
    assert alarms.variables == ("ALARM_WEBHOOK_URL", "ALARM_TOKEN")
    assert alarms.waiting_seconds == 900
    assert Alarms.from_dict(json.loads(json.dumps(alarms.to_dict()))) == alarms
    Controller(on="node-1", alarms=alarms)
    for bad, words in (
        (lambda: Alarms(targets=[Notify.command(["true"])]), "runs no command"),
        (lambda: Alarms(targets=[Notify.webhook(url_env="HOOK")]), "Alarms(secret=)"),
        (lambda: Alarms(targets=[Notify.ntfy(url_env="HOME")], secret="s"), "HOME"),
        (lambda: Alarms(waiting_after="10s"), "at least 1m"),
        (lambda: Alarms(targets=Notify.desktop()), "list of Notify"),
        (lambda: Controller(on="node-1", alarms={"targets": []}), "Alarms(...)"),
    ):
        with pytest.raises(
            ValueError, match=words.replace("(", r"\(").replace(")", r"\)")
        ) as error:
            bad()
        assert getattr(error.value, "code", None) == "cluster-invalid"
    with pytest.raises(ValueError, match="url or url_env"):
        Notify.ntfy()


def test_the_controller_reads_secrets_from_its_secret_never_its_config() -> None:
    from piceli.gitops.install import InstallSettings, render_controller
    from piceli.infra.composition import load_composition
    from piceli.infra.controller import CompositionConfig

    example = Path(__file__).resolve().parents[2] / "examples" / "composition"
    composition = load_composition(example / "infra.py")
    alarms = Alarms(
        targets=[Notify.ntfy(url_env="ALARM_NTFY_URL", token_env="ALARM_NTFY_TOKEN")],
        secret="piceli-alarms",
    )
    config = CompositionConfig(
        composition=composition.to_dict(), alarms=alarms.to_dict()
    )
    assert CompositionConfig.from_dict(config.to_dict()) == config
    plain = CompositionConfig(composition=composition.to_dict())
    assert "alarms" not in plain.to_dict()  # same config hash without alarms
    objects = render_controller(
        config, InstallSettings(image="ghcr.io/example/piceli@sha256:" + "0" * 64)
    )
    (deployment,) = [item for item in objects if item["kind"] == "Deployment"]
    (container,) = deployment["spec"]["template"]["spec"]["containers"]
    found = {item["name"]: item for item in container["env"]}
    assert found["ALARM_NTFY_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": "piceli-alarms", "key": "ALARM_NTFY_TOKEN", "optional": True}  # fmt: skip
    assert "ALARM" not in " ".join(container["args"])


# ------------------------------------------------------------- sending


@contextmanager
def receiver(status: int = 200) -> Iterator[tuple[str, list[dict[str, Any]]]]:
    got: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            got.append({"path": self.path, "headers": dict(self.headers), "body": body})
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", got
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_ntfy_and_webhook_get_the_alarm_and_failures_are_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs: list[str] = []
    with receiver() as (url, got), receiver(status=500) as (broken, _):
        monkeypatch.setenv("ALARM_NTFY_URL", url + "/s3cr3t-topic")
        monkeypatch.setenv("ALARM_NTFY_TOKEN", "tk_s3cr3t")
        alarms = Alarms(
            targets=[
                Notify.ntfy(url_env="ALARM_NTFY_URL", token_env="ALARM_NTFY_TOKEN"),
                Notify.webhook(url + "/hook"),
                Notify.webhook(broken + "/hook"),
                Notify.desktop(),  # not the controller's: alarms --follow
            ],
            secret="piceli-alarms",
        )
        sender = AlarmSender(alarms, log=logs.append)
        memory: dict[str, Any] = {}
        sent = sender.run(memory, _status(staging=_failed()), NOW, _iso(NOW))
    assert [alarm.key for alarm in sent] == ["env-failed:staging"]
    ntfy, hook = got
    assert ntfy["path"] == "/s3cr3t-topic"
    assert ntfy["headers"]["Title"] == "piceli: staging failed"
    assert ntfy["headers"]["Authorization"] == "Bearer tk_s3cr3t"
    assert ntfy["body"].decode().startswith("staging failed in stage build")
    payload = json.loads(hook["body"])
    assert payload["schema"] == "piceli.alarm.v1" and payload["reason"] == (
        "component-build-failed")  # fmt: skip
    assert payload["next"] == "piceli gitops status" and payload["ok"] is False
    assert memory["sent"] == 2
    (failure,) = memory["failures"]
    assert failure["reason"] == "gitops-alarm-failed" and failure["alarm"] == (
        "env-failed:staging")  # fmt: skip
    text = json.dumps(memory) + "\n".join(logs)
    assert "s3cr3t" not in text and "[gitops-alarm-failed]" in "\n".join(logs)


def test_a_target_that_hangs_or_is_unset_never_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALARM_HOOK", raising=False)
    alarms = Alarms(targets=[Notify.webhook(url_env="ALARM_HOOK")], secret="s")
    memory: dict[str, Any] = {}
    AlarmSender(alarms).run(memory, _status(staging=_failed()), NOW, _iso(NOW))
    assert memory["failures"][0]["target"] == "webhook $ALARM_HOOK"
    assert "sent" not in memory


# ------------------------------------------------------------- the local follower


def test_gitops_alarms_lists_and_follows_with_desktop_notifications(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    channel = DirectoryChannel(tmp_path)
    status = _status(staging=_failed())
    status["controller"]["alarms"] = {
        "waiting_after_seconds": 600,
        "failures": [{"alarm": "env-failed:staging", "target": "ntfy ntfy.example.org",
                      "at": _iso(NOW), "reason": "gitops-alarm-failed"}],
    }  # fmt: skip
    channel.publish(status)
    once = CliRunner().invoke(
        cli, ["gitops", "alarms", "--state-dir", str(tmp_path), "--json"]
    )
    assert once.exit_code == 0, once.output
    assert "ALARM staging failed: staging failed in stage build" in once.stderr
    assert "could not send env-failed:staging to ntfy ntfy.example.org" in once.stderr
    body = json.loads(once.stdout)
    assert [a["event"] for a in body["alarms"]] == ["env-failed"]

    shown: list[list[str]] = []
    monkeypatch.setattr(
        "piceli.train.notify.desktop_argv", lambda title, text: ["notify", title, text]
    )

    class Done:
        returncode = 0

    def run(argv: list[str], **_kw: Any) -> Done:
        shown.append(argv)
        return Done()

    monkeypatch.setattr("piceli.train.notify.subprocess.run", run)
    reads = iter([status, status, _status(staging=DEPLOYED)])
    monkeypatch.setattr(DirectoryChannel, "read_status", lambda self: next(reads))
    monkeypatch.setattr("piceli.k8s.cli.gitops_watch.time.sleep", lambda _s: None)
    follow = CliRunner().invoke(
        cli,
        ["gitops", "alarms", "--state-dir", str(tmp_path), "--follow", "--polls", "3"],
    )
    assert follow.exit_code == 0, follow.output
    assert [argv[1] for argv in shown] == [
        "piceli: staging failed", "piceli: staging recovered"]  # fmt: skip
    assert follow.stderr.count("could not send") == 1
