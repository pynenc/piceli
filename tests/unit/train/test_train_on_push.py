"""``CargoTests(on_push="all")``: the impact selection for runs that do not
push, every package for a run with ``--push``."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from piceli.train import CargoCheck, CargoTests, Impact, Train, TrainError
from piceli.train.engine import TrainRun, locate
from piceli.train.runner import Command, Completed, LocalRunner
from piceli.train.state import TrainState
from tests.train_toy import Toy, make_toy

SPEC = """\
from piceli.train import CargoTests, Impact, Step, Train

train = Train(
    "toy",
    steps=[
        CargoTests(select=Impact(outside="none"), on_push="all"),
        Step.publish("publish", ["true"]),
    ],
)
"""


class FakeCargo:
    """Answers ``cargo metadata`` with one member, records ``cargo test``,
    and runs everything else here."""

    host = "local"

    def __init__(self) -> None:
        self.tests: list[list[str]] = []
        self.local = LocalRunner()

    def run(self, command: Command) -> Completed:
        argv = list(command.argv)
        if argv[:2] == ["cargo", "metadata"]:
            root = str(command.cwd)
            member = {
                "id": "core-id",
                "name": "core",
                "version": "0.1.0",
                "manifest_path": f"{root}/crates/core/Cargo.toml",
                "dependencies": [],
            }
            body = {
                "workspace_root": root,
                "workspace_members": ["core-id"],
                "packages": [member],
            }
            return Completed(0, 0.0, 0.0, "local", json.dumps(body))
        if argv[:2] == ["cargo", "test"]:
            self.tests.append(argv)
            return Completed(0, 0.0, 0.0, "local", "")
        return self.local.run(command)


@pytest.fixture
def toy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Toy:
    for name in list(os.environ):
        if name.startswith("OTEL_"):
            monkeypatch.delenv(name)
    toy = make_toy(tmp_path)
    toy.commit({"train_spec.py": SPEC}, "cargo tests, full on push")
    toy.git("push", "-q", "origin", "main")
    return toy


def _run(toy: Toy, branch: str, **flags: Any) -> tuple[dict[str, Any], FakeCargo]:
    toy.branch(branch, {f"src/{branch}.txt": f"{branch}\n"})
    loaded, repo, spec_path = locate(str(toy.repo / "train_spec.py"), toy.repo)
    fake = FakeCargo()
    run = TrainRun(
        loaded,
        repo=repo,
        spec_path=spec_path,
        branches=[branch],
        state=TrainState.for_train("toy", toy.root / "state"),
        runner=fake,
        observe=False,
        **flags,
    )
    return run.run(), fake


def _cargo_step(result: dict[str, Any]) -> dict[str, Any]:
    (step,) = [s for s in result["trials"][0]["steps"] if s["name"] == "cargo-test"]
    return dict(step)


def test_a_run_that_pushes_tests_every_package(toy: Toy) -> None:
    result, fake = _run(toy, "feat-a")
    step = _cargo_step(result)
    assert step["verdict"] == "skipped" and step["detail"]["selection"] == []
    assert not fake.tests
    result, fake = _run(toy, "feat-b", promote=True, push=True)
    step = _cargo_step(result)
    assert step["verdict"] == "passed" and step["detail"]["selection"] == "all"
    assert fake.tests and "--workspace" in fake.tests[0]
    assert "on_push" in Path(step["log"]).read_text()


def test_on_push_is_declared_and_round_trips() -> None:
    for step in (CargoTests(on_push="all"), CargoCheck(on_push="all")):
        train = Train("t", steps=[step])
        assert Train.from_dict(json.loads(json.dumps(train.to_dict()))) == train
    assert CargoTests().on_push is None
    assert "on_push" not in CargoTests().to_dict()
    with pytest.raises(TrainError, match="on_push"):
        CargoTests(on_push="some")  # type: ignore[arg-type]
    with pytest.raises(TrainError, match="on_push"):
        CargoTests(select="all", on_push="all")
    assert CargoTests(select=Impact(), on_push="all").select == Impact()
