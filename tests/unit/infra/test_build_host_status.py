"""0.28.0: ``piceli builder status``: what each build host last did.

Read from the controller's status only: ``build_hosts`` (version, last seen,
last build), ``builds_wanted`` (live and expired claims) and
``build_health`` (quarantine). Declared hosts that never showed up are
listed too.
"""

from __future__ import annotations

from typing import Any

from piceli.infra import build_host_status


def _status() -> dict[str, Any]:
    return {
        "controller": {
            "build_hosts": {
                "builder-1": {
                    "version": "0.28.0",
                    "seen_at": "2027-01-15T07:58:00Z",
                    "seen": "build-progress",
                    "last_build": {
                        "id": "a" * 20,
                        "kind": "components",
                        "outcome": "claimed",
                        "at": "2027-01-15T07:50:00Z",
                    },
                },
                "builder-2": {
                    "version": "0.27.1",
                    "seen_at": "2027-01-15T06:00:00Z",
                    "seen": "build-claim",
                    "last_build": {
                        "id": "b" * 20,
                        "kind": "components",
                        "outcome": "claimed",
                        "at": "2027-01-15T06:00:00Z",
                    },
                },
                "builder-3": {
                    "version": "0.28.0",
                    "seen_at": "2027-01-14T20:00:00Z",
                    "seen": "built",
                    "last_build": {
                        "id": "c" * 20,
                        "kind": "components",
                        "outcome": "built",
                        "at": "2027-01-14T20:00:00Z",
                    },
                },
            },
            "builds_wanted": [
                {
                    "id": "a" * 20,
                    "kind": "components",
                    "images": ["web"],
                    "claimed": {"host": "builder-1", "at": "2027-01-15T07:50:00Z"},
                    "progress": {"stage": "build", "at": "2027-01-15T07:58:00Z"},
                },
                {
                    "id": "b" * 20,
                    "kind": "components",
                    "images": ["api"],
                    "claimed": None,
                    "claim_expired": {
                        "host": "builder-2",
                        "at": "2027-01-15T06:00:00Z",
                    },
                },
            ],
            "build_health": {
                "builder-3": {
                    "kind": "host",
                    "quarantined": True,
                    "since": "2027-01-14T21:00:00Z",
                    "reason": "3 segfaults in 24h",
                    "recent": 3,
                    "events": [{"at": "2027-01-14T21:00:00Z", "kind": "segfault"}],
                },
                "cluster": {"kind": "node", "quarantined": False, "events": []},
            },
        }
    }


def _rows(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["host"]: row for row in document["hosts"]}


def test_each_host_says_its_version_last_seen_claim_and_quarantine() -> None:
    document = build_host_status.view(
        _status(), ["builder-1", "builder-2", "builder-3", "builder-4"]
    )
    assert document["schema"] == "piceli.builder-status.v1"
    rows = _rows(document)
    assert list(rows) == ["builder-1", "builder-2", "builder-3", "builder-4"]

    live = rows["builder-1"]
    assert live["version"] == "0.28.0" and live["declared"] is True
    assert live["last_seen"] == "2027-01-15T07:58:00Z"
    assert live["claim"] == {
        "build": "a" * 20,
        "state": "live",
        "at": "2027-01-15T07:50:00Z",
        "stage": "build",
    }
    assert live["last_build"]["outcome"] == "claimed"
    assert live["quarantined"] is False and live["state"] == "building"

    expired = rows["builder-2"]
    assert expired["claim"]["state"] == "expired"
    assert expired["state"] == "claim-expired"

    sick = rows["builder-3"]
    assert sick["quarantined"] is True
    assert sick["quarantine"] == {
        "since": "2027-01-14T21:00:00Z",
        "reason": "3 segfaults in 24h",
    }
    assert sick["last_seen"] == "2027-01-14T21:00:00Z"  # its health record
    assert sick["last_build"]["outcome"] == "built"
    assert sick["state"] == "quarantined"

    never = rows["builder-4"]
    assert never["state"] == "never-seen" and never["last_seen"] is None
    assert never["version"] is None and never["claim"] is None
    assert document["never_seen"] == ["builder-4"]
    assert "cluster" not in rows  # the builder node is not a build host


def test_a_host_seen_only_through_a_claim_of_an_older_controller() -> None:
    status = _status()
    del status["controller"]["build_hosts"]
    rows = _rows(build_host_status.view(status, []))
    assert rows["builder-1"]["version"] is None
    assert rows["builder-1"]["last_seen"] == "2027-01-15T07:58:00Z"  # progress
    assert rows["builder-1"]["declared"] is False
    assert rows["builder-2"]["last_seen"] == "2027-01-15T06:00:00Z"


def test_an_idle_host_is_idle() -> None:
    status = {
        "controller": {
            "build_hosts": {
                "builder-1": {
                    "version": "0.28.0",
                    "seen_at": "2027-01-15T07:00:00Z",
                    "seen": "built",
                    "last_build": {"id": "a" * 20, "outcome": "built"},
                }
            }
        }
    }
    (row,) = build_host_status.view(status, ["builder-1"])["hosts"]
    assert row["state"] == "idle" and row["claim"] is None


def test_no_status_lists_the_declared_hosts_as_never_seen() -> None:
    document = build_host_status.view(None, ["builder-1"])
    assert document["never_seen"] == ["builder-1"]
