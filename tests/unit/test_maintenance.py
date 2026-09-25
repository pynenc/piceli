"""``piceli cache status/prune``, ``piceli doctor`` and ``piceli runs`` on
synthetic state directories (no cluster, no Docker)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli import app as cli
from piceli.maintenance.cache import (
    CacheError,
    PruneOptions,
    StateDir,
    category,
    parse_size,
    prune,
    status,
)
from piceli.maintenance.doctor import diagnose
from piceli.state.backend import directory_lock
from piceli.state.errors import StateError

GIB = 1024**3
HOUR = 3600.0


def config(n: int) -> str:
    return "sha256:" + f"{n:x}" * 64


def write(path: Path, text: str | bytes = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text)
    return path


def add_run(
    state: Path,
    index: int,
    *,
    run_state: str = "ready",
    release: str | None = None,
    image: int | None = None,
) -> str:
    run_id = f"2026010{index}T000000000000Z-{index:08x}"
    stages: dict[str, Any] = {
        name: {"state": "done"}
        for name in ("inputs", "build", "deliver", "plan", "apply", "checks")
    }
    if release is not None:
        stages["apply"]["output"] = {"release": release}
    if image is not None:
        stages["deliver"]["output"] = {
            "images": {"web": {"config_digest": config(image), "action": "pushed"}}
        }
    write(
        state / "runs" / f"{run_id}.json",
        json.dumps(
            {
                "schema": "piceli.deploy-run.v1",
                "run_id": run_id,
                "state": run_state,
                "created_at": f"2026-01-0{index}T00:00:00Z",
                "until": "checks",
                "combined_hash": "0" * 64,
                "stages": stages,
            }
        ),
    )
    write(state / "runs" / run_id / "summary.md", "# summary\n")
    return run_id


def delivery(state: Path, image: int) -> Path:
    return write(
        state / "deliveries" / f"web-{config(image)[7:]}.json",
        json.dumps({"approved_digest": config(image), "state": "succeeded"}),
    )


def make_state(root: Path) -> Path:
    """Six runs of three releases; one delivery per run; a release state."""
    state = root / "state"
    for name in ("catalog.json", "secrets.sqlite", "journal.sqlite", "history.json"):
        write(state / "release" / name, "keep")
    write(state / "release" / "plans" / ("a" * 64 + ".json"), "{}")
    write(state / "release" / "backups" / "Deployment-web.json", "{}")
    write(state / "mirrors" / "m.json", "{}")
    write(
        state / "builds" / "shop" / "receipt.json",
        json.dumps({"outputs": {"images": {"web": {"image_id": config(6)}}}}),
    )
    write(state / "builds" / "shop" / "build.log", b"l" * 4096)
    write(state / "builds" / "shop" / "outputs" / "app.bin", b"o" * 8192)
    for index, release in enumerate(("r1", "r1", "r2", "r2", "r3", "r3"), start=1):
        add_run(state, index, release=release, image=index)
        delivery(state, index)
    return state


def names(report: dict[str, Any]) -> list[str]:
    return [entry["path"] for entry in report["state_dirs"][0]["removed"]]


def test_parse_size() -> None:
    assert parse_size("20GiB") == 20 * GIB
    assert parse_size("20G") == 20 * GIB
    assert parse_size("500MB") == 500_000_000
    assert parse_size("1.5 KiB") == 1536
    assert parse_size(4096) == 4096
    for bad in ("", "lots", "-1G", "0", "12XB", True):
        with pytest.raises(CacheError) as error:
            parse_size(bad)  # type: ignore[arg-type]
        assert error.value.code == "cache-budget-invalid"


def test_categories() -> None:
    from pathlib import PurePosixPath as P

    assert category(P("builds/shop/outputs/app.bin")) == "builds"
    assert category(P("builds/shop/build.log")) == "builds"
    assert category(P("builds/shop/receipt.json")) == "receipts"
    assert category(P("deliveries/web-1.json")) == "receipts"
    assert category(P("runs/x.json")) == "runs"
    assert category(P("release/secrets.sqlite")) == "release"
    assert category(P("registry/catalog.json")) == "release"
    assert category(P("toolchains/cargo/target/a")) == "toolchains"
    assert category(P("blobs/sha256/ab")) == "blobs"
    assert category(P("release/.catalog.json.ab12cd34")) == "temp"
    assert category(P(".piceli-state.json")) == "other"
    assert category(P("deploy.lock")) == "other"


@pytest.fixture
def temp(tmp_path: Path) -> Any:
    """A temporary directory for piceli-* entries (emptied afterwards: the
    session guard fails on any left behind)."""
    import shutil

    folder = tmp_path / "tmp"
    folder.mkdir()
    yield folder
    shutil.rmtree(folder)


def test_status_reports_categories_runs_and_temp(tmp_path: Path, temp: Path) -> None:
    state = make_state(tmp_path)
    write(temp / "piceli-build-abcd1234" / "f", b"t" * 100)
    write(temp / "unrelated" / "f", b"u")
    report = status([StateDir(state)], keep_last=2, temp_dir=temp)
    [item] = report["state_dirs"]
    assert item["exists"] and item["runs"] == {"count": 6, "resumable": 0}
    categories = item["categories"]
    assert categories["builds"] == {"bytes": 4096 + 8192, "files": 2}
    assert categories["release"]["files"] == 6
    assert categories["receipts"]["files"] == 1 + 6 + 1  # build, deliveries, mirror
    assert item["bytes"] == sum(value["bytes"] for value in categories.values())
    assert item["reclaimable_bytes"] > 0
    assert report["temp"]["entries"] == 1 and report["temp"]["bytes"] == 100
    assert report["temp"]["stale"] == 0  # fresh: may belong to a running build
    assert report["total_bytes"] == item["bytes"] + 100


def test_prune_keeps_what_rollback_resume_and_release_need(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    before = sorted(p.relative_to(state).as_posix() for p in state.rglob("*"))
    report = prune([StateDir(state)], PruneOptions(keep_last=2), temp_dir=tmp_path)
    removed = names(report)
    # Runs 1 and 2 (release r1) are beyond the last 2 releases (r3, r2).
    runs = sorted(p.stem for p in (state / "runs").glob("*.json"))
    assert [run[-1] for run in runs] == ["4", "5", "6"]
    assert sorted(p.name[-1] for p in (state / "runs").iterdir() if p.is_dir()) == [
        "4",
        "5",
        "6",
    ]
    assert not any(path.startswith("release") for path in removed)
    for kept in (
        "release/secrets.sqlite",
        "release/catalog.json",
        "release/journal.sqlite",
        "release/plans/" + "a" * 64 + ".json",
        "release/backups/Deployment-web.json",
        "mirrors/m.json",
        "builds/shop/receipt.json",
        "builds/shop/outputs/app.bin",
        "builds/shop/build.log",
    ):
        assert (state / kept).exists(), kept
    # Deliveries of removed runs go; the current build's (6) and the kept
    # runs' stay.
    left = sorted(
        p.stem.rpartition("-")[2][:1] for p in (state / "deliveries").iterdir()
    )
    assert "6" in left and "1" not in left and "2" not in left
    assert report["state"] == "pruned" and report["within_budget"]
    assert report["freed_bytes"] > 0
    after = {p.relative_to(state).as_posix() for p in state.rglob("*")} - {
        "deploy.lock"
    }
    assert after < set(before)


def test_prune_never_removes_the_latest_or_a_resumable_run(tmp_path: Path) -> None:
    state = tmp_path / "state"
    resumable = add_run(state, 1, run_state="failed")
    for index in (2, 3, 4):
        add_run(state, index)
    latest = add_run(state, 5, run_state="stopped")
    prune([StateDir(state)], PruneOptions(keep_last=1), temp_dir=tmp_path)
    left = sorted(p.stem for p in (state / "runs").glob("*.json"))
    assert left == [resumable, latest]


def test_dry_run_removes_nothing(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    before = sorted(state.rglob("*"))
    report = prune(
        [StateDir(state)],
        PruneOptions(keep_last=1, budget=1, dry_run=True),
        temp_dir=tmp_path,
    )
    assert report["state"] == "planned" and names(report)
    assert sorted(state.rglob("*")) == before


def test_budget_drops_build_outputs_then_old_runs(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    size = sum(p.stat().st_size for p in state.rglob("*") if p.is_file())
    report = prune(
        [StateDir(state)],
        PruneOptions(keep_last=10, budget=size - 8192),
        temp_dir=tmp_path,
    )
    assert names(report) == ["builds/shop/outputs"]
    assert report["within_budget"]
    report = prune(
        [StateDir(state)], PruneOptions(keep_last=10, budget=1), temp_dir=tmp_path
    )
    assert not report["within_budget"]  # the release state is never pruned
    assert (state / "release" / "secrets.sqlite").exists()
    assert (state / "builds" / "shop" / "receipt.json").exists()
    # Only the latest run of each of the last 10 releases (and the latest) stay.
    left = sorted(p.stem[-1] for p in (state / "runs").glob("*.json"))
    assert left == ["2", "4", "6"]


def test_stale_temporary_directories_and_partial_files(
    tmp_path: Path, temp: Path
) -> None:
    state = make_state(tmp_path)
    old = time.time() - 7 * HOUR
    stale = write(temp / "piceli-tls-abcd1234" / "tls.key", "k").parent
    fresh = write(temp / "piceli-build-efgh5678" / "f", "f").parent
    foreign = write(temp / "someone-else" / "f", "f").parent
    partial = write(state / "release" / ".catalog.json.ab12cd34", "half")
    recent = write(state / "runs" / ".x.json.cd34ef56", "half")
    gitignore = write(state / ".gitignore", "*")
    for path in (stale, stale / "tls.key", foreign, partial, gitignore):
        os.utime(path, (old, old))
    report = prune([StateDir(state)], PruneOptions(), temp_dir=temp)
    assert [entry["name"] for entry in report["temp"]["removed"]] == [stale.name]
    assert not stale.exists() and fresh.exists() and foreign.exists()
    assert not partial.exists() and recent.exists() and gitignore.exists()
    assert "release/.catalog.json.ab12cd34" in names(report)


def test_shared_state_prunes_only_machine_local_files(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    runs = sorted((state / "runs").glob("*.json"))
    report = prune(
        [StateDir(state, backend="cluster")],
        PruneOptions(keep_last=1, budget=1),
        temp_dir=tmp_path,
    )
    assert sorted((state / "runs").glob("*.json")) == runs
    assert len(list((state / "deliveries").iterdir())) == 6
    assert not (state / "builds" / "shop" / "outputs").exists()
    assert "only machine-local" in report["state_dirs"][0]["note"]


def test_prune_is_refused_while_a_run_holds_the_state(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    with directory_lock(state), pytest.raises(StateError) as error:
        prune([StateDir(state)], temp_dir=tmp_path)
    assert error.value.code == "pipeline-locked"


# ------------------------------------------------------------------ doctor


def test_doctor_warns_on_low_disk_memory_and_missing_tools(tmp_path: Path) -> None:
    state = tmp_path / "state"
    write(
        state / "builds" / "shop" / "receipt.json",
        json.dumps(
            {
                "outputs": {
                    "images": {"web": {"image_id": config(1), "size_bytes": 3 * GIB}}
                }
            }
        ),
    )
    ran: list[list[str]] = []

    def run(argv: list[str]) -> bool:
        ran.append(argv)
        return argv[:2] != ["docker", "buildx"]

    report = diagnose(
        [StateDir(state)],
        temp_dir=tmp_path,
        run=run,
        which=lambda name: None if name == "kubectl" else f"/usr/bin/{name}",
        memory=lambda: GIB,
        disk=lambda _path: 5 * GIB,
    )
    assert report["state"] == "warnings"
    assert report["needs"]["disk_bytes"] == 2 * 3 * GIB + GIB
    assert report["needs"]["basis"] == "1 build receipt(s)"
    checks = {item["check"]: item for item in report["checks"]}
    assert checks["disk:temp"]["reason"] == "runner-disk-low"
    assert checks["memory"]["reason"] == "runner-memory-low"
    assert checks["tool:docker"]["status"] == "ok"
    assert checks["tool:docker buildx"]["reason"] == "runner-tool-missing"
    assert checks["tool:kubectl"]["reason"] == "runner-tool-missing"
    assert ["kubectl", "version", "--client=true", "--output=json"] not in ran
    assert report["reason"] == "runner-disk-low"


def test_doctor_ok_and_unknown_memory(tmp_path: Path) -> None:
    report = diagnose(
        [StateDir(tmp_path / "missing")],
        temp_dir=tmp_path,
        run=lambda _argv: True,
        which=lambda name: f"/usr/bin/{name}",
        memory=lambda: None,
        disk=lambda _path: 100 * GIB,
    )
    assert report["state"] == "ok" and "reason" not in report
    checks = {item["check"]: item for item in report["checks"]}
    assert checks["memory"]["status"] == "unknown"
    assert report["needs"]["basis"] == "default"


# --------------------------------------------------------------------- CLI


def invoke(*args: str) -> tuple[int, dict[str, Any], str]:
    result = CliRunner().invoke(cli, list(args))
    body = json.loads(result.stdout) if result.stdout.strip() else {}
    return result.exit_code, body, result.stderr


def test_cli_cache_status_prune_and_runs(tmp_path: Path) -> None:
    state = make_state(tmp_path)
    code, body, stderr = invoke("cache", "status", "--state-dir", str(state), "--json")
    assert code == 0, stderr
    assert body["schema"] == "piceli.cache-status.v1"
    assert body["state_dirs"][0]["path"] == str(state)
    assert "builds" in stderr and "temporary directories" in stderr

    code, body, stderr = invoke(
        "cache", "prune", "--state-dir", str(state), "--keep-last", "2", "--dry-run"
    )
    assert code == 0, stderr
    assert body["state"] == "planned" and body["dry_run"] is True
    assert "would free" in stderr
    assert len(list((state / "runs").glob("*.json"))) == 6

    code, body, stderr = invoke(
        "cache", "prune", "--state-dir", str(state), "--budget", "1"
    )
    assert code == 1
    assert body["state"] == "failed" and body["reason"] == "cache-over-budget"

    code, body, stderr = invoke(
        "cache", "prune", "--state-dir", str(state), "--budget", "lots"
    )
    assert code == 2 and body["reason"] == "cache-budget-invalid"

    code, body, stderr = invoke("runs", "--state-dir", str(state), "--json")
    assert code == 0 and body["schema"] == "piceli.runs.v1"
    assert [entry["run_id"] for entry in body["runs"]] == sorted(
        (entry["run_id"] for entry in body["runs"]), reverse=True
    )

    code, body, _ = invoke("runs", "--env", "prod", "--state-dir", str(state))
    assert code == 2 and body["reason"] == "cache-arguments-conflict"


def test_cli_doctor_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import piceli.maintenance.doctor as doctor

    monkeypatch.setattr(doctor, "_run", lambda _argv: True)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(doctor, "available_memory", lambda: 64 * GIB)
    code, body, stderr = invoke("doctor", "--state-dir", str(tmp_path), "--json")
    free = {item["check"]: item for item in body["checks"]}["disk:temp"]
    expected = 0 if free["status"] == "ok" else 1
    assert code == expected, stderr
    assert body["schema"] == "piceli.doctor.v1"
    monkeypatch.setattr(doctor, "available_memory", lambda: 1)
    code, body, stderr = invoke("doctor", "--state-dir", str(tmp_path), "--json")
    assert code == 1 and "runner-memory-low" in json.dumps(body)
    assert "piceli explain" in stderr
