"""Focused provider-free release catalog tests."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from piceli.k8s.ops.session import DeploymentSessionArchive
from piceli.k8s.release import (
    ReleaseCatalog,
    ReleaseRecord,
    ReleaseSource,
    load_release_input,
    source_closure,
)


def archive() -> DeploymentSessionArchive:
    return DeploymentSessionArchive.from_json(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": "a" * 32,
                "private_inputs": [],
                "composition": [],
                "revision": {},
                "bundle": {},
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def record(name: str = "branch-a") -> ReleaseRecord:
    return ReleaseRecord(
        name=name,
        namespace="app-preview-branch-a",
        source=ReleaseSource(
            "dirty",
            "sha256:" + "1" * 64,
            dirty_closure_sha256="sha256:" + "1" * 64,
            artifact_digest="sha256:" + "2" * 64,
        ),
        archive=archive(),
        ttl_seconds=3600,
    )


def test_catalog_is_atomic_selectable_and_immutable(tmp_path: Path) -> None:
    catalog = ReleaseCatalog(tmp_path / "operator" / "releases.json")
    created = catalog.add(record())
    assert catalog.selected().release_id == created.release_id
    assert catalog.select("branch-a").source.kind == "dirty"
    assert (tmp_path / "operator" / "releases.json").stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="another immutable record"):
        catalog.add(
            ReleaseRecord(
                name="branch-a",
                namespace="app-preview-branch-a",
                source=ReleaseSource(
                    "oci", "sha256:" + "3" * 64, artifact_digest="sha256:" + "3" * 64
                ),
                archive=archive(),
                ttl_seconds=3600,
            )
        )


def test_source_closure_distinguishes_clean_git_and_private_dirty_content(
    tmp_path: Path,
) -> None:
    root = tmp_path / "source"
    root.mkdir()

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "piceli@example.invalid")
    git("config", "user.name", "Piceli Test")
    tracked = root / "deployment.py"
    tracked.write_text("VERSION = 1\n")
    git("add", "deployment.py")
    git("commit", "-qm", "initial")

    clean = source_closure(root)
    assert clean.kind == "git"
    assert len(clean.identity) == 40

    tracked.write_text("VERSION = 2\n")
    private = root / "private.py"
    private.write_text("LOCAL = True\n")
    dirty = source_closure(root)
    assert dirty.kind == "dirty"
    assert dirty.identity == dirty.dirty_closure_sha256
    assert dirty.identity == source_closure(root).identity

    private.write_text("LOCAL = False\n")
    assert source_closure(root).identity != dirty.identity


def test_retained_pvcs_cannot_be_expiry_cleanup() -> None:
    with pytest.raises(ValueError, match="retained PVC"):
        ReleaseRecord(
            name="bad",
            namespace="app-preview-bad",
            source=record().source,
            archive=archive(),
            ttl_seconds=1,
            retained_pvc_policy="delete-on-expiry",
        )


def test_json_and_yaml_use_the_same_safe_object_shape() -> None:
    expected = {"source": {"kind": "oci"}}
    assert load_release_input(json.dumps(expected)) == expected
    assert load_release_input("source:\n  kind: oci\n") == expected


def test_catalog_reads_do_not_write_or_select(tmp_path: Path) -> None:
    path = tmp_path / "uncreated" / "releases.json"
    catalog = ReleaseCatalog(path)
    assert catalog.records() == ()
    assert not path.parent.exists()
    catalog.add(record("first"))
    catalog.add(record("second"))
    before = path.read_bytes()
    modified = path.stat().st_mtime_ns
    assert catalog.get("first").name == "first"
    assert catalog.selected().name == "second"
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == modified
