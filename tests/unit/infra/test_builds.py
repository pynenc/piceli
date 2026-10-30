"""Builds you can see and start (0.26.0): NixRelease triggers (a branch, a
schedule, by hand), cancel, progress and the uniform build records."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.dev import nix
from piceli.gitops.state import (
    approve_request,
    build_cancel_request,
    build_request,
)
from piceli.infra import builds, remote_builds
from tests.unit.infra.test_nix_releases import INFRA, PUBLIC, _release, _worker, _world


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    return _world(tmp_path, monkeypatch)


def _at(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp()


# ------------------------------------------------------------------ schedules


def test_a_schedule_is_named_or_cron_and_due_once_its_minute_passed() -> None:
    assert builds.parse_schedule("daily")[:2] == (frozenset({0}), frozenset({0}))
    assert builds.parse_schedule("*/15 1-3 * * 1,5")[0] == frozenset({0, 15, 30, 45})
    for bad in ("", "every day", "61 * * * *", "* * * *", "*/0 * * * *", 3):
        with pytest.raises(ValueError):
            builds.parse_schedule(bad)
    noon = _at("2026-10-24T12:00:30")
    assert not builds.schedule_due("daily", None, noon)  # first sight
    assert not builds.schedule_due("daily", noon, noon + 3600)
    assert builds.schedule_due("daily", noon, _at("2026-10-25T00:00:10"))
    assert builds.schedule_due("hourly", noon, noon + 3600)
    # 2026-10-26 is a Monday: weekly fires then, not on the Sunday before.
    assert not builds.schedule_due("weekly", noon, _at("2026-10-25T23:59:00"))
    assert builds.schedule_due("weekly", noon, _at("2026-10-26T00:00:00"))
    assert builds.schedule_due("30 6 * * 0", noon, _at("2026-10-25T06:30:00"))


def test_a_release_with_a_bad_schedule_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from piceli.infra import CompositionError

    with pytest.raises(CompositionError, match="cron"):
        _world(tmp_path, monkeypatch, infra=INFRA.replace("auto_approve=AUTO",
               "auto_approve=AUTO, schedule='sometimes'"))  # fmt: skip


# ------------------------------------------------------------------ triggers

BRANCH = INFRA.replace('follow=Tag("web-v*")', 'follow=Branch("main")').replace(
    "NixRelease, Source, Tag",
    "NixRelease, Source, Tag\nfrom piceli.hosts import Branch",
)


def test_a_branch_follow_publishes_each_new_head_with_its_branch_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(tmp_path, monkeypatch, auto=True, infra=BRANCH)
    controller, repo = world["controller"], world["repo"]
    first = _release(controller.poll_once())
    assert first["state"] == "baseline" and first["tag"] is None
    assert first["follow"] == {"kind": "branch", "name": "main"}

    commit = repo.commit({"hosts/web-1.nix": "{ }\n"})
    record = _release(controller.poll_once())
    assert record["state"] == "waiting-build" and record["commit"] == commit
    assert record["trigger_kind"] == "branch" and record["tag"] is None
    plan = record["plan"]
    assert plan["branch"] == "main" and plan["tag"] is None
    assert plan["pointers"] == ["piceli/web-1/refs/heads/main.json"]
    worker, _ = _worker(world, tmp_path)
    assert worker.once() is not None
    done = _release(controller.poll_once())
    assert done["state"] == "published", done
    pointer = json.loads(
        (world["cache"] / "piceli/web-1/refs/heads/main.json").read_text()
    )
    assert nix.verify_pointer(pointer, PUBLIC) and pointer["commit"] == commit
    manifest = json.loads((world["cache"] / f"piceli/web-1/{commit}.json").read_text())
    assert manifest["tag"] is None
    assert _release(controller.poll_once())["state"] == "published"  # same head


def test_a_schedule_plans_the_newest_tag_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    infra = INFRA.replace("auto_approve=AUTO", "auto_approve=AUTO, schedule='hourly'")
    world = _world(tmp_path, monkeypatch, infra=infra)
    controller, clock = world["controller"], world["clock"]
    assert _release(controller.poll_once())["state"] == "baseline"
    clock["now"] += 120
    assert _release(controller.poll_once())["state"] == "baseline"
    clock["now"] += 3600
    record = _release(controller.poll_once())
    assert record["state"] == "approval-required" and record["tag"] == "web-v1.0.0"
    assert record["trigger_kind"] == "schedule" and record["schedule"] == "hourly"


def test_a_build_started_by_hand_plans_its_ref_through_the_approval(
    world: dict[str, Any], tmp_path: Path
) -> None:
    controller, channel, repo = world["controller"], world["channel"], world["repo"]
    controller.poll_once()
    old = repo.commit({"a": "1"})
    repo.tag("web-v1.1.0")
    repo.commit({"a": "2"})
    repo.tag("web-v1.2.0")
    controller.poll_once()  # the newest tag waits for its approval
    channel.add_request(*build_request("web-1", "web-v1.1.0", by="ana"))
    record = _release(controller.poll_once())
    assert (record["state"], record["tag"], record["commit"]) == (
        "approval-required", "web-v1.1.0", old,
    )  # fmt: skip
    assert record["trigger_kind"] == "cli" and record["requested_by"] == "ana"
    digest = record["plan_digest"]
    assert _release(controller.poll_once())["tag"] == "web-v1.1.0"  # not replaced

    # --approve with the plan's digest: it goes on at once.
    channel.add_request(
        *build_request("web-1", old, via="ui", by="ana", approve=digest)
    )
    record = _release(controller.poll_once())
    assert record["state"] == "waiting-build" and record["tag"] == "web-v1.1.0"
    assert record["trigger_kind"] == "ui"

    channel.add_request(*build_request("web-1", "web-v9.9.9"))
    status = controller.poll_once()
    assert status["rejected_requests"][-1]["reason"] == "gitops-build-ref-unknown"
    channel.add_request(*build_request("nothing"))
    status = controller.poll_once()
    assert status["rejected_requests"][-1]["reason"] == "gitops-request-invalid"


def test_cancel_drops_a_queued_publish_and_stops_a_running_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(tmp_path, monkeypatch, auto=True)
    controller, channel, repo = world["controller"], world["channel"], world["repo"]
    controller.poll_once()
    repo.commit({"a": "1"})
    repo.tag("web-v1.1.0")
    assert _release(controller.poll_once())["state"] == "waiting-build"
    channel.add_request(*build_cancel_request("web-1", by="ana"))
    status = controller.poll_once()
    record = _release(status)
    assert (
        record["state"] == "cancelled" and record["reason"] == "gitops-build-cancelled"
    )
    assert record["cancelled_by"] == "ana"
    assert status["controller"]["builds_wanted"] == []
    channel.add_request(*build_cancel_request("web-1"))
    status = controller.poll_once()
    assert status["rejected_requests"][-1]["reason"] == "gitops-build-not-running"

    # Claimed: the host stops before its next stage and answers nothing.
    channel.add_request(*build_request("web-1"))
    assert _release(controller.poll_once())["state"] == "waiting-build"

    def cancelled_mid_build(checkout: Path, attribute: str, say: Any) -> str:
        say("building " + attribute)
        channel.add_request(*build_cancel_request("web-1"))
        controller.poll_once()
        return "/nix/store/" + "a" * 32 + "-x"

    worker, seen = _worker(world, tmp_path, build=cancelled_mid_build)
    assert worker.once() is not None
    assert not any("push" in item for item in seen)  # never signed nor uploaded
    status = controller.poll_once()
    assert _release(status)["state"] == "cancelled"
    assert not [r for r in status["rejected_requests"] if r["kind"] == "nix-published"]
    kinds = [body["kind"] for body in channel.requests().values()]
    assert "nix-published" not in kinds and "build-failed" not in kinds


# ------------------------------------------------------------------ records


def test_every_build_has_one_record_with_its_trigger_stages_and_targets(
    world: dict[str, Any], tmp_path: Path
) -> None:
    controller, channel, repo = world["controller"], world["channel"], world["repo"]
    controller.poll_once()
    commit = repo.commit({"a": "1"})
    repo.tag("web-v1.1.0")
    record = _release(controller.poll_once())
    document = builds.records(controller.status(), None, now=world["clock"]["now"])
    (waiting,) = document["builds"]
    assert waiting["stage"] == "approval-required" and waiting["actions"]["approve"]
    assert waiting["plan_digest"] == record["plan_digest"] and waiting["plan"]
    channel.add_request(*approve_request("web-1", record["plan_digest"], via="ui"))
    controller.poll_once()
    worker, _ = _worker(world, tmp_path)
    assert worker.once() is not None
    progress = [b for b in channel.requests().values() if b["kind"] == "build-progress"]
    assert {b["stage"] for b in progress} == {"fetch", "build", "publish"}
    status = controller.poll_once()
    history = json.loads((world["state"] / "history.json").read_text())
    document = builds.records(status, history, now=world["clock"]["now"])
    assert document["schema"] == "piceli.builds.v1"
    (done,) = document["builds"]
    assert done["kind"] == "nix-publish" and done["target"] == "web-1"
    assert done["trigger"] == {"kind": "tag", "by": None, "detail": "tag web-v1.1.0"}
    assert (done["commit"], done["tag"], done["host"]) == (commit, "web-v1.1.0", "mini")
    assert done["stage"] == "published" and done["seconds"] is not None
    assert [s["stage"] for s in done["stages"]] == [
        "approval-required", "queued", "fetching-inputs", "building", "uploading",
    ]  # fmt: skip
    # Published is not switched: the controller's view never says a host runs it.
    assert done["running_on"] == [] and done["host_switch"] == "not-switched-yet"
    assert done["actions"] == {"approve": False, "retry": True, "cancel": False}
    assert builds.records(status, history, now=0, target="api")["builds"] == []
    assert builds.records(status, history, now=0, state="active")["builds"] == []


def _host(commit: str, tag: str | None, checks: str = "passed") -> dict[str, Any]:
    return {"status": {
        "current": {"commit": commit, "tag": tag},
        "last_attempt": {"commit": commit, "state": "deployed",
                         "ended_at": "2026-10-24T10:05:00Z",
                         "checks": [{"name": "http", "state": checks}]},
    }}  # fmt: skip


def test_a_nix_host_runs_a_publish_only_once_it_reports_it_with_checks_passed(
    world: dict[str, Any], tmp_path: Path
) -> None:
    controller, channel, repo = world["controller"], world["channel"], world["repo"]
    controller.poll_once()
    commit = repo.commit({"a": "1"})
    repo.tag("web-v1.1.0")
    record = _release(controller.poll_once())
    channel.add_request(*approve_request("web-1", record["plan_digest"], via="ui"))
    controller.poll_once()
    worker, _ = _worker(world, tmp_path)
    assert worker.once() is not None
    status = controller.poll_once()

    def done(hosts: dict[str, Any]) -> dict[str, Any]:
        (item,) = builds.records(status, None, now=0, hosts=hosts)["builds"]
        assert item["stage"] == "published"
        return item

    previous = done({"web-1": _host("0" * 40, "web-v1.0.0")})
    assert previous["running_on"] == []
    assert previous["host_switch"] == "not-switched-yet"
    failing = done({"web-1": _host(commit, "web-v1.1.0", checks="failed")})
    assert failing["running_on"] == [] and failing["host_switch"] == "not-switched-yet"
    switched = done({"web-1": _host(commit, "web-v1.1.0")})
    assert switched["running_on"] == ["web-1"] and switched["host_switch"] == "switched"


def test_image_builds_and_train_handoffs_are_records_too() -> None:
    status = {
        "controller": {
            "builds_wanted": [
                {"id": "b" * 20, "kind": "components", "owner": "main",
                 "commits": {"shop": "c" * 40}, "images": ["web"],
                 "requested_at": "2026-10-24T10:00:00Z",
                 "claimed": {"host": "mini", "at": "2026-10-24T10:00:05Z"},
                 "progress": {"stage": "build", "at": "2026-10-24T10:00:09Z",
                              "log_tail": "Compiling web"}},
            ],
            "prebuilt": [{"component": "web", "key": "k1", "image": "r/web@sha256:1",
                          "host": "laptop", "train": "main",
                          "at": "2026-10-24T09:00:00Z"}],
        },
        "envs": {"main": {"state": "pending", "revision": {"shop": "c" * 40}}},
    }  # fmt: skip
    document = builds.records(status, {}, now=_at("2026-10-24T10:01:00"))
    image, train = document["builds"]
    assert image["kind"] == "images" and image["target"] == "main"
    assert image["stage"] == "building" and image["host"] == "mini"
    assert image["log_tail"] == "Compiling web" and image["actions"]["cancel"]
    assert image["seconds"] == 60.0
    assert train["kind"] == "train-handoff" and train["trigger"]["kind"] == "train"


def test_image_build_progress_and_cancel_on_the_wanted_build() -> None:
    state: dict[str, Any] = {remote_builds.STATE_KEY: {"b" * 20: {
        "id": "b" * 20, "owner": "main", "claimed": {"host": "mini"}}}}  # fmt: skip
    remote_builds.progress(state, {"id": "b" * 20, "host": "other", "stage": "build"})
    assert "progress" not in state[remote_builds.STATE_KEY]["b" * 20]
    remote_builds.progress(state, {"id": "b" * 20, "host": "mini", "stage": "build",
                                   "log_tail": "x", "at": "t"})  # fmt: skip
    assert state[remote_builds.STATE_KEY]["b" * 20]["progress"]["stage"] == "build"
    remote_builds.cancel(state, "b" * 20, "ana", 0.0)
    assert state[remote_builds.STATE_KEY] == {}
    failure = state[remote_builds.FAILED_KEY]["b" * 20]
    assert failure["reason"] == "cancelled by ana" and failure["host"] == "mini"


# ------------------------------------------------------------------ the CLI


def test_the_cli_starts_cancels_and_lists_builds(
    world: dict[str, Any], tmp_path: Path
) -> None:
    from piceli.k8s.cli import app

    controller = world["controller"]
    controller.poll_once()
    state = str(world["state"])
    runner = CliRunner()
    started = runner.invoke(
        app, ["gitops", "build", "web-1", "--ref", "web-v1.0.0", "--by", "ana",
              "--state-dir", state],
    )  # fmt: skip
    assert started.exit_code == 0, started.output
    body = json.loads(started.stdout)
    assert body["state"] == "requested" and body["trigger"] == {
        "kind": "cli",
        "by": "ana",
    }
    record = _release(controller.poll_once())
    assert record["state"] == "approval-required" and record["requested_by"] == "ana"
    shown = runner.invoke(app, ["gitops", "builds", "--json", "--state-dir", state])
    assert shown.exit_code == 0, shown.output
    (item,) = json.loads(shown.stdout)["builds"]
    assert item["trigger"]["kind"] == "cli" and item["stage"] == "approval-required"
    assert "web-1" in shown.stderr
    bad = runner.invoke(
        app, ["gitops", "builds", "--state", "nope", "--state-dir", state]
    )
    assert bad.exit_code == 2
    cancelled = runner.invoke(app, ["gitops", "cancel", "web-1", "--state-dir", state])
    assert cancelled.exit_code == 0, cancelled.output
    assert _release(controller.poll_once())["state"] == "cancelled"
    refused = runner.invoke(
        app, ["gitops", "build", "web-1", "--approve", "nope", "--state-dir", state]
    )
    assert refused.exit_code == 2


# ------------------------------------------------------------------ private inputs


def test_a_hand_started_publish_fetches_private_inputs_reports_and_cancels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.unit.infra.test_nix_releases import APP, FLAKE_LOCK, STORE

    infra = INFRA.replace(
        "auto_approve=AUTO)", 'auto_approve=AUTO, inputs={"app": "app-token"})'
    )
    world = _world(tmp_path, monkeypatch, infra=infra)
    controller, channel, repo = world["controller"], world["channel"], world["repo"]
    controller.poll_once()
    repo.commit({"flake.lock": FLAKE_LOCK})
    repo.tag("web-v1.1.0")
    controller.poll_once()
    channel.add_request(*build_request("web-1", "web-v1.1.0", by="ana"))
    record = _release(controller.poll_once())
    assert record["trigger_kind"] == "cli" and record["state"] == "approval-required"
    assert [item["input"] for item in record["plan"]["inputs"]] == ["app"]
    assert record["plan"]["inputs"][0]["url"] == f"git+{APP}"
    channel.add_request(*approve_request("web-1", record["plan_digest"], via="cli"))
    controller.poll_once()

    seen: list[str] = []

    def stage_now() -> str:
        status = controller.poll_once()
        (item,) = builds.records(status, None, now=world["clock"]["now"])["builds"]
        return str(item["stage"])

    def fetch(pairs: Any, say: Any, **_kw: Any) -> dict[str, Any]:
        say("fetching app")
        seen.append(stage_now())
        return {"state": "fetched", "inputs": [{"name": "app"}]}

    def build(checkout: Path, attribute: str, say: Any) -> str:
        seen.append(stage_now())
        return STORE

    worker, _ = _worker(world, tmp_path, fetch=fetch, build=build)
    assert worker.once() is not None
    status = controller.poll_once()
    assert seen == ["fetching-inputs", "building"]
    assert _release(status)["state"] == "published"
    history = json.loads((world["state"] / "history.json").read_text())
    (done,) = builds.records(status, history, now=0)["builds"]
    assert done["stage"] == "published" and done["trigger"]["by"] == "ana"
    assert [s["stage"] for s in done["stages"]][-3:] == [
        "fetching-inputs", "building", "uploading",
    ]  # fmt: skip

    # Cancelled while it fetches its inputs: the build never starts.
    channel.add_request(*build_request("web-1", "web-v1.1.0", by="ana"))
    assert _release(controller.poll_once())["state"] == "waiting-build"  # approved
    built: list[str] = []

    def cancelled_fetch(pairs: Any, say: Any, **_kw: Any) -> dict[str, Any]:
        channel.add_request(*build_cancel_request("web-1"))
        controller.poll_once()
        return {"state": "fetched", "inputs": []}

    worker, _ = _worker(
        world, tmp_path, fetch=cancelled_fetch,
        build=lambda *_a: built.append("x") or STORE,
    )  # fmt: skip
    assert worker.once() is not None
    status = controller.poll_once()
    assert built == [] and _release(status)["state"] == "cancelled"
    assert not [r for r in status["rejected_requests"] if r["kind"] == "build-failed"]
