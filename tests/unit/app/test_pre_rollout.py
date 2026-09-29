"""Declaring pre-rollout checks on an App (slice a)."""

from __future__ import annotations

import pytest

from piceli import App, ClaimTemplate, ExistingClaim, UpgradeCheck


def _app() -> App:
    return App("shop")


def test_a_declaration_renders_nothing_and_keeps_the_plan_hash() -> None:
    plain, declared = _app(), _app()
    for app in (plain, declared):
        app.stateful_set(
            "db",
            image="example/db:1",
            volumes={"/var/lib/db": ClaimTemplate("data", size="1Gi")},
        )
    db = declared.objects[0]
    declared.pre_rollout(
        db, ["db", "check"], upgrade=UpgradeCheck(["db", "verify", "/var/lib/db"])
    )
    assert declared.render("shop") == plain.render("shop")
    assert declared.pre_rollouts[0].describe() == {
        "workload": "db",
        "check": {"command": ["db", "check"], "timeout_seconds": 300},
        "upgrade": {
            "command": ["db", "verify", "/var/lib/db"],
            "volumes": [],
            "timeout_seconds": 300,
        },
    }


def test_refusals_name_the_problem() -> None:
    app = _app()
    api = app.deployment("api", image="example/api:1")
    job = app.job("migrate", image="example/api:1")
    with pytest.raises(ValueError, match="only a Deployment, StatefulSet"):
        app.pre_rollout(job, ["true"])
    with pytest.raises(ValueError, match="needs a command"):
        app.pre_rollout(api)
    with pytest.raises(ValueError, match="mounts no retained claim"):
        app.pre_rollout(api, upgrade=UpgradeCheck(["true"]))
    with pytest.raises(ValueError, match="declared on this app"):
        app.pre_rollout(_app().deployment("api", image="example/api:1"), ["true"])
    app.pre_rollout(api, ["true"])
    with pytest.raises(ValueError, match="already has a pre-rollout"):
        app.pre_rollout(api, ["true"])


def test_upgrade_volumes_must_be_retained_claim_mounts() -> None:
    app = _app()
    db = app.deployment(
        "db",
        image="example/db:1",
        volumes={"/data": ExistingClaim("db-data"), "/tmp": ExistingClaim("scratch")},
    )
    with pytest.raises(ValueError, match=r"\['/other'\] are not retained"):
        app.pre_rollout(db, upgrade=UpgradeCheck(["true"], volumes=["/other"]))
    check = app.pre_rollout(db, upgrade=UpgradeCheck(["true"], volumes=["/data"]))
    assert check.upgrade is not None and check.upgrade.volumes == ("/data",)


def test_a_command_is_an_argv_not_a_string() -> None:
    app = _app()
    api = app.deployment("api", image="example/api:1")
    with pytest.raises(ValueError, match="argv list"):
        app.pre_rollout(api, "check --all")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        app.pre_rollout(api, ["true"], timeout_seconds=0)


def test_environments_keep_the_checks_of_the_workloads_they_keep() -> None:
    from piceli import Environment

    app = _app()
    api = app.deployment("api", image="example/api:1")
    app.pre_rollout(api, ["true"])
    app.environment(Environment("dev"))
    assert [item.workload for item in app.for_environment("dev").pre_rollouts] == [
        "api"
    ]
