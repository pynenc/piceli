"""``piceli train SPEC --branches ... --compare "COMMAND"``: old and new side by side.

The old command is a made-up shell script that merges the branches in its
scratch checkout and runs the toy's unit check; the train runs the toy's
spec on the same branches. Nothing is promoted or pushed.
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli import app
from piceli.train.compare import count_tests, old_phases, render_table
from tests.train_toy import Toy, make_toy

OLD = (
    "export GIT_AUTHOR_NAME=old GIT_AUTHOR_EMAIL=old@example.invalid "
    "GIT_COMMITTER_NAME=old GIT_COMMITTER_EMAIL=old@example.invalid; "
    "set -e; "
    "for b in {branches}; do git merge -q --no-ff --no-edit $b; done; "
    'echo "==> unit"; '
    "PY scripts/unit.py; "
    'echo "test result: ok. 2 passed; 0 failed; 0 ignored"'
).replace("PY", shlex.quote(sys.executable))


@pytest.fixture
def toy(tmp_path: Path) -> Toy:
    return make_toy(tmp_path)


def compare(toy: Toy, command: str, *args: str) -> tuple[int, dict[str, Any], str]:
    result = CliRunner().invoke(
        app,
        [
            "train",
            toy.spec,
            *args,
            "--compare",
            command,
            "--json",
            "--state-dir",
            str(toy.root / "state"),
        ],
    )
    if result.exception and not isinstance(result.exception, SystemExit):
        raise result.exception
    lines = result.stdout.strip().splitlines()
    return result.exit_code, (json.loads(lines[-1]) if lines else {}), result.stderr


def test_both_pass_and_agree_nothing_moves(toy: Toy) -> None:
    toy.branch("feat-a", {"src/a.txt": "a\n"})
    toy.branch("feat-b", {"src/b.txt": "b\n"})
    main, remote = toy.rev("main"), toy.remote_refs()
    status, body, stderr = compare(toy, OLD, "--branches", "feat-a", "feat-b")
    assert status == 0, stderr
    assert body["schema"] == "piceli.train-compare.v1"
    assert body["branches"] == ["feat-a", "feat-b"]
    old, new = body["old"], body["train"]
    assert old["exit_code"] == 0 and old["verdict"] == "pass"
    assert old["tests"] == 2 and old["failed_tests"] == 0
    assert [p["name"] for p in old["phases"]] == ["setup", "unit"]
    assert new["verdict"] == "pass"
    assert new["failures"] == []
    assert {p["name"] for p in new["phases"]} >= {"unit", "fitness", "merge", "other"}
    assert new["trials"] == 1
    assert body["agree"] is True
    assert old["wall"] > 0 and new["wall"] > 0
    # The old command ran in a scratch checkout, removed afterwards.
    assert not Path(old["checkout"]).exists()
    assert toy.git("status", "--porcelain") == ""
    assert toy.git("rev-parse", "--abbrev-ref", "HEAD") == "main"
    # Never promoted, never pushed.
    assert toy.rev("main") == main and toy.rev("tested") is None
    assert toy.remote_refs() == remote
    assert "wall time" in stderr and "agree" in stderr and "yes" in stderr


def test_both_find_the_broken_branch(toy: Toy) -> None:
    toy.branch("good", {"src/good.txt": "good\n"})
    toy.branch("bad", {"src/bad.txt": "BROKEN\n"})
    status, body, stderr = compare(toy, OLD, "--branches", "good", "bad")
    assert status == 0, stderr
    assert body["old"]["verdict"] == "fail" and body["old"]["exit_code"] != 0
    assert body["train"]["verdict"] == "fail"
    assert body["train"]["failures"] == [
        {"branch": "bad", "step": "unit", "reason": "train-step-failed"}
    ]
    assert body["agree"] is True


def test_disagreement_exits_1(toy: Toy) -> None:
    toy.branch("bad", {"src/bad.txt": "BROKEN\n"})
    status, body, stderr = compare(toy, "true", "--branches", "bad")
    assert status == 1
    assert body["old"]["verdict"] == "pass" and body["train"]["verdict"] == "fail"
    assert body["agree"] is False
    assert "no" in stderr


def test_compare_refuses_promote_push_and_queue(toy: Toy) -> None:
    toy.branch("feat", {"src/f.txt": "f\n"})
    for flag in ("--promote", "--queue"):
        status, body, _ = compare(toy, "true", "--branches", "feat", flag)
        assert status == 2 and body["reason"] == "train-compare-invalid"


def test_parsers_count_tests_and_phases() -> None:
    text = (
        "test result: ok. 3 passed; 0 failed; 0 ignored\n"
        "test result: FAILED. 1 passed; 2 failed; 0 ignored\n"
        "===== 4 passed, 1 failed in 0.20s =====\n"
        "Tests:       1 failed, 5 passed, 6 total\n"
    )
    assert count_tests(text) == (17, 4)
    lines = [
        (0.0, "starting"),
        (1.0, "+ cargo test --offline"),
        (4.0, "test result: ok. 3 passed"),
        (5.0, "==> deploy"),
        (6.0, "done"),
    ]
    phases = old_phases(lines, end=7.0)
    assert [(p["name"], p["duration"]) for p in phases] == [
        ("setup", 1.0),
        ("cargo-test", 4.0),
        ("deploy", 2.0),
    ]


def test_table_puts_matching_phases_on_one_row() -> None:
    body = {
        "branches": ["a"],
        "agree": True,
        "old": {
            "wall": 300.0,
            "verdict": "pass",
            "exit_code": 0,
            "tests": 10,
            "failed_tests": 0,
            "retries": 1,
            "phases": [{"name": "cargo-test", "duration": 250.0}],
        },
        "train": {
            "wall": 60.0,
            "verdict": "pass",
            "tests": 10,
            "failed_tests": 0,
            "retries": 0,
            "failures": [],
            "trials": 1,
            "phases": [
                {"name": "other", "duration": 2.0},
                {"name": "cargo-test", "duration": 50.0},
            ],
        },
    }
    table = render_table(body)
    rows = {line.split("  ")[0].strip(): line for line in table.splitlines()}
    assert "300.0 s" in rows["wall time"] and "60.0 s" in rows["wall time"]
    assert "250.0 s" in rows["cargo-test"] and "50.0 s" in rows["cargo-test"]
    assert rows["agree"].rstrip().endswith("yes")
