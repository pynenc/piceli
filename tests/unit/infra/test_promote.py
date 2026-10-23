"""``piceli promote`` (0.24.0): pin the commit an environment verified.

A flake repository with a bare remote and a working clone, its input's
repository and a controller history in a ``--state-dir``, all in
``tmp_path``. Without nix the lock is edited (its narHash computed here);
with nix on the PATH one test also locks with ``nix flake lock`` and checks
that both agree. No network.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.infra import promote
from piceli.infra.promotion import locked_rev
from piceli.k8s.cli import app
from tests.unit.infra import promotion_support as support
from tests.unit.infra.git_support import git
from tests.unit.infra.promotion_support import history, run


@pytest.fixture
def found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    world = support.world(tmp_path, monkeypatch)
    monkeypatch.setattr(promote.shutil, "which", lambda _name: None)  # edit path
    support.publish_history(
        world["state"],
        history(
            run(world["c"], "r3", "2026-10-22T10:00:00Z"),
            run(world["b"], "r2", "2026-10-21T10:00:00Z", passed=False),
            run(world["a"], "r1", "2026-10-20T10:00:00Z"),
        ),
    )
    return world


def _promote(found: dict[str, Any], *extra: str) -> tuple[int, dict[str, Any], str]:
    result = CliRunner().invoke(app, [
        "promote", "web-1", "--from", "main", "--composition", str(found["infra"]),
        "--repo", str(found["work"]), "--state-dir", str(found["state"]), "--json",
        *extra,
    ])  # fmt: skip
    body = json.loads(result.stdout) if result.stdout.strip() else {}
    return result.exit_code, body, result.stderr


def _remote(found: dict[str, Any]) -> dict[str, str]:
    lines = git(found["work"], "ls-remote", found["hosts"].url).splitlines()
    return {ref: sha for sha, ref in (line.split("\t") for line in lines)}


def test_plan_then_approve_pins_commits_tags_and_pushes(found: dict[str, Any]) -> None:
    work, a, c = found["work"], found["a"], found["c"]
    before = _remote(found)
    code, body, said = _promote(found)
    assert code == 3, body
    plan = body["plan"]
    head = git(work, "rev-parse", "HEAD")
    assert body["state"] == "approval-required"
    assert plan["pinned"] == {"input": "app", "commit": c, "environment": "main",
                              "verified_run": "r3",
                              "verified_at": "2026-10-22T10:00:00Z"}  # fmt: skip
    assert plan["change"]["from"] == a and plan["change"]["to"] == c
    assert plan["change"]["method"] == "edit" and plan["change"]["file"] == "flake.lock"
    assert (plan["tag"], plan["previous_tag"], plan["bump"]) == (
        "web-v1.0.1", "web-v1.0.0", "patch")  # fmt: skip
    assert (plan["remote"], plan["branch"], plan["base"]) == ("origin", "main", head)
    assert plan["commit_message"].startswith(f"promote app to {c[:12]} from main")
    assert body["approve_command"].endswith(f"--approve {body['digest']}")
    assert f"pin app {a[:12]} -> {c[:12]} (edit)" in said
    assert git(work, "status", "--porcelain") == ""  # the plan changed nothing
    assert _remote(found) == before

    code, done, _ = _promote(found, "--approve", body["digest"])
    assert code == 0, done
    assert done["state"] == "promoted" and done["tag"] == "web-v1.0.1"
    new = git(work, "rev-parse", "HEAD")
    assert done["commit"] == new and git(work, "rev-parse", "HEAD~1") == head
    assert git(work, "show", "--name-only", "--format=", "HEAD") == "flake.lock"
    remote = _remote(found)
    assert remote["refs/heads/main"] == new
    assert remote["refs/tags/web-v1.0.1^{}"] == new
    lock = git(work, "show", "HEAD:flake.lock")
    assert locked_rev(lock, "app") == c
    node = json.loads(lock)["nodes"]["app"]["locked"]
    assert node["revCount"] == 3 and node["narHash"] != "sha256-" + "A" * 43 + "="

    code, again, said = _promote(found)
    assert code == 0 and again["state"] == "current" and again["plan"] is None
    assert again["tag"] == "web-v1.0.1" and "points at it" in said


def test_a_pinned_commit_without_a_tag_is_offered_a_tag(found: dict[str, Any]) -> None:
    support.publish_history(
        found["state"], history(run(found["a"], "r1", "2026-10-20T10:00:00Z"))
    )
    code, body, _ = _promote(found)
    assert code == 0 and body["state"] == "current"
    assert body["plan"] is None and body["tag"] == "web-v1.0.0"  # tagged already

    found["hosts"].commit({"README.md": "hosts\n"})
    git(found["work"], "pull", "-q")
    code, body, said = _promote(found)
    assert code == 0 and body["state"] == "current", body
    assert body["plan"]["change"] is None and body["plan"]["tag"] == "web-v1.0.1"
    assert "approving tags" in said
    code, done, _ = _promote(found, "--approve", body["digest"])
    assert code == 0, done
    head = git(found["work"], "rev-parse", "HEAD")
    assert done["commit"] == head
    assert _remote(found)["refs/tags/web-v1.0.1^{}"] == head


@pytest.mark.parametrize("case", ["dirty", "behind", "ahead", "diverged", "detached"])
def test_a_working_copy_out_of_sync_is_refused(
    found: dict[str, Any], case: str
) -> None:
    work = found["work"]
    if case == "dirty":
        (work / "flake.nix").write_text("{ }\n")
    if case in ("behind", "diverged"):
        found["hosts"].commit({"README.md": "upstream\n"})
    if case in ("ahead", "diverged"):
        (work / "notes.txt").write_text("local\n")
        git(work, "add", "notes.txt")
        git(work, "commit", "-q", "-m", "local")
    if case == "detached":
        git(work, "checkout", "-q", "--detach")
    code, body, _ = _promote(found)
    assert code == 2
    assert body["reason"] == (
        "promote-dirty" if case == "dirty" else "promote-not-in-sync"
    )
    words = {"behind": "is behind", "ahead": "is ahead of", "diverged": "diverged from",
             "detached": "detached HEAD", "dirty": "uncommitted"}[case]  # fmt: skip
    assert words in body["message"]


def test_other_refusals(found: dict[str, Any]) -> None:
    code, body, _ = _promote(found, "--approve", "sha256:" + "0" * 64)
    assert (code, body["reason"]) == (2, "promote-plan-changed")
    code, body, _ = _promote(found, "--bump", "huge")
    assert (code, body["reason"]) == (2, "promote-invalid")
    result = CliRunner().invoke(app, [
        "promote", "web-1", "--from", "rc", "--composition", str(found["infra"]),
        "--state-dir", str(found["state"]),
    ])  # fmt: skip
    assert result.exit_code == 2
    assert json.loads(result.stdout)["reason"] == "promote-invalid"
    support.publish_history(
        found["state"],
        history(run(found["c"], "r9", "2026-10-22T10:00:00Z", passed=False)),
    )
    code, body, _ = _promote(found)
    assert (code, body["reason"]) == (2, "promotion-not-verified")
    (found["state"] / "history.json").unlink()
    code, body, _ = _promote(found)
    assert (code, body["reason"]) == (2, "promotion-history-unreachable")


def test_a_refused_push_leaves_the_remote_unchanged(found: dict[str, Any]) -> None:
    hook = Path(found["hosts"].url) / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    before = _remote(found)
    code, body, _ = _promote(found)
    code, done, _ = _promote(found, "--approve", body["digest"])
    assert (code, done["reason"]) == (1, "promote-push-failed")
    assert done["tag"] == "web-v1.0.1" and "git push --atomic origin" in done["message"]
    assert _remote(found) == before


def test_next_tag_bumps_the_highest_release_of_the_pattern() -> None:
    tags = ["web-v1.9.0", "web-v1.10.0", "web-v2.0.0-rc.1", "api-v5.0.0", "v9.0.0"]
    assert promote.next_tag(tags, "web-v*", "patch") == ("web-v1.10.1", "web-v1.10.0")
    assert promote.next_tag(tags, "web-v*", "minor")[0] == "web-v1.11.0"
    assert promote.next_tag(tags, "web-v*", "major")[0] == "web-v2.0.0"
    assert promote.next_tag([], "web-v*", "patch") == ("web-v0.1.0", None)
    assert promote.next_tag(["web-v1.2.3"], "web-*", "patch")[0] == "web-v1.2.4"


def test_override_urls() -> None:
    rev = "f" * 40
    assert promote.override_url({"type": "github", "owner": "o", "repo": "r",
                                 "ref": "main"}, rev) == f"github:o/r/{rev}"  # fmt: skip
    assert (
        promote.override_url(
            {"type": "git", "url": "https://h/r.git", "ref": "main"}, rev
        )
        == f"git+https://h/r.git?ref=main&rev={rev}"
    )
    with pytest.raises(promote.PromotionError) as refused:
        promote.override_url({"type": "path", "path": "/x"}, rev)
    assert refused.value.code == "promote-lock-failed"


@pytest.mark.skipif(shutil.which("nix") is None, reason="needs nix")
def test_nix_and_the_edit_path_lock_the_same_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = support.world(tmp_path, monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    top, c = world["work"], world["c"]
    head = git(top, "rev-parse", "HEAD")
    by_nix, method = promote.pinned_lock(top, head, "app", c)
    assert method == "nix"
    old = git(top, "show", "HEAD:flake.lock").encode()
    by_edit = promote.lock_by_edit(old, "app", c)
    nixed = json.loads(by_nix)["nodes"]["app"]["locked"]
    edited = json.loads(by_edit)["nodes"]["app"]["locked"]
    for key in ("rev", "narHash", "lastModified", "revCount"):
        assert nixed[key] == edited[key], key
    assert git(top, "worktree", "list").count("\n") == 0  # the worktree is gone
