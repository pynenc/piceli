"""Acceptance: every typed App kind through ``piceli release`` against the fake API.

StatefulSet (claim templates, headless Service), DaemonSet, Job, CronJob,
HorizontalPodAutoscaler, PodDisruptionBudget, Ingress and Gateway API
HTTPRoute are planned, applied, re-planned as no-ops, pruned and rolled back;
a changed Job is refused until it is named with ``--replace``; an autoscaled
Deployment never has its replica count removed or reset.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli.release import app
from piceli.testing import TARGET, FakeAPI, serve, write_kubeconfig

DIGEST = "sha256:" + "5" * 64
OWNER = "kinds-shop"
NS = TARGET.namespace

COMPOSITION = """
from piceli import App, ClaimTemplate, PodDefaults, Resources, Route, Security

FULL = {full}
JOB_TAG = {job_tag!r}
AUTOSCALE = {autoscale}


def build(ctx):
    shop = App("shop", pod_defaults=PodDefaults(
        security=Security.restricted(user=10001),
        automount_token=False,
    ))
    image = ctx.image("api")
    api = shop.deployment(
        "api", image=image, ports=[8080], resources=Resources(cpu="100m"),
        **({{}} if AUTOSCALE else {{"replicas": 2}}),
    )
    web = shop.service(api, port=8080)
    migrate = shop.job(
        "migrate", image=image, command=["migrate", JOB_TAG], backoff_limit=1,
    )
    shop.depends(api, on=migrate)
    if AUTOSCALE:
        shop.autoscaler(api, max_replicas=4, cpu=70)
    if FULL:
        db = shop.stateful_set(
            "db", image=image, ports=[5432], replicas=2,
            volumes={{"/var/lib/db": ClaimTemplate("data", size="1Gi")}},
        )
        shop.disruption_budget(db, max_unavailable=1)
        shop.daemon_set("agent", image=image, command=["agent"])
        shop.cron_job("report", schedule="@hourly", image=image)
        shop.ingress("shop", hosts=["shop.example.com"], routes=[Route(web, "/")])
        shop.http_route("shop", gateway="public", routes=[Route(web, "/")])
    return shop
