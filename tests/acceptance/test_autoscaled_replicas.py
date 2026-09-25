"""Acceptance: an autoscaler owns ``spec.replicas``; releases do not fight it.

A HorizontalPodAutoscaler writes ``spec.replicas`` through the ``scale``
subresource (:meth:`FakeAPI.scale` records exactly that ownership). Before the
fix a re-plan showed a perpetual ``replicas`` diff and drift, and applying it
reset the count.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from piceli.k8s.cli.release import app
from piceli.k8s.ops.discovery import DiscoveryCoverage, ResourceScope
from piceli.k8s.ops.plan import (
    DeploymentComponent,
    DeploymentComposition,
    FieldManagerEntry,
    ObservedResource,
    ObservedSnapshot,
    Ownership,
    PlanTarget,
    ResourceIntent,
    autoscaled_replicas,
)
from piceli.testing import TYPES, FakeAPI, serve
from tests.acceptance.fake_api import TARGET

DIGEST_1 = "sha256:" + "1" * 64
DIGEST_2 = "sha256:" + "2" * 64

COMPOSITION = """
from piceli.k8s.ops.plan import DeploymentComponent, DeploymentComposition, ResourceIntent

AUTOSCALED = {autoscaled}


def build(ctx):
    meta = lambda name: {{"name": name, "namespace": ctx.namespace}}
    worker = ResourceIntent.from_manifest(
        {{"apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta("worker"),
         "spec": {{"replicas": 2, "selector": {{"matchLabels": {{"app": "worker"}}}},
                  "template": {{"metadata": {{"labels": {{"app": "worker"}}}},
                               "spec": {{"containers": [
                                   {{"name": "worker", "image": ctx.image("api")}}]}}}}}}}}
    )
    resources = [worker]
    if AUTOSCALED:
        resources.append(ResourceIntent.from_manifest(
            {{"apiVersion": "autoscaling/v2", "kind": "HorizontalPodAutoscaler",
             "metadata": meta("worker"),
             "spec": {{"scaleTargetRef": {{"apiVersion": "apps/v1",
                                          "kind": "Deployment", "name": "worker"}},
                      "minReplicas": 3, "maxReplicas": 5}}}}
        ))
    return DeploymentComposition((DeploymentComponent("worker", tuple(resources)),))
