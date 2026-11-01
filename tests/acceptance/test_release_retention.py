"""Acceptance: release retention after apply, its dry run, and a rollback to a
kept release (fake API server)."""

from __future__ import annotations

import json

from tests.acceptance.test_release_diff import (  # noqa: F401 - fixture
    _receipt,
    _run,
    release_env,
)

DIGESTS = ["sha256:" + str(n) * 64 for n in range(1, 5)]


def test_apply_retires_old_releases_and_kept_ones_roll_back(release_env) -> None:  # noqa: F811
    api, tmp_path = release_env
    spec = tmp_path / "release.toml"
    spec.write_text(
        spec.read_text().replace(
            'state_dir = "state"', 'state_dir = "state"\nkeep_releases = 2'
        )
    )
    names = []
    for digest in DIGESTS:
        _receipt(tmp_path / "build.receipt.json", digest)
        code, outcome, result = _run(tmp_path, "apply", "--auto-approve")
        assert code == 0, result.output
        names.append(outcome["release"])
        assert outcome["retention"]["state"] in {"done", "deferred"}
    catalog = json.loads((tmp_path / "state" / "catalog.json").read_text())
    kept = [item["name"] for item in catalog["releases"]]
    assert sorted(kept) == sorted(names[-2:])
    assert not (tmp_path / "state" / "releases" / f"{names[0]}.json").exists()

    code, report, result = _run(tmp_path, "retention", "--dry-run")
    assert code == 0, result.output
    assert report["state"] == "dry-run" and report["retired"] == []
    assert report["kept"] == names[-2:]

    code, report, result = _run(tmp_path, "retention", "--dry-run", "--keep", "1")
    assert code == 2  # below 2 refused

    code, outcome, result = _run(tmp_path, "rollback", "previous", "--auto-approve")
    assert code == 0, result.output
    assert outcome["release"] == names[-2]
    assert outcome["release_state"] == "ready"

    code, outcome, result = _run(tmp_path, "rollback", names[0], "--auto-approve")
    assert code == 2 and outcome["reason"] == "unknown-release"
    assert "retired" in outcome["message"]
