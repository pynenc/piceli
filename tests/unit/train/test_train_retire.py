"""``--retire``: joined branches are renamed ``to_delete_<date>_<branch>``
once promoted (never deleted); ``--retire-remote`` also deletes them on the
remote after the push. A branch checked out with changes, or one that moved
after it joined, is never touched."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli import app
from piceli.train.status import overview, table
from piceli.train.summary import headline, summarize
from tests.train_toy import Toy, make_toy


@pytest.fixture
def toy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Toy:
    monkeypatch.setenv("TOY_PUBLISH_OUT", str(tmp_path / "published"))
    for name in list(os.environ):
        if name.startswith("OTEL_"):
            monkeypatch.delenv(name)
    return make_toy(tmp_path)


def run(toy: Toy, *args: str) -> tuple[int, dict[str, Any], str]:
    result = CliRunner().invoke(
        app,
        ["train", toy.spec, *args, "--json", "--state-dir", str(toy.root / "state")],
    )
    if result.exception and not isinstance(result.exception, SystemExit):
        raise result.exception
    lines = result.stdout.strip().splitlines()
    return result.exit_code, json.loads(lines[-1]) if lines else {}, result.stderr


def retired_name(branch: str) -> str:
    return f"to_delete_{datetime.now().strftime('%Y-%m-%d')}_{branch}"


def heads(toy: Toy) -> set[str]:
    return set(
        toy.git("for-each-ref", "--format=%(refname:short)", "refs/heads").split()
    )


def test_promote_retire_renames_joined_branches_and_never_touches_the_rest(
    toy: Toy,
) -> None:
    for name in ("feat-a", "feat-b", "feat-c", "feat-d"):
        toy.branch(name, {f"src/{name}.txt": f"{name}\n"})
    toy.branch("fix-e", {"src/fix-e.txt": "BROKEN\n"})
    status, first, _err = run(
        toy, "--branches", "feat-a", "feat-b", "feat-c", "feat-d", "fix-e"
    )
    assert status == 1 and first["promote"]["state"] == "not-requested"
    assert "retire" not in first or first["retire"]["state"] == "not-requested"
    # feat-b is checked out with changes; feat-c moved after it joined;
    # feat-d is checked out in a clean worktree.
    dirty = toy.root / "wt-b"
    toy.git("worktree", "add", "-q", str(dirty), "feat-b")
    (dirty / "src" / "feat-b.txt").write_text("work in progress\n")
    toy.git("checkout", "-q", "feat-c")
    toy.commit({"src/feat-c.txt": "more\n"}, "feat-c: after joining")
    toy.git("checkout", "-q", "main")
    clean = toy.root / "wt-d"
    toy.git("worktree", "add", "-q", str(clean), "feat-d")

    status, body, err = run(toy, "--promote-run", "last", "--retire")
    # Exit 1: the promoted run had ejected fix-e, which is never retired.
    assert status == 1 and body["promote"]["state"] == "promoted", err
    retire = body["retire"]
    assert retire["mode"] == "local" and retire["state"] == "retired"
    assert {r["branch"]: r["renamed"] for r in retire["retired"]} == {
        "feat-a": retired_name("feat-a"),
        "feat-d": retired_name("feat-d"),
    }
    assert {s["branch"]: s["reason"] for s in retire["skipped"]} == {
        "feat-b": "checked-out-with-changes",
        "feat-c": "moved",
    }
    names = heads(toy)
    assert {
        retired_name("feat-a"),
        retired_name("feat-d"),
        "feat-b",
        "feat-c",
        "fix-e",
    } <= names
    assert "feat-a" not in names and "feat-d" not in names
    # Renamed, never deleted: the commit is still there under the new name.
    assert toy.git("rev-parse", retired_name("feat-a")) == first["branches"][0]["tip"]
    assert toy.git("-C", str(clean), "branch", "--show-current") == retired_name(
        "feat-d"
    )
    assert (dirty / "src" / "feat-b.txt").read_text() == "work in progress\n"
    assert "retired: feat-a" in err
    # Status and notifications say what was retired.
    summary = summarize(body)
    assert summary["retired"] == ["feat-a", "feat-d"]
    assert "retired 2" in headline(summary)
    events = [
        json.loads(line)
        for line in Path(body["record"])
        .with_name("events.jsonl")
        .read_text()
        .splitlines()
    ]
    assert [e["branch"] for e in events if e["event"] == "branch-retired"] == [
        "feat-a",
        "feat-d",
    ]
    view = overview(toy.root / "state", ["toy"])
    (latest, *_rest) = view["trains"][0]["runs"]
    assert latest["retired"] == ["feat-a", "feat-d"]
    assert "retired feat-a, feat-d" in "\n".join(table(view))


def test_retire_remote_deletes_the_pushed_branch_after_the_push(toy: Toy) -> None:
    toy.branch("feat-a", {"src/feat-a.txt": "a\n"})
    toy.branch("feat-b", {"src/feat-b.txt": "b\n"})
    toy.git("push", "-q", "origin", "feat-a")

    def remote() -> set[str]:
        listed = toy.git("ls-remote", "--heads", "origin").replace("refs/heads/", "")
        return set(listed.split()[1::2])

    assert "feat-a" in remote()
    status, body, err = run(
        toy, "--branches", "feat-a", "feat-b", "--promote", "--push", "--retire-remote"
    )
    assert status == 0, err
    assert body["push"]["state"] == "pushed"
    states = {r["branch"]: r["remote"] for r in body["retire"]["retired"]}
    assert states == {"feat-a": "deleted", "feat-b": "absent"}
    assert "feat-a" not in remote() and "main" in remote()
    assert {retired_name("feat-a"), retired_name("feat-b")} <= heads(toy)


@pytest.mark.parametrize(
    "args",
    [
        ("--branches", "feat-a", "--retire"),
        ("--branches", "feat-a", "--promote", "--retire-remote"),
    ],
)
def test_retire_needs_a_promotion_and_retire_remote_a_push(
    toy: Toy, args: tuple[str, ...]
) -> None:
    toy.branch("feat-a", {"src/feat-a.txt": "a\n"})
    status, body, _err = run(toy, *args)
    assert status == 2 and body["reason"] == "train-retire-invalid"
    assert "feat-a" in heads(toy)


def test_a_queued_request_keeps_its_retire_flag(toy: Toy) -> None:
    from piceli.k8s.cli.train import build_request
    from piceli.train.queue import TrainQueue
    from piceli.train.state import TrainState

    toy.branch("feat-a", {"src/feat-a.txt": "a\n"})
    queue = TrainQueue(TrainState.for_train("toy", toy.root / "state"))
    request = queue.enqueue(
        entry=str(toy.repo / "train_spec.py"),
        cwd=str(toy.repo),
        branches=["feat-a"],
        promote=True,
        push=True,
        retire="remote",
    )
    (row,) = queue.pending()
    assert row["retire"] == "remote"
    lines = "\n".join(table(overview(toy.root / "state", ["toy"])))
    assert "--promote --push --retire-remote" in lines
    built = build_request(toy.root / "state")(request)
    assert built.retire_mode == "remote"
