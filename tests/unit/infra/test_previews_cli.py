"""``piceli env up|down BRANCH --cluster/--state-dir``: a preview through the controller.

The commands record a ``preview`` request; ``--wait`` follows it in the
published status until the preview runs its branch's head with its checks
passed (the URL is printed then, once), failed, or timed out.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.gitops.state import DirectoryChannel, preview_request
from piceli.k8s.cli import app
from piceli.k8s.cli.env_preview import wait_preview
from tests.unit.infra.test_previews import World


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    remotes = tmp_path / "remotes"
    monkeypatch.setenv("COMPOSITION_SHOP_URL", str(remotes / "shop.git"))
    monkeypatch.setenv("COMPOSITION_CATALOG_URL", str(remotes / "catalog.git"))
    return World(tmp_path)


def _json(output: str) -> dict[str, Any]:
    line = [item for item in output.splitlines() if item.startswith("{")][-1]
    return dict(json.loads(line))


def test_env_up_records_a_preview_request(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app, ["env", "up", "wp/login", "--state-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    body = _json(result.stdout)
    assert body["state"] == "requested" and body["branch"] == "wp/login"
    pending = DirectoryChannel(tmp_path).requests()
    assert pending[body["request"]]["kind"] == "preview"
    assert pending[body["request"]]["action"] == "up"


def test_env_down_records_a_preview_down_request(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app, ["env", "down", "wp-login", "--state-dir", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    body = _json(result.stdout)
    assert DirectoryChannel(tmp_path).requests()[body["request"]]["action"] == "down"


def test_pipeline_options_are_refused_with_the_controller(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "env",
            "up",
            "wp-login",
            "--state-dir",
            str(tmp_path),
            "--digest",
            "web=x@sha256:1",
        ],
    )
    assert result.exit_code == 2
    assert "gitops-request-invalid" in result.output
    assert DirectoryChannel(tmp_path).requests() == {}


def test_wait_prints_the_url_once_ready(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.poll()
    world.shop.commit({"web/index.html": "<h1>login</h1>\n"}, branch="wp-login")
    world.offer("wp-login")
    key = world.ask("wp-login")
    reads: list[int] = []

    def sleep(_seconds: float) -> None:
        reads.append(1)
        assert "https://wp-login" not in capsys.readouterr().err  # not before ready
        world.poll()

    code, body = wait_preview(
        world.channel, "wp-login", key, action="up", timeout=60, interval=2, sleep=sleep
    )
    assert code == 0 and reads == [1]
    assert body["state"] == "ready"
    assert body["url"] == "https://wp-login.preview.example.org"
    world.poll()  # nothing more to print
    err = capsys.readouterr().err
    assert err.count("https://wp-login.preview.example.org") == 1
    assert world.ports.builder.built == [["api", "catalog", "web"]]


def test_wait_reports_a_rejected_request(world: World) -> None:
    world.poll()
    key = world.ask("no-such-branch")
    code, body = wait_preview(
        world.channel,
        "no-such-branch",
        key,
        action="up",
        timeout=60,
        interval=2,
        sleep=lambda _s: world.poll(),
    )
    assert code == 1
    assert body["state"] == "failed" and body["reason"] == "gitops-request-invalid"


def test_wait_reports_a_failed_preview(tmp_path: Path) -> None:
    channel = DirectoryChannel(tmp_path)
    key, _ = preview_request("wp-login")
    channel.publish(
        {
            "controller": {
                "last_poll": "2026-10-24T10:00:00Z",
                "previews": {"served": [key]},
            },
            "envs": {
                "wp-login": {
                    "state": "failed",
                    "reason": "pipeline-checks-failed",
                    "failed_stage": "checks",
                    "preview": {"request": key, "url": "https://x.example.org"},
                }
            },
        }
    )
    code, body = wait_preview(
        channel,
        "wp-login",
        key,
        action="up",
        timeout=5,
        interval=1,
        sleep=lambda _s: None,
    )
    assert code == 1 and body["reason"] == "pipeline-checks-failed"
    assert "url" not in body


def test_wait_times_out(tmp_path: Path) -> None:
    key, _ = preview_request("wp-login")
    code, body = wait_preview(
        DirectoryChannel(tmp_path),
        "wp-login",
        key,
        action="up",
        timeout=0,
        interval=1,
        sleep=lambda _s: None,
    )
    assert code == 2 and body["reason"] == "gitops-wait-timeout"


def test_wait_for_down_ends_when_the_environment_is_gone(world: World) -> None:
    world.poll()
    world.shop.commit({"web/index.html": "<h1>login</h1>\n"}, branch="wp-login")
    world.poll()
    key = world.ask("wp-login", action="down")
    code, body = wait_preview(
        world.channel,
        "wp-login",
        key,
        action="down",
        timeout=60,
        interval=2,
        sleep=lambda _s: world.poll(),
    )
    assert code == 0 and body["state"] == "removed"
