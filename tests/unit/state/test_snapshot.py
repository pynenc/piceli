"""Snapshots of a state directory: what travels, what stays, and safe unpacking."""

from __future__ import annotations

import io
import sqlite3
import stat
import tarfile
from pathlib import Path, PurePosixPath

import pytest

from piceli.pipeline import Pipeline, PipelineError
from piceli.state import StateSettings
from piceli.state.snapshot import (
    SnapshotError,
    entries,
    excluded,
    members,
    pack,
    private,
    unpack,
)


def _state(root: Path) -> Path:
    state = root / "state"
    (state / "runs").mkdir(parents=True)
    (state / "runs" / "one.json").write_text('{"run": 1}\n')
    (state / "release" / "releases").mkdir(parents=True)
    (state / "release" / "catalog.json").write_text("{}\n")
    (state / "release" / "releases" / "shop-1.discovery.json").write_text("{}\n")
    (state / "builds" / "shop" / "outputs").mkdir(parents=True)
    (state / "builds" / "shop" / "receipt.json").write_text("{}\n")
    (state / "builds" / "shop" / "build.log").write_text("log\n")
    (state / "builds" / "shop" / "outputs" / "image.tar").write_text("big\n")
    (state / "deploy.lock").write_text("")
    (state / "runs" / ".one.json.tmp123").write_text("partial")
    database = sqlite3.connect(state / "release" / "secrets.sqlite")
    database.execute("create table t (v text)")
    database.execute("insert into t values ('value')")
    database.commit()
    database.close()
    return state


def test_members_leave_local_files_out(tmp_path: Path) -> None:
    state = _state(tmp_path)
    assert [str(item) for item in members(state)] == [
        "builds/shop/receipt.json",
        "release/catalog.json",
        "release/releases/shop-1.discovery.json",
        "release/secrets.sqlite",
        "runs/one.json",
    ]
    assert excluded(PurePosixPath("release/journal.sqlite-journal"))
    assert private(PurePosixPath("release/secrets.sqlite"))
    assert private(PurePosixPath("registry/journal.sqlite"))
    assert private(PurePosixPath("release/backups/x/0001-Secret-a.json"))
    assert private(PurePosixPath("release/plans/abc.json"))
    assert not private(PurePosixPath("release/catalog.json"))
    assert not private(PurePosixPath("runs/one.json"))


def test_pack_is_deterministic_and_unpack_replaces(tmp_path: Path) -> None:
    state = _state(tmp_path)
    first, names = pack(state)
    second, _ = pack(state)
    assert first == second and len(names) == 5
    target = tmp_path / "other"
    (target / "runs").mkdir(parents=True)
    (target / "runs" / "stale.json").write_text("{}")
    (target / "deploy.lock").write_text("")
    unpack(first, target)
    assert [str(item) for item in members(target)] == names
    assert not (target / "runs" / "stale.json").exists()
    assert (target / "deploy.lock").exists()  # local-only files stay
    mode = stat.S_IMODE((target / "runs" / "one.json").stat().st_mode)
    assert mode == 0o600
    database = sqlite3.connect(target / "release" / "secrets.sqlite")
    assert database.execute("select v from t").fetchall() == [("value",)]
    database.close()


def test_snapshot_of_an_open_database_is_consistent(tmp_path: Path) -> None:
    state = _state(tmp_path)
    writer = sqlite3.connect(state / "release" / "secrets.sqlite")
    writer.execute("insert into t values ('committed')")
    writer.commit()
    writer.execute("begin")
    writer.execute("insert into t values ('uncommitted')")
    data, _ = pack(state)  # while a write transaction is open
    writer.rollback()
    writer.close()
    target = tmp_path / "restored"
    unpack(data, target)
    database = sqlite3.connect(target / "release" / "secrets.sqlite")
    assert sorted(database.execute("select v from t").fetchall()) == [
        ("committed",),
        ("value",),
    ]
    database.close()


@pytest.mark.parametrize("name", ["../escape.json", "/abs.json", "a/../../b.json"])
def test_unsafe_members_are_refused_before_anything_changes(
    tmp_path: Path, name: str
) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo(name)
        info.size = 2
        tar.addfile(info, io.BytesIO(b"{}"))
    target = _state(tmp_path)
    before = members(target)
    with pytest.raises(SnapshotError):
        unpack(buffer.getvalue(), target)
    assert members(target) == before
    assert not (tmp_path / "escape.json").exists()


def test_links_are_refused(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo("runs/link.json")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)
    with pytest.raises(SnapshotError):
        list(entries(buffer.getvalue()))
    state = _state(tmp_path)
    (state / "runs" / "link.json").symlink_to(state / "runs" / "one.json")
    with pytest.raises(SnapshotError):
        pack(state)


def test_not_an_archive_is_corrupt() -> None:
    with pytest.raises(SnapshotError) as raised:
        list(entries(b"not gzip"))
    assert raised.value.code == "state-corrupt"


def test_state_settings_are_validated(tmp_path: Path) -> None:
    assert StateSettings() == StateSettings("local", 60)
    with pytest.raises(ValueError):
        StateSettings("s3")
    with pytest.raises(ValueError):
        StateSettings("cluster", 2)
    from piceli import App, Target

    target = Target.kubeconfig("k", context="c", namespace="shop")
    app = App("shop")
    app.deployment("web", image="example.invalid/web@sha256:" + "a" * 64)
    with pytest.raises(PipelineError) as raised:
        Pipeline(app, target, state="object-store")
    assert raised.value.code == "pipeline-invalid"
    pipeline = Pipeline(app, target, state="cluster", state_lease_seconds=30)
    assert pipeline.state == StateSettings("cluster", 30)


def test_release_toml_cluster_state_keeps_every_store_in_state_dir() -> None:
    from piceli.k8s.release_spec import ReleaseSettings

    base = {
        "name": "shop",
        "owner": "shop",
        "field_manager": "shop",
        "composition": "app:compose",
        "state_dir": "state",
    }
    assert ReleaseSettings.model_validate({**base, "state": "cluster"}).state == (
        "cluster"
    )
    with pytest.raises(ValueError):
        ReleaseSettings.model_validate(
            {**base, "state": "cluster", "catalog": "elsewhere.json"}
        )
    with pytest.raises(ValueError):
        ReleaseSettings.model_validate({**base, "state": "object-store"})