"""

ALL = {
    "Job/migrate": "create",
    "Deployment/api": "create",
    "Service/api": "create",
    "StatefulSet/db": "create",
    "Service/db": "create",
    "PodDisruptionBudget/db": "create",
    "DaemonSet/agent": "create",
    "CronJob/report": "create",
    "Ingress/shop": "create",
    "HTTPRoute/shop": "create",
    "HorizontalPodAutoscaler/api": "create",
}


def _write(directory: Path, name: str, **values: object) -> None:
    settings = {"full": True, "job_tag": "v1", "autoscale": True, **values}
    (directory / name).write_text(COMPOSITION.format(**settings))


@pytest.fixture
def kinds_env(tmp_path):
    _write(tmp_path, "full.py")
    _write(tmp_path, "job_v2.py", job_tag="v2")
    _write(tmp_path, "trimmed.py", full=False)
    _write(tmp_path, "fixed.py", autoscale=False)
    api = FakeAPI()
    with serve(api) as (api, url):
        write_kubeconfig(url, tmp_path / "kubeconfig")
        yield api, tmp_path


def _spec(directory: Path, module: str) -> Path:
    spec = directory / "release.toml"
    spec.write_text(
        textwrap.dedent(
            f"""
            [target]
            kubeconfig = "kubeconfig"
            context = "fake"
            namespace = "{NS}"
            transport = "loopback-http"

            [release]
            name = "shop"
            owner = "{OWNER}"
            field_manager = "{OWNER}"
            composition = "{module}:build"
            state_dir = "state"
            prune = true

            [execution]
            max_seconds = 30
            readiness_seconds = 1
            poll_seconds = 0.05

            [images]
            api = "registry.example/api@{DIGEST}"
            """
        )
    )
    return spec


def _run(spec: Path, *args: str) -> tuple[int, dict, str]:
    result = CliRunner().invoke(app, [*args, "--spec", str(spec)])
    return result.exit_code, json.loads(result.stdout or "{}"), result.output


def _operations(payload: dict) -> dict[str, str]:
    return {f"{a['kind']}/{a['name']}": a["operation"] for a in payload["actions"]}


def _apply(spec: Path, *args: str) -> dict:
    code, planned, output = _run(spec, "plan", *args)
    assert code == 0, output
    # The plan hash carries the planning flags; apply takes only the hash.
    code, applied, output = _run(spec, "apply", "--approve", planned["plan_hash"])
    assert code == 0, output
    assert applied["execution"]["state"] == "ready", applied
    return planned


def test_every_kind_plans_applies_and_replans_as_no_op(kinds_env):
    api, directory = kinds_env
    spec = _spec(directory, "full.py")
    planned = _apply(spec)
    assert _operations(planned) == ALL
    order = [f"{a['kind']}/{a['name']}" for a in planned["actions"]]
    # app.depends(api, on=migrate): the Job completes before the Deployment.
    assert order.index("Job/migrate") < order.index("Deployment/api")
    # Routes, budgets and autoscalers come after the workloads and Services.
    assert order.index("HTTPRoute/shop") > order.index("Service/api")

    sts = api.objects[("StatefulSet", "db")]["spec"]
    assert sts["volumeClaimTemplates"][0]["metadata"]["name"] == "data"
    assert sts["persistentVolumeClaimRetentionPolicy"]["whenDeleted"] == "Retain"
    assert api.objects[("Service", "db")]["spec"]["clusterIP"] == "None"
    assert "replicas" not in api.objects[("Deployment", "api")]["spec"]
    for kind, name in (
        ("StatefulSet", "db"),
        ("DaemonSet", "agent"),
        ("Job", "migrate"),
    ):
        pod = api.objects[(kind, name)]["spec"]["template"]["spec"]
        assert pod["securityContext"]["runAsUser"] == 10001, kind
        assert pod["automountServiceAccountToken"] is False, kind
    cron = api.objects[("CronJob", "report")]["spec"]["jobTemplate"]["spec"]
    assert cron["template"]["spec"]["securityContext"]["runAsNonRoot"] is True

    code, again, output = _run(spec, "plan")
    assert code == 0, output
    assert set(again["summary"]) == {"no-op"}, again["summary"]


def test_changed_job_is_refused_until_replaced(kinds_env):
    api, directory = kinds_env
    _apply(_spec(directory, "full.py"))
    before = api.objects[("Job", "migrate")]["metadata"]["uid"]

    spec = _spec(directory, "job_v2.py")
    code, refused, output = _run(spec, "plan")
    assert code == 2, output
    assert refused["reason"] == "immutable-field-changed"
    (blocking,) = refused["blocking"]
    assert blocking["kind"] == "Job" and blocking["name"] == "migrate"
    assert blocking["suggest"] == ["--replace Job/migrate"]
    assert "spec.template" in blocking["message"]

    planned = _apply(spec, "--replace", "Job/migrate")
    operations = _operations(planned)
    assert operations["Job/migrate"] == "replace"
    assert {v for k, v in operations.items() if k != "Job/migrate"} == {"no-op"}
    job = api.objects[("Job", "migrate")]
    assert job["metadata"]["uid"] != before
    assert job["spec"]["template"]["spec"]["containers"][0]["command"] == [
        "migrate",
        "v2",
    ]
    # A standing replace entry does not rerun an unchanged Job.
    code, again, output = _run(spec, "plan", "--replace", "Job/migrate")
    assert code == 0, output
    assert _operations(again)["Job/migrate"] == "no-op"
    assert "replace Job/migrate: not needed" in output


def test_autoscaled_replicas_are_left_to_the_autoscaler(kinds_env):
    api, directory = kinds_env
    _apply(_spec(directory, "fixed.py"))
    assert api.objects[("Deployment", "api")]["spec"]["replicas"] == 2
    # Adding the autoscaler: the Deployment stops declaring replicas, and the
    # plan does not remove the live count (the HPA owns it from now on).
    planned = _apply(_spec(directory, "full.py"))
    action = next(
        a for a in planned["actions"] if (a["kind"], a["name"]) == ("Deployment", "api")
    )
    assert "/spec/replicas" not in action.get("removes", [])
    assert api.objects[("Deployment", "api")]["spec"]["replicas"] == 2
    # The HPA scales; the next plan leaves it alone.
    api.objects[("Deployment", "api")]["spec"]["replicas"] = 4
    code, again, output = _run(_spec(directory, "full.py"), "plan")
    assert code == 0, output
    assert _operations(again)["Deployment/api"] == "no-op"


def test_prune_and_rollback_of_every_kind(kinds_env):
    api, directory = kinds_env
    _apply(_spec(directory, "full.py"))
    # A claim the StatefulSet controller created: never part of any plan.
    api.put(
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {"name": "data-db-0", "namespace": NS},
            "spec": {"resources": {"requests": {"storage": "1Gi"}}},
        }
    )
    trimmed = _spec(directory, "trimmed.py")
    code, planned, output = _run(trimmed, "plan")
    assert code == 0, output
    deletes = {k for k, v in _operations(planned).items() if v == "delete"}
    assert deletes == {
        "StatefulSet/db",
        "Service/db",
        "PodDisruptionBudget/db",
        "DaemonSet/agent",
        "CronJob/report",
        "Ingress/shop",
        "HTTPRoute/shop",
    }
    code, applied, output = _run(trimmed, "apply", "--approve", planned["plan_hash"])
    assert code == 0, output
    assert applied["execution"]["state"] == "ready", applied
    assert ("StatefulSet", "db") not in api.objects
    assert ("PersistentVolumeClaim", "data-db-0") in api.objects

    code, pending, output = _run(trimmed, "rollback", "previous")
    assert code == 3, output
    creates = {k for k, v in _operations(pending).items() if v == "create"}
    assert creates == deletes
    code, rolled, output = _run(
        trimmed, "rollback", "previous", "--approve", pending["plan_hash"]
    )
    assert code == 0, output
    assert rolled["execution"]["state"] == "ready", rolled
    assert ("HTTPRoute", "shop") in api.objects
    assert ("PersistentVolumeClaim", "data-db-0") in api.objects
