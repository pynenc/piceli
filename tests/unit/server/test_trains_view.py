"""The UI's Trains view: ``piceli trains``'s document, read-only, local only."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from piceli.server.app import create_app
from piceli.services.query import QueryService
from piceli.services.trains import TrainsControl
from piceli.train.state import TrainState
from piceli.train.status import overview

RUN = "20261023T100000Z-abcdef"


def _state(root: Path) -> TrainState:
    state = TrainState(root / "shop")
    head = "a" * 40
    state.write_record(
        "20261022T090000Z-aaaaaa",
        {
            "run": "20261022T090000Z-aaaaaa", "train": "shop", "state": "joined", "ok": True,
            "started": "2026-10-22T09:00:00.000000Z", "duration": 90.0, "head": head,
            "branches": [{"branch": "feat-a", "verdict": "joined"}],
            "trials": [{"id": 1, "steps": [{"name": "unit", "kind": "command", "verdict": "passed", "duration": 80.0}]}],
            "promote": {"state": "promoted", "tag": "tested/2026-10-22.1", "head": head},
            "push": {"state": "pushed"},
        },
    )  # fmt: skip
    log = state.run_dir(RUN) / "01-unit.log"
    log.write_text("compiling\nrunning 3 tests\n")
    for event in (
        {"event": "run-started", "at": "2026-10-23T10:00:00.000000Z", "branches": ["feat-b"]},
        {"event": "trial-started", "at": "2026-10-23T10:00:01.000000Z", "trial": 1, "branches": ["feat-b"]},
        {"event": "step-started", "at": "2026-10-23T10:00:02.000000Z", "trial": 1, "step": "unit", "kind": "command", "host": "local", "log": str(log)},
    ):  # fmt: skip
        state.append_event(RUN, event)
    return state


def test_trains_page_data_is_the_cli_document(tmp_path: Path) -> None:
    state = _state(tmp_path)
    (tmp_path / "index.html").write_text("<!doctype html><title>Piceli</title>")
    targets = {
        "schema": "piceli.targets.v1", "generated_at": "2026-10-23T10:00:05Z",
        "controller": {"read": True},
        "targets": [{"kind": "environment", "name": "main", "state": "deployed", "revision": {"commit": "a" * 40, "tag": None}}],
    }  # fmt: skip
    app = create_app(
        QueryService([]),
        static_dir=tmp_path,
        trains_control=TrainsControl(tmp_path, targets=lambda: targets),
    )
    with state.lock(RUN), TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert client.get("/api/v1/trains").status_code == 403  # no session yet
        client.get(f"/?token={app.state.security.launch_token}")
        capabilities = client.get("/api/v1/capabilities").json()
        assert capabilities["actions"]["trains"]["allowed"] is True
        body = client.get("/api/v1/trains").json()
        cli = overview(tmp_path)
        joined = client.get("/api/v1/trains/deployed").json()
    assert body["schema"] == "piceli.trains.v1"
    (train,) = body["trains"]
    assert train["state"] == "running"
    step = train["current"]["step"]
    assert step["name"] == "unit" and "running 3 tests" in step["tail"]
    # Same functions as the CLI, without local paths or process ids.
    assert "log" not in step and "pid" not in train["current"]
    assert str(tmp_path) not in str(body)
    assert train["runs"] == cli["trains"][0]["runs"]
    assert train["trends"] == cli["trains"][0]["trends"]
    (row,) = joined["rows"]
    assert (
        row["train"]["run"] == "20261022T090000Z-aaaaaa" and row["matched"] == "commit"
    )


def test_without_trains_the_view_is_unavailable(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<!doctype html><title>Piceli</title>")
    app = create_app(QueryService([]), static_dir=tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        client.get(f"/?token={app.state.security.launch_token}")
        assert "trains" not in client.get("/api/v1/capabilities").json()["actions"]
        assert client.get("/api/v1/trains").status_code == 409
        assert client.get("/api/v1/trains/deployed").status_code == 409


def test_deployed_is_off_without_a_composition_and_survives_a_failing_read(
    tmp_path: Path,
) -> None:
    assert TrainsControl(tmp_path).deployed() == {"configured": False, "rows": []}

    def broken() -> dict:  # type: ignore[type-arg]
        raise OSError("unreachable")

    failing = TrainsControl(tmp_path, targets=broken).deployed()
    assert failing["configured"] is True and failing["rows"] == []
