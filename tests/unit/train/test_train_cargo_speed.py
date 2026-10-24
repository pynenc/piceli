"""Train speed with real cargo on the two-crate toy workspace: the shared
target directory keeps later trials warm, and a cargo step with inputs is
reused across the trials of a bisection. Skipped when cargo is not
installed."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli import app
from tests.train_toy import Toy, make_toy

pytestmark = pytest.mark.skipif(shutil.which("cargo") is None, reason="needs cargo")


@pytest.fixture
def toy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Toy:
    # No CARGO_TARGET_DIR: the train's own shared target directory is used.
    monkeypatch.delenv("CARGO_TARGET_DIR", raising=False)
    monkeypatch.delenv("RUSTC_WRAPPER", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
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


def step(body: dict[str, Any], branch: str, name: str) -> dict[str, Any]:
    found = next(b for b in body["branches"] if b["branch"] == branch)
    return next(s for s in found["steps"] if s["name"] == name)


@pytest.mark.timeout(300)
def test_a_second_run_builds_in_the_warm_shared_target(toy: Toy) -> None:
    toy.branch("one", {"crates/toyapp/src/extra.rs": "// one\n"})
    toy.branch("two", {"crates/toyapp/src/extra.rs": "// two\n"})
    status, first = run(toy, "one")
    assert status == 0, first
    check = step(first, "one", "cargo-check")
    assert "Checking toycore" in Path(check["log"]).read_text()
    target = next(
        note for note in check["detail"]["placement"] if "CARGO_TARGET_DIR=" in note
    ).split("=", 1)[1]
    assert Path(target).is_relative_to(toy.root / "cache" / "piceli" / "train")
    status, second = run(toy, "two")
    assert status == 0, second
    again = step(second, "two", "cargo-check")
    # Another fresh worktree, the same target: toycore is not checked again.
    assert "Checking toycore" not in Path(again["log"]).read_text()
    assert f"CARGO_TARGET_DIR={target}" in again["detail"]["placement"]


SPEC = """\
import sys

from piceli.train import CargoCheck, Step, Train

train = Train(
    "toy",
    base="main",
    tested="tested",
    steps=[
        CargoCheck(select="all", inputs=["crates/**", "Cargo.toml", "Cargo.lock"]),
        Step.command("fitness", [sys.executable, "scripts/fitness.py"]),
    ],
)
"""


@pytest.mark.timeout(300)
def test_a_cargo_step_with_inputs_is_reused_through_a_bisection(toy: Toy) -> None:
    toy.commit({"train_spec.py": SPEC}, "spec: cargo check keyed by its inputs")
    toy.branch("crate", {"crates/toyapp/src/extra.rs": "// more\n"})
    toy.branch("bad", {"src/bad.txt": "forbidden\n"})
    status, body = run(toy, "crate", "bad")
    assert status == 1
    assert {b["branch"]: b["verdict"] for b in body["branches"]} == {
        "crate": "joined",
        "bad": "ejected",
    }
    first = body["trials"][0]["steps"]
    checked = next(s for s in first if s["name"] == "cargo-check")
    assert checked["verdict"] == "passed" and checked["detail"]["stored"] == 0
    for branch in ("crate", "bad"):
        again = step(body, branch, "cargo-check")
        assert again["verdict"] == "reused", again
        assert again["detail"]["reused"]["trial"] == 1
        assert again["detail"]["key"] == checked["detail"]["key"]
