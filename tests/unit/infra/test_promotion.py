"""The promotion gate of a NixRelease (0.24.0, ``gate=Promotion(...)``).

The gate's rules on a history, the declaration, the controller's release
plan (refused, then published once the environment passed the pinned
commit, with ``pinned`` in the plan, the status and the signed manifest)
and a hand-run ``dev nix build --publish --release``. Local Git
repositories in ``tmp_path``; no network, no cluster.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from piceli.dev import nix
from piceli.infra import CompositionError
from piceli.infra.composition import Composition, load_composition
from piceli.infra.promotion import (
    Promotion,
    PromotionError,
    locked_rev,
    newest,
    passed_runs,
    verify,
)
from tests.unit.dev.test_nix import ATTR, _invoke, cli_env
from tests.unit.infra import promotion_support as support
from tests.unit.infra.git_support import git
from tests.unit.infra.promotion_support import history, run

__all__ = ["cli_env"]

A, B, C = "a" * 40, "b" * 40, "c" * 40
GATE = Promotion(input="app", environment="main")
PINNED_KEYS = {"input", "commit", "environment", "verified_run", "verified_at"}


# ------------------------------------------------------------------ the rules


def test_only_deployed_runs_with_every_check_passed_qualify() -> None:
    document = history(
        run(A, "r1", "2026-10-20T10:00:00Z"),
        run(B, "r2", "2026-10-21T10:00:00Z", passed=False),
        run(B, "r3", "2026-10-21T11:00:00Z", state="failed"),
        run(B, "r4", "2026-10-21T12:00:00Z", rollback=True),
        run(B, "r5", "2026-10-21T13:00:00Z", state="degraded"),
        run(B, "r6", "2026-10-21T14:00:00Z", checks=0),  # no checks: no proof
        run(C, "r7", "2026-10-22T10:00:00Z", source="other"),
        {"kind": "stop", "state": "stopped", "at": "2026-10-22T11:00:00Z"},
    )
    assert [item["run_id"] for item in passed_runs(document, GATE)] == ["r1"]
    assert newest(document, GATE) == {
        "commit": A, "run_id": "r1", "verified_at": "2026-10-20T10:00:00Z",
    }  # fmt: skip
    assert verify(GATE, A, document) == {
        "input": "app", "commit": A, "environment": "main",
        "verified_run": "r1", "verified_at": "2026-10-20T10:00:00Z",
    }  # fmt: skip


def test_an_unverified_commit_is_refused_with_the_newest_passed_one() -> None:
    document = history(
        run(A, "r1", "2026-10-20T10:00:00Z"), run(B, "r2", "2026-10-21T10:00:00Z")
    )
    with pytest.raises(PromotionError) as refused:
        verify(GATE, C, document, host="web-1")
    error = refused.value
    assert error.code == "promotion-not-verified"
    assert error.detail == {
        "input": "app", "commit": C, "environment": "main",
        "newest_passed": {"commit": B, "run_id": "r2",
                          "verified_at": "2026-10-21T10:00:00Z"},
    }  # fmt: skip
    assert "piceli promote web-1 --from main" in str(error)
    assert B[:12] in str(error)
    with pytest.raises(PromotionError, match="no run with passed"):
        verify(GATE, C, history())


@pytest.mark.parametrize("document", [None, {}, {"schema": "other"}, []])
def test_no_history_never_passes(document: Any) -> None:
    with pytest.raises(PromotionError) as refused:
        verify(GATE, A, document)
    assert refused.value.code == "promotion-history-unreachable"


def test_the_locked_rev_of_an_input() -> None:
    text = support.lock("/srv/app.git", A)
    assert locked_rev(text, "app") == A
    for lock, words in (
        (None, "no flake.lock"),
        ("{", "not JSON"),
        (text, "no locked root input 'web'"),
        (text.replace(A, "main"), "no Git commit"),
    ):
        with pytest.raises(PromotionError, match=words) as refused:
            locked_rev(lock, "web" if "web" in words else "app")
        assert refused.value.code == "promotion-input-missing"


# ------------------------------------------------------------------ declaration


def test_a_gated_release_names_an_environment_that_follows_its_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    found = support.world(tmp_path, monkeypatch)
    composition = load_composition(found["infra"])
    (target,) = composition.nix_releases
    assert target.gate == Promotion("app", "main", "app")
    body = composition.to_dict()
    assert body["nix_releases"][0]["gate"] == {
        "input": "app", "environment": "main", "source": "app",
    }  # fmt: skip
    assert Composition.from_dict(body).nix_releases == (target,)
    text = found["infra"].read_text()
    for old, new in (
        ('environment="main"', 'environment="rc"'),
        ("Promotion(input=", 'Promotion(source="hosts", input='),
    ):
        found["infra"].write_text(text.replace(old, new))
        with pytest.raises(CompositionError, match="gate"):
            load_composition(found["infra"])
    for kwargs, words in (
        ({"input": "", "environment": "main"}, "flake input"),
        ({"input": "app", "environment": "Main Env"}, "environment's name"),
        ({"input": "app", "environment": "main", "source": 3}, "Source"),
    ):
        with pytest.raises(CompositionError, match=words):
            Promotion(**kwargs)


# ------------------------------------------------------------------ the controller


def _controller(found: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> Any:
    from piceli.gitops.state import DirectoryChannel
    from piceli.infra.controller import CompositionConfig, CompositionController
    from piceli.infra.machines.credentials import save_credential
    from piceli.infra.sources import SourceSet
    from tests.unit.infra.test_controller import FakePorts

    monkeypatch.setenv(
        "PICELI_CREDENTIALS_DIR", str(found["state"].parent / "credentials")
    )
    save_credential("cache-1", "nix-signing-key", support.SECRET)
    composition = load_composition(found["infra"])
    config = CompositionConfig(composition=composition.to_dict(), poll_seconds=10)
    state = found["state"] / "controller"
    clock = {"now": 1_000_000.0}
    controller = CompositionController(
        config,
        state_dir=state,
        sources=SourceSet(composition.sources, state / "sources"),
        ports=FakePorts(),
        channel=DirectoryChannel(state),
        clock=lambda: clock["now"],
    )
    document = {"value": history()}
    monkeypatch.setattr(controller, "_history_document", lambda: document["value"])
    found.update(controller=controller, channel=controller.channel, clock=clock,
                 history=document, cache=found["cache"], controller_state=state)  # fmt: skip
    return controller


def test_the_controller_publishes_only_a_verified_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.unit.infra.test_nix_releases import _worker

    found = support.world(tmp_path, monkeypatch)
    controller = _controller(found, monkeypatch)
    hosts, a, b = found["hosts"], found["a"], found["b"]
    status = controller.poll_once()
    assert status["nix_releases"]["web-1"]["state"] == "baseline"

    found["history"]["value"] = history(
        run(a, "r1", "2026-10-20T10:00:00Z"),
        run(b, "r2", "2026-10-21T10:00:00Z", passed=False),
    )
    commit = hosts.commit({"flake.lock": support.lock(found["app"].url, b)})
    hosts.tag("web-v1.1.0")
    record = controller.poll_once()["nix_releases"]["web-1"]
    assert (
        record["state"] == "rejected" and record["reason"] == "promotion-not-verified"
    )
    assert (record["tag"], record["commit"]) == ("web-v1.1.0", commit)
    assert record["plan"] is None and record["pinned"] is None
    assert record["refusal"]["newest_passed"]["commit"] == a
    assert record["refusal"]["commit"] == b
    status = controller.poll_once()  # checked again: still refused, once in history
    assert status["controller"]["builds_wanted"] == []
    events = controller.state["nix_release_history"]["web-1"]
    assert [item["state"] for item in events] == ["rejected"]

    found["history"]["value"] = history(
        run(b, "r3", "2026-10-21T12:00:00Z"), run(a, "r1", "2026-10-20T10:00:00Z")
    )
    record = controller.poll_once()["nix_releases"]["web-1"]
    assert record["state"] == "waiting-build"
    pinned = {"input": "app", "commit": b, "environment": "main",
              "verified_run": "r3", "verified_at": "2026-10-21T12:00:00Z"}  # fmt: skip
    assert record["pinned"] == pinned and record["plan"]["pinned"] == pinned
    assert nix.plan_digest(record["plan"]) == record["plan_digest"]

    found["clock"]["now"] += 60
    worker, _ = _worker(found, tmp_path)
    assert worker.once() is not None
    status = controller.poll_once()
    done = status["nix_releases"]["web-1"]
    assert done["state"] == "published" and done["pinned"] == pinned
    manifest = json.loads((found["cache"] / f"piceli/web-1/{commit}.json").read_text())
    assert manifest["pinned"] == pinned and nix.verify_manifest(
        manifest, support.PUBLIC
    )
    tampered = {**manifest, "pinned": {**pinned, "commit": a}}
    assert not nix.verify_manifest(tampered, support.PUBLIC)

    from typer.testing import CliRunner

    from piceli.k8s.cli import app

    shown = CliRunner().invoke(
        app, ["gitops", "status", "--state-dir", str(found["controller_state"]),
              "--json"],
    )  # fmt: skip
    assert shown.exit_code == 0, shown.stdout + shown.stderr
    assert json.loads(shown.stdout)["nix_releases"]["web-1"]["pinned"] == pinned
    assert f"pinned app {b[:12]} verified on main (run r3)" in shown.stderr


def test_an_unreadable_history_refuses_the_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    found = support.world(tmp_path, monkeypatch)
    controller = _controller(found, monkeypatch)
    controller.poll_once()

    def unreadable() -> Any:
        raise OSError("no history")

    monkeypatch.setattr(found["controller"], "_history_document", unreadable)
    found["hosts"].commit({"flake.lock": support.lock(found["app"].url, found["b"])})
    found["hosts"].tag("web-v1.1.0")
    record = controller.poll_once()["nix_releases"]["web-1"]
    assert record["state"] == "rejected"
    assert record["reason"] == "promotion-history-unreachable"


def test_old_manifests_still_verify() -> None:
    published = {"store_path": "/nix/store/" + "a" * 32 + "-x",
                 "nar_hash": "sha256:" + nix.nix32(b"\x01" * 32), "nar_size": 1}  # fmt: skip
    body = nix.manifest(name="web-1", commit=A, tag="web-v1.0.0", target=ATTR,
                        published=published, host="mini", key="cache-1")  # fmt: skip
    assert "pinned" not in body
    assert nix.verify_manifest(nix.sign_manifest(body, support.SECRET), support.PUBLIC)


# ------------------------------------------------------------------ hand-run


def test_a_hand_run_publish_runs_the_gate(
    cli_env: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    found = support.world(tmp_path / "w", monkeypatch)
    flake = cli_env["app"]
    (flake / "flake.lock").write_text(support.lock(found["app"].url, found["b"]))
    git(flake, "add", "flake.lock")
    git(flake, "commit", "-q", "-m", "pin b")
    args = ("dev", "nix", "build", ATTR, "--cluster", "infra.py:c", "--ref", "HEAD",
            "--name", "web-1", "--publish", "infra.py:cache", "--release", "web-1",
            "--composition", str(found["infra"]),
            "--state-dir", str(found["state"]), "--json")  # fmt: skip

    unreachable = _invoke(*args)
    assert unreachable.exit_code == 2, unreachable.stdout + unreachable.stderr
    assert json.loads(unreachable.stdout)["reason"] == "promotion-history-unreachable"

    support.publish_history(
        found["state"], history(run(found["a"], "r1", "2026-10-20T10:00:00Z"))
    )
    refused = _invoke(*args)
    assert refused.exit_code == 2
    body = json.loads(refused.stdout)
    assert body["reason"] == "promotion-not-verified"
    assert (
        body["newest_passed"]["commit"] == found["a"] and body["commit"] == found["b"]
    )
    assert cli_env["runs"] == []  # refused before any build

    support.publish_history(
        found["state"], history(run(found["b"], "r2", "2026-10-21T10:00:00Z"))
    )
    planned = _invoke(*args)
    assert planned.exit_code == 3, planned.stdout + planned.stderr
    plan = json.loads(planned.stdout)
    assert plan["plan"]["pinned"]["commit"] == found["b"]
    assert set(plan["plan"]["pinned"]) == PINNED_KEYS
    assert "--release web-1 --composition" in plan["approve_command"]
    done = _invoke(*args, "--approve", plan["digest"])
    assert done.exit_code == 0, done.stdout + done.stderr
    head = git(flake, "rev-parse", "HEAD")
    manifest = json.loads((cli_env["cache"] / f"piceli/web-1/{head}.json").read_text())
    assert manifest["pinned"] == plan["plan"]["pinned"]

    other = _invoke(*[a if a != ATTR else "nixosConfigurations.api.config.system"
                      ".build.toplevel" for a in args])  # fmt: skip
    assert json.loads(other.stdout)["reason"] == "dev-nix-invalid"