"""

HPA_TYPES = {
    **TYPES,
    "horizontalpodautoscalers": (
        "autoscaling/v2",
        "HorizontalPodAutoscaler",
        True,
    ),
}


@pytest.fixture
def env(tmp_path):
    api = FakeAPI(types=HPA_TYPES)
    api.field_ownership = True
    # Server defaults: a no-op then needs the server dry run (as on a real
    # cluster), so the dry run must be of the write the plan declares.
    api.server_defaults = True
    with serve(api) as (served, url):
        (tmp_path / "kubeconfig").write_text(
            textwrap.dedent(
                f"""
                apiVersion: v1
                kind: Config
                clusters: [{{name: fake, cluster: {{server: "{url}"}}}}]
                users: [{{name: nobody, user: {{}}}}]
                contexts: [{{name: fake, context: {{cluster: fake, user: nobody}}}}]
                """
            )
        )
        for module, autoscaled in (("plain", False), ("scaled", True)):
            (tmp_path / f"{module}.py").write_text(
                COMPOSITION.format(autoscaled=autoscaled)
            )
        yield served, tmp_path


def _spec(tmp_path: Path, module: str, digest: str = DIGEST_1) -> Path:
    path = tmp_path / "release.toml"
    path.write_text(
        textwrap.dedent(
            f"""
            [target]
            kubeconfig = "kubeconfig"
            context = "fake"
            namespace = "{TARGET.namespace}"
            cluster_uid = "cluster-uid"
            transport = "loopback-http"

            [release]
            name = "app"
            owner = "acceptance-owner"
            field_manager = "piceli-acceptance"
            composition = "{module}.py:build"
            state_dir = "state"

            [execution]
            max_seconds = 30
            readiness_seconds = 2
            poll_seconds = 0.05

            [images]
            api = "registry.example/app/api@{digest}"
            """
        )
    )
    return path


def _run(spec: Path, *args: str) -> tuple[int, dict]:
    result = CliRunner().invoke(app, [*args, "--spec", str(spec)])
    return result.exit_code, json.loads(result.stdout or "{}")


def _operations(payload: dict) -> dict[str, str]:
    return {f"{a['kind']}/{a['name']}": a["operation"] for a in payload["actions"]}


def _worker(api: FakeAPI) -> dict:
    return api.objects[("Deployment", "worker")]


def _modes(payload: dict) -> list[str]:
    return [item["mode"] for item in payload["autoscaled"]]


def test_autoscaler_takes_replicas_without_diff_drift_or_reset(env):
    api, tmp_path = env
    spec = _spec(tmp_path, "scaled")
    code, planned = _run(spec, "plan")
    assert code == 0, planned
    assert _modes(planned) == ["initial"]
    code, applied = _run(spec, "apply", "--approve", planned["plan_hash"])
    assert code == 0 and applied["release_state"] == "ready", applied
    first = applied["release"]
    assert _worker(api)["spec"]["replicas"] == 2

    api.scale("Deployment", "worker", 3)

    result = CliRunner().invoke(app, ["plan", "--spec", str(spec)])
    planned = json.loads(result.stdout)
    assert result.exit_code == 0, planned
    assert (
        "replicas Deployment/worker: left to HorizontalPodAutoscaler/worker"
        in result.stderr
    )
    assert set(_operations(planned).values()) == {"no-op"}, planned["diffs"]
    assert planned["drift"] == []
    assert planned["autoscaled"] == [
        {
            "resource": {
                "api_version": "apps/v1",
                "kind": "Deployment",
                "namespace": TARGET.namespace,
                "name": "worker",
            },
            "field": "/spec/replicas",
            "autoscalers": ["HorizontalPodAutoscaler/worker"],
            "mode": "yielded",
        }
    ]

    spec = _spec(tmp_path, "scaled", DIGEST_2)
    code, planned = _run(spec, "plan")
    assert code == 0, planned
    assert _operations(planned)["Deployment/worker"] == "apply"
    (diff,) = planned["diffs"]
    assert [change["path"] for change in diff["changes"]] == [
        "/spec/template/spec/containers/0/image"
    ]
    code, applied = _run(spec, "apply", "--approve", planned["plan_hash"])
    assert code == 0 and applied["release_state"] == "ready", applied
    live = _worker(api)
    assert live["spec"]["replicas"] == 3
    assert live["spec"]["template"]["spec"]["containers"][0]["image"].endswith(DIGEST_2)
    assert ("f:spec", "f:replicas") in api.managers("Deployment", "worker")[
        "kube-controller-manager/Update"
    ]

    code, rolled = _run(spec, "rollback", first, "--auto-approve")
    assert code == 0 and rolled["selected"] == first, rolled
    assert _worker(api)["spec"]["replicas"] == 3


def test_adding_an_autoscaler_holds_the_live_count(env):
    api, tmp_path = env
    code, applied = _run(_spec(tmp_path, "plain"), "apply", "--auto-approve")
    assert code == 0, applied
    assert _worker(api)["spec"]["replicas"] == 2

    # The live count differs from the declared one (someone scaled by hand
    # before the autoscaler existed): the release keeps the live value.
    api.objects[("Deployment", "worker")]["spec"]["replicas"] = 4
    spec = _spec(tmp_path, "scaled")
    code, planned = _run(spec, "plan")
    assert code == 0, planned
    assert _modes(planned) == ["held"]
    assert _operations(planned) == {
        "Deployment/worker": "no-op",
        "HorizontalPodAutoscaler/worker": "create",
    }, planned["diffs"]
    code, applied = _run(spec, "apply", "--approve", planned["plan_hash"])
    assert code == 0 and applied["release_state"] == "ready", applied
    assert _worker(api)["spec"]["replicas"] == 4

    # A later release that changes the image never resets or removes it.
    code, applied = _run(_spec(tmp_path, "scaled", DIGEST_2), "apply", "--auto-approve")
    assert code == 0 and applied["release_state"] == "ready", applied
    assert _worker(api)["spec"]["replicas"] == 4


TARGET_REF = PlanTarget("cluster-uid", "piceli-test")


def _deployment(replicas: int | None) -> dict:
    spec: dict = {"selector": {"matchLabels": {"app": "web"}}}
    if replicas is not None:
        spec["replicas"] = replicas
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "web", "namespace": "piceli-test"},
        "spec": spec,
    }


def _hpa(target: str = "web", api_version: str = "apps/v1") -> dict:
    return {
        "apiVersion": "autoscaling/v2",
        "kind": "HorizontalPodAutoscaler",
        "metadata": {"name": "web", "namespace": "piceli-test"},
        "spec": {
            "scaleTargetRef": {
                "apiVersion": api_version,
                "kind": "Deployment",
                "name": target,
            },
            "minReplicas": 2,
            "maxReplicas": 4,
        },
    }


def _observed(manifest: dict, managers: list[FieldManagerEntry]) -> ObservedResource:
    value = json.loads(json.dumps(manifest))
    value["metadata"].update(uid="uid-" + value["kind"], resourceVersion="7")
    resource = ObservedResource.from_manifest(
        value, ownership=Ownership.MANAGED, scope=ResourceScope.NAMESPACED
    )
    return ObservedResource(
        resource.intent,
        resource.precondition,
        resource.ownership,
        resource.retained,
        field_managers=tuple(managers),
    )


def _entry(manager: str, subresource: str = "", operation: str = "Update"):
    return FieldManagerEntry(
        manager, operation, subresource, json.dumps({"f:spec": {"f:replicas": {}}})
    )


def _transform(desired: list[dict], observed: list[ObservedResource]):
    composition = DeploymentComposition(
        (
            DeploymentComponent(
                "web", tuple(ResourceIntent.from_manifest(m) for m in desired)
            ),
        )
    )
    snapshot = ObservedSnapshot(
        TARGET_REF,
        DiscoveryCoverage("fake", "capture", "policy", (), (), ()),
        tuple(observed),
    )
    result, report = autoscaled_replicas(composition, snapshot, "piceli")
    again, _ = autoscaled_replicas(result, snapshot, "piceli")
    assert again == result  # idempotent
    (component,) = result.components
    return component.resources[0].manifest["spec"], [item.mode for item in report]


def test_modes_follow_the_live_owner_of_replicas():
    # No autoscaler: untouched.
    assert _transform([_deployment(2)], []) == (
        {"replicas": 2, "selector": {"matchLabels": {"app": "web"}}},
        [],
    )
    # Not live yet: the declared value is the initial size.
    assert _transform([_deployment(2), _hpa()], [])[1] == ["initial"]
    # Live, owned by Piceli only: the live value is held.
    spec, modes = _transform(
        [_deployment(2), _hpa()], [_observed(_deployment(5), [_entry("piceli")])]
    )
    assert (spec["replicas"], modes) == (5, ["held"])
    # A client (transferable) wrote it: still held, never dropped.
    spec, modes = _transform(
        [_deployment(2), _hpa()], [_observed(_deployment(5), [_entry("kubectl-edit")])]
    )
    assert (spec["replicas"], modes) == (5, ["held"])
    # The autoscaler's scale entry owns it: not declared at all.
    spec, modes = _transform(
        [_deployment(2), _hpa()],
        [_observed(_deployment(3), [_entry("kube-controller-manager", "scale")])],
    )
    assert ("replicas" not in spec, modes) == (True, ["yielded"])
    # A live autoscaler this release does not declare counts too.
    spec, modes = _transform(
        [_deployment(2)],
        [
            _observed(_deployment(3), [_entry("kube-controller-manager", "scale")]),
            _observed(_hpa(), []),
        ],
    )
    assert ("replicas" not in spec, modes) == (True, ["yielded"])
    # An autoscaler of another workload or API group does not.
    assert _transform([_deployment(2), _hpa(target="api")], [])[1] == []
    assert (
        _transform([_deployment(2), _hpa(api_version="example.test/v1")], [])[1] == []
    )
