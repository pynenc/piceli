"""0.28.0: a train run records its cycle (``piceli.cycle``) in ``run.json``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.train_toy import Toy, make_toy
from tests.unit.train.test_train_run import good, train


@pytest.fixture
def toy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Toy:
    monkeypatch.setenv("TOY_PUBLISH_OUT", str(tmp_path / "published"))
    return make_toy(tmp_path)


def test_a_promoted_run_records_its_phases(toy: Toy) -> None:
    good(toy, "feat-a")
    status, body, _ = train(toy, "--branches", "feat-a", "--promote")
    assert status == 0, body
    found = body["cycle"]
    assert found["kind"] == "train" and found["ok"] and found["commit"] == body["head"]
    assert set(found["phases"]) >= {
        "merging", "testing unit", "testing fitness", "publishing", "promoting",
    }  # fmt: skip
    assert all(seconds >= 0 for seconds in found["phases"].values())
    assert found["seconds"] == pytest.approx(body["duration"], abs=1.0)
    assert found["seen_at"] == body["started"]  # not queued: from its start
    record = json.loads(Path(body["record"]).read_text())
    assert record["cycle"] == found


def test_a_failing_run_is_a_failed_cycle(toy: Toy) -> None:
    toy.branch("broken", {"src/broken.txt": "BROKEN\n"})
    status, body, _ = train(toy, "--branches", "broken")
    assert status == 1
    assert body["cycle"]["ok"] is False and body["cycle"]["seconds"] is None
