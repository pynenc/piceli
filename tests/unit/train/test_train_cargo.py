"""``piceli train`` with real cargo on a two-crate toy workspace.

One train over five branches: a passing change, a failing test, a flaky
test (fails once, passes on the retry), an old branch that the base's newer
rule rejects, and a branch whose own allowlist permits what the rule
forbids. Skipped when cargo is not installed.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli import app
from piceli.train.state import TrainState
from tests.train_toy import FAILING_TEST, FLAKY_TEST, Toy, make_toy

pytestmark = pytest.mark.skipif(shutil.which("cargo") is None, reason="needs cargo")


@pytest.fixture
def toy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Toy:
    # One target directory for every trial (each runs in a fresh worktree).
    monkeypatch.setenv("CARGO_TARGET_DIR", str(tmp_path / "target"))
    monkeypatch.setenv("TOY_FLAKY_MARKER", str(tmp_path / "flaky-seen"))
    monkeypatch.setenv("TOY_PUBLISH_OUT", str(tmp_path / "published"))
    return make_toy(tmp_path, cargo=True)


def run(toy: Toy, *branches: str) -> tuple[int, dict[str, Any]]:
    result = CliRunner().invoke(
        app,
        [
            "train", toy.spec, "--branches", *branches,
            "--json", "--state-dir", str(toy.root / "state"),
        ],
    )  # fmt: skip
    if result.exception and not isinstance(result.exception, SystemExit):
        raise result.exception
    return result.exit_code, json.loads(result.stdout.strip().splitlines()[-1])


def step(branch: dict[str, Any], name: str) -> dict[str, Any]:
    return next(s for s in branch["steps"] if s["name"] == name)


@pytest.mark.timeout(300)
def test_cargo_train_joins_ejects_records_the_flake_and_uses_each_commits_rules(
    toy: Toy,
) -> None:
    toy.branch(
        "old",
        {"crates/toyapp/src/notes.rs": "// a legacy shim\n"},
    )
    toy.commit({"rules/forbidden.txt": "forbidden\nlegacy\n"}, "rules: forbid legacy")
    toy.branch(
        "good",
        {
            "crates/toycore/src/lib.rs": (
                "pub fn value() -> i32 {\n    1\n}\n\npub fn three() -> i32 {\n    3\n}\n"
            )
        },
    )
    toy.branch("bad", {"crates/toyapp/tests/bad.rs": FAILING_TEST})
    toy.branch("flaky", {"crates/toyapp/tests/flaky.rs": FLAKY_TEST})
    toy.branch(
        "allowed",
        {
            "crates/toycore/src/compat.rs": "// legacy by design\n",
            "rules/allow.txt": "crates/toycore/src/compat.rs\n",
        },
    )
    status, body = run(toy, "good", "bad", "flaky", "old", "allowed")
    assert status == 1, body
    branches = {b["branch"]: b for b in body["branches"]}
    assert {name: b["verdict"] for name, b in branches.items()} == {
        "good": "joined",
        "bad": "ejected",
        "flaky": "joined",
        "old": "ejected",
        "allowed": "joined",
    }
    # bad: its test failed twice (first run and the retry by exact name).
    bad = branches["bad"]
    assert bad["reason"] == "train-step-failed" and bad["step"] == "cargo-test"
    tests = step(bad, "cargo-test")
    assert tests["detail"]["failed"] == [
        {"target": "-p toyapp --test bad", "tests": ["always_fails"]}
    ]
    assert "twice is not three" in bad["tail"]
    assert "--exact always_fails" in Path(tests["log"]).read_text()
    # old: the base's newer rule, read from the merged tree.
    assert branches["old"]["step"] == "fitness"
    assert "uses 'legacy'" in branches["old"]["tail"]
    # good: impact selected the changed crate and its dependent.
    assert step(branches["good"], "cargo-test")["detail"]["selection"] == [
        "toyapp",
        "toycore",
    ]
    # The flaky test failed once (in the first, joint trial) and is recorded once.
    (flake,) = body["flaky"]
    assert flake["key"] == "-p toyapp --test flaky sometimes"
    assert flake["count"] == 1
    assert "fails the first time only" in flake["last_tail"]
    assert body["trials"][0]["steps"][-1]["name"] == "cargo-test"
    assert body["trials"][0]["steps"][-1]["flaky"][0]["test"] == "sometimes"
    registry = TrainState.for_train("toy", toy.root / "state").flaky()
    assert registry[flake["key"]]["count"] == 1
    lines = (toy.root / "state" / "toy" / "flaky.jsonl").read_text().splitlines()
    assert len(lines) == 1


@pytest.mark.timeout(300)
def test_flaky_registry_counts_across_runs(toy: Toy) -> None:
    toy.branch("flaky", {"crates/toyapp/tests/flaky.rs": FLAKY_TEST})
    status, body = run(toy, "flaky")
    assert status == 0, body
    assert body["flaky"][0]["count"] == 1
    (toy.root / "flaky-seen").unlink()
    toy.branch(
        "flaky-again",
        {"crates/toyapp/tests/flaky.rs": FLAKY_TEST + "\n// touched\n"},
    )
    status, body = run(toy, "flaky-again")
    assert status == 0, body
    (flake,) = body["flaky"]
    assert flake["count"] == 2 and flake["first_seen"] < flake["last_seen"]
    history = TrainState.for_train("toy", toy.root / "state").history()
    assert [len(run["flaky"]) for run in history] == [1, 1]
