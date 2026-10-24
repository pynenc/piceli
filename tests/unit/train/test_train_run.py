"""``piceli train`` on a toy repository with command steps (no cargo).

Each test builds a tiny Git repository with a bare remote in ``tmp_path``
(:mod:`tests.train_toy`) and runs the real command in-process.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli import app
from tests.train_toy import Toy, make_toy, spec_source


@pytest.fixture
def toy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Toy:
    monkeypatch.setenv("TOY_PUBLISH_OUT", str(tmp_path / "published"))
    # The train's cache (slots, targets, content store) stays in tmp_path.
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return make_toy(tmp_path)


def train(toy: Toy, *args: str) -> tuple[int, dict[str, Any], str]:
    result = CliRunner().invoke(
        app,
        ["train", toy.spec, *args, "--json", "--state-dir", str(toy.root / "state")],
    )
    lines = result.stdout.strip().splitlines()
    body = json.loads(lines[-1]) if lines else {}
    if result.exception and not isinstance(result.exception, SystemExit):
        raise result.exception
    return result.exit_code, body, result.stderr


def verdicts(body: dict[str, Any]) -> dict[str, str]:
    return {b["branch"]: b["verdict"] for b in body["branches"]}


def good(toy: Toy, name: str) -> None:
    toy.branch(name, {f"src/{name}.txt": f"{name}\n"})


def bad(toy: Toy, name: str) -> None:
    toy.branch(name, {f"src/{name}.txt": "BROKEN\n"})


def test_joint_run_joins_every_passing_branch_in_one_trial(toy: Toy) -> None:
    good(toy, "feat-a")
    good(toy, "feat-b")
    main = toy.rev("main")
    status, body, _ = train(toy, "--branches", "feat-a", "feat-b")
    assert status == 0, body
    assert verdicts(body) == {"feat-a": "joined", "feat-b": "joined"}
    assert len(body["trials"]) == 1
    assert body["trials"][0]["branches"] == ["feat-a", "feat-b"]
    # Without --promote nothing moved, and the head holds both merges in order.
    assert toy.rev("main") == main and toy.rev("tested") is None
    assert body["promote"] == {"state": "not-requested"}
    merges = toy.git("log", "--merges", "--format=%s", body["head"]).splitlines()
    assert merges == [
        "Merge branch 'feat-b' (train toy)",
        "Merge branch 'feat-a' (train toy)",
    ]


def test_bisect_finds_two_bad_branches_in_four(toy: Toy) -> None:
    good(toy, "g1")
    bad(toy, "b1")
    good(toy, "g2")
    bad(toy, "b2")
    status, body, stderr = train(toy, "--branches", "g1", "b1", "g2", "b2")
    assert status == 1
    assert verdicts(body) == {
        "g1": "joined",
        "b1": "ejected",
        "g2": "joined",
        "b2": "ejected",
    }
    trials = [t["branches"] for t in body["trials"]]
    assert trials == [
        ["g1", "b1", "g2", "b2"],
        ["g1", "b1"],
        ["g1"],
        ["b1"],
        ["g2", "b2"],
        ["g2"],
        ["b2"],
    ]
    for name in ("b1", "b2"):
        branch = next(b for b in body["branches"] if b["branch"] == name)
        assert branch["reason"] == "train-step-failed"
        assert branch["step"] == "unit"
        assert f"src/{name}.txt: BROKEN" in branch["tail"]
    # g2 was tried on the head that already held g1.
    g1_head = body["trials"][2]["head"]
    assert body["trials"][5]["base"] == g1_head
    assert "ejected: b1 (unit) [train-step-failed]" in stderr


def test_branch_json_has_timed_steps_with_logs_and_a_record(toy: Toy) -> None:
    good(toy, "feat-a")
    status, body, _ = train(toy, "--branches", "feat-a")
    assert status == 0
    (branch,) = body["branches"]
    names = [step["name"] for step in branch["steps"]]
    assert names == ["merge", "spec", "unit", "fitness"]
    for step in branch["steps"]:
        assert step["host"] == "local"
        assert step["verdict"] == "passed"
        assert step["duration"] >= 0
        datetime.strptime(step["started"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    unit = branch["steps"][2]
    assert Path(unit["log"]).is_file() and "unit: ok" in Path(unit["log"]).read_text()
    assert branch["steps"][1]["detail"] == {
        "source": "merged-tree",
        "steps": ["unit", "fitness"],
    }
    record = json.loads(Path(body["record"]).read_text())
    assert record == body
    assert body["schema"] == "piceli.train.v1"


def test_conflict_ejects_with_its_files_and_the_others_join(toy: Toy) -> None:
    toy.branch("first", {"src/shared.txt": "one\nFIRST\nthree\n"})
    toy.branch("second", {"src/shared.txt": "one\nSECOND\nthree\n"})
    status, body, _ = train(toy, "--branches", "first", "second")
    assert status == 1
    assert verdicts(body) == {"first": "joined", "second": "ejected"}
    second = body["branches"][1]
    assert second["reason"] == "train-merge-conflict"
    assert second["step"] == "merge"
    assert second["files"] == ["src/shared.txt"]


def test_a_conflict_with_an_ejected_branch_does_not_eject(toy: Toy) -> None:
    """``late`` conflicts only with ``broken``, which fails its tests: once
    ``broken`` is out, ``late`` merges cleanly onto the head and joins."""
    toy.branch("broken", {"src/shared.txt": "one\nBROKEN\nthree\n"})
    toy.branch("late", {"src/shared.txt": "one\nLATE\nthree\n"})
    status, body, _ = train(toy, "--branches", "broken", "late")
    assert status == 1
    assert verdicts(body) == {"broken": "ejected", "late": "joined"}
    assert body["branches"][0]["reason"] == "train-step-failed"
    assert body["trials"][0]["reason"] == "train-merge-conflict"


def test_steps_come_from_the_merged_tree(toy: Toy) -> None:
    """A branch is judged by its base plus itself: the base's newer rule
    ejects an old branch; another branch's own allowlist lets it join; a
    step the base added runs for both."""
    toy.branch("old", {"src/legacy.txt": "a legacy shim\n"})
    toy.commit(
        {
            "rules/forbidden.txt": "forbidden\nlegacy\n",
            "train_spec.py": spec_source(
                cargo=False,
                extra_steps='\n        Step.command("docs", [sys.executable, "-c", "print(1)"]),',
            ),
        },
        "rules: forbid legacy; a docs step",
    )
    toy.branch(
        "allowed",
        {"src/compat.txt": "legacy by design\n", "rules/allow.txt": "src/compat.txt\n"},
    )
    status, body, _ = train(toy, "--branches", "old", "allowed")
    assert status == 1
    assert verdicts(body) == {"old": "ejected", "allowed": "joined"}
    old, allowed = body["branches"]
    assert old["step"] == "fitness" and "src/legacy.txt: uses 'legacy'" in old["tail"]
    assert [s["name"] for s in allowed["steps"]] == [
        "merge", "spec", "unit", "fitness", "docs",
    ]  # fmt: skip
    # The caller's checkout lacks the allowlist entry: the merged tree's was used.
    assert (toy.repo / "rules" / "allow.txt").read_text() == ""


def test_a_branch_that_breaks_the_spec_is_ejected(toy: Toy) -> None:
    toy.branch("spec-broken", {"train_spec.py": "raise RuntimeError('nope')\n"})
    status, body, _ = train(toy, "--branches", "spec-broken")
    assert status == 1
    (branch,) = body["branches"]
    assert branch["reason"] == "train-spec-invalid" and branch["step"] == "spec"
    assert "RuntimeError: nope" in branch["tail"]


def test_a_branch_already_in_the_base_is_included(toy: Toy) -> None:
    toy.git("branch", "old-news", "main")
    good(toy, "feat-a")
    status, body, _ = train(toy, "--branches", "old-news", "feat-a")
    assert status == 0
    assert verdicts(body) == {"old-news": "included", "feat-a": "joined"}


def test_promote_tags_and_fast_forwards_then_publishes_without_pushing(
    toy: Toy,
) -> None:
    good(toy, "feat-a")
    before = toy.remote_refs()
    status, body, _ = train(toy, "--branches", "feat-a", "--promote")
    assert status == 0, body
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    tag = f"tested/{today}.1"
    assert body["tags"] == [tag]
    assert body["promote"]["state"] == "promoted"
    head = body["head"]
    assert toy.rev("main") == head and toy.rev("tested") == head
    assert toy.rev(f"refs/tags/{tag}") == head
    # main is checked out in the toy's checkout: it moved there by fast-forward.
    assert (toy.repo / "src" / "feat-a.txt").is_file()
    published = toy.root / "published" / tag.replace("/", "_")
    assert published.read_text() == head
    assert body["promote"]["publish"][0]["verdict"] == "passed"
    assert toy.remote_refs() == before  # nothing pushed without --push
    assert body["push"] == {"state": "not-requested"}
    good(toy, "feat-b")
    status, body, _ = train(toy, "--branches", "feat-b", "--promote")
    assert status == 0, body
    assert body["tags"] == [f"tested/{today}.2"]


def test_no_publish_without_promote(toy: Toy) -> None:
    good(toy, "feat-a")
    status, _, _ = train(toy, "--branches", "feat-a")
    assert status == 0
    assert not (toy.root / "published").exists()


def test_push_sends_the_tag_tested_and_base(toy: Toy) -> None:
    good(toy, "feat-a")
    status, body, _ = train(toy, "--branches", "feat-a", "--promote", "--push")
    assert status == 0, body
    refs = toy.remote_refs()
    head = body["head"]
    assert refs["refs/heads/main"] == head
    assert refs["refs/heads/tested"] == head
    assert body["push"]["state"] == "pushed"
    assert any(name.startswith("refs/tags/tested/") for name in refs)


def test_promote_refuses_a_tested_branch_that_is_not_an_ancestor(toy: Toy) -> None:
    toy.branch("tested", {"src/side.txt": "made outside the train\n"})
    good(toy, "feat-a")
    main, tested = toy.rev("main"), toy.rev("tested")
    status, body, stderr = train(toy, "--branches", "feat-a", "--promote")
    assert status == 1
    assert verdicts(body) == {"feat-a": "joined"}
    assert body["promote"]["state"] == "rejected"
    assert body["promote"]["reason"] == "train-not-fast-forward"
    assert body["tags"] == []
    assert toy.rev("main") == main and toy.rev("tested") == tested
    assert toy.git("tag", "--list") == ""
    assert not (toy.root / "published").exists()
    assert "[train-not-fast-forward]" in stderr


def test_promote_refuses_a_dirty_checkout_of_the_base(toy: Toy) -> None:
    good(toy, "feat-a")
    (toy.repo / "README.md").write_text("# edited, not committed\n")
    main = toy.rev("main")
    status, body, _ = train(toy, "--branches", "feat-a", "--promote")
    assert status == 1
    assert body["promote"]["reason"] == "train-checkout-dirty"
    assert toy.rev("main") == main and toy.rev("tested") is None


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        (["--branches"], "train-branches-required"),
        (["--branches", "nope"], "train-branch-unknown"),
        (["--branches", "main"], "train-branch-unknown"),
        (["--branches", "x", "--push"], "train-push-needs-promote"),
    ],
)
def test_usage_errors_are_rejected(toy: Toy, args: list[str], reason: str) -> None:
    status, body, _ = train(toy, *args)
    assert status == 2
    assert body["state"] == "rejected" and body["reason"] == reason


def test_branches_can_follow_the_app_without_the_flag(toy: Toy) -> None:
    good(toy, "feat-a")
    status, body, _ = train(toy, "feat-a")
    assert status == 0 and verdicts(body) == {"feat-a": "joined"}


def test_a_repository_without_a_git_identity_still_merges(
    toy: Toy, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A CI runner or a build host often has no user.name/user.email: the train
    # commits its merges as itself instead of ejecting every branch.
    good(toy, "feat-a")
    toy.git("config", "--unset", "user.name")
    toy.git("config", "--unset", "user.email")
    toy.git("config", "user.useConfigOnly", "true")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(toy.root / "no-global-config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("NAME", "EMAIL"):
        for who in ("AUTHOR", "COMMITTER"):
            monkeypatch.delenv(f"GIT_{who}_{name}", raising=False)
    status, body, _ = train(toy, "--branches", "feat-a")
    assert status == 0, body
    assert verdicts(body) == {"feat-a": "joined"}
    author = toy.git("log", "-1", "--format=%an <%ae>", body["head"])
    assert author == "Piceli train <train@piceli.invalid>"


def test_a_merge_that_fails_without_a_conflict_says_why(
    toy: Toy, monkeypatch: pytest.MonkeyPatch
) -> None:
    from piceli.train import git as train_git

    good(toy, "feat-a")
    real = train_git.Git.run

    def failing(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if args and args[0] == "merge" and "--no-ff" in args:
            done = real(
                self,
                "--version",
                **{k: v for k, v in kwargs.items() if k != "check"},
                check=False,
            )
            done.returncode, done.stderr = 128, "fatal: unable to write new index file"
            return done
        return real(self, *args, **kwargs)

    monkeypatch.setattr(train_git.Git, "run", failing)
    status, body, _ = train(toy, "--branches", "feat-a")
    assert status == 1
    branch = body["branches"][0]
    assert branch["verdict"] == "ejected" and branch["reason"] == "train-merge-failed"
    assert "unable to write new index file" in branch["tail"]
