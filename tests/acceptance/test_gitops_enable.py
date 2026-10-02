"""``piceli gitops enable|disable|status`` against the in-process fake API.

The install is planned with a hash (exit 3), applied only with that hash,
idempotent, and scoped: the controller's RBAC, a pinned image, a Git
credentials Secret that is mounted but never read or printed.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.gitops.config import ControllerConfig
from piceli.gitops.install import DEPLOYER, NAME
from piceli.gitops.ports import DefaultPorts
from piceli.k8s.cli import app
from piceli.testing import FakeAPI, fake_cluster, write_kubeconfig

IMAGE = "registry.example.com/piceli@sha256:" + "a" * 64
BUILDER = "registry.example.com/builder@sha256:" + "b" * 64
SECRET_VALUE = "ghp-not-a-real-token"


@pytest.fixture
def cluster(tmp_path: Path) -> Iterator[tuple[FakeAPI, Path]]:
    api = FakeAPI(namespace="piceli-system")
    with fake_cluster(api) as running:
        # The owner's credentials Secret: the install must never read it.
        api.put(
            {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": "git-creds", "namespace": "piceli-system"},
                "stringData": {"password": SECRET_VALUE},
            }
        )
        yield api, write_kubeconfig(running.url, tmp_path / "kubeconfig")


def _enable(kubeconfig: Path, *extra: str) -> Any:
    return CliRunner().invoke(app, _enable_args(kubeconfig, *extra))


def _enable_args(kubeconfig: Path, *extra: str) -> list[str]:
    return [
        "gitops",
        "enable",
        "deploy/app.py:pipeline",
        "--repo",
        "https://git.example.com/org/app.git",
        "--branches",
        "main,wp-*",
        "--poll",
        "2m",
        "--image",
        IMAGE,
        "--credentials-secret",
        "git-creds",
        "--builder-image",
        BUILDER,
        "--builder-selector",
        "piceli.io/builder=true",
        "--kubeconfig",
        str(kubeconfig),
        "--context",
        "fake",
        "--transport",
        "loopback-http",
        *extra,
    ]


def _object(api: FakeAPI, kind: str, name: str) -> dict[str, Any]:
    return api.objects[(kind, name)]


def test_enable_plans_then_installs_with_the_hash(
    cluster: tuple[FakeAPI, Path],
) -> None:
    api, kubeconfig = cluster
    planned = _enable(kubeconfig)
    assert planned.exit_code == 3, planned.output
    body = json.loads(planned.stdout)
    assert body["state"] == "approval-required"
    kinds = {(c["kind"], c["name"]): c["operation"] for c in body["changes"]}
    assert kinds[("Deployment", NAME)] == "create"
    assert kinds[("ClusterRole", DEPLOYER)] == "create"
    assert kinds[("Namespace", "piceli-system")] == "apply"  # labels added
    assert ("Deployment", NAME) not in api.objects  # nothing ran
    assert body["approve_command"].endswith("--approve " + body["plan_hash"])
    assert SECRET_VALUE not in planned.output
    # No request read the Secret.
    assert not [r for r in api.requests if "/secrets" in r["path"]]

    wrong = _enable(kubeconfig, "--approve", "sha256:" + "0" * 64)
    assert wrong.exit_code == 2
    assert json.loads(wrong.stdout)["reason"] == "gitops-plan-changed"

    done = _enable(kubeconfig, "--approve", body["plan_hash"])
    assert done.exit_code == 0, done.output
    assert json.loads(done.stdout)["state"] == "enabled"

    deployment = _object(api, "Deployment", NAME)
    spec = deployment["spec"]
    assert spec["replicas"] == 1 and spec["strategy"] == {"type": "Recreate"}
    pod = spec["template"]["spec"]
    container = pod["containers"][0]
    assert container["image"] == IMAGE
    assert container["command"] == ["piceli"]
    assert container["args"][:2] == ["gitops", "run"]
    assert "--service-account" in container["args"]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert pod["serviceAccountName"] == NAME
    secret_volume = next(v for v in pod["volumes"] if "secret" in v)
    assert secret_volume["secret"]["secretName"] == "git-creds"
    assert not any(
        "env" in c and "valueFrom" in json.dumps(c["env"]) for c in [container]
    )

    config = json.loads(
        _object(api, "ConfigMap", "piceli-gitops-config")["data"]["config.json"]
    )
    assert config["repo"] == "https://git.example.com/org/app.git"
    assert config["branches"] == ["main", "wp-*"] and config["poll_seconds"] == 120
    assert config["build"]["builder_image"] == BUILDER
    assert config["build"]["selector"] == {"piceli.io/builder": "true"}
    assert "git-creds" not in json.dumps(config)
    ControllerConfig.from_dict(config)  # what `gitops run --config` reads

    # Least privilege: the cluster-wide role reads nodes, manages namespaces,
    # removes a torn-down branch's volumes and binds only the deployer role;
    # the rest is namespaced.
    cluster_rules = _object(api, "ClusterRole", NAME)["rules"]
    resources = {r for rule in cluster_rules for r in rule["resources"]}
    assert resources == {
        "nodes",
        "namespaces",
        "persistentvolumes",
        "endpointslices",
        "rolebindings",
        "clusterroles",
    }
    slices = next(r for r in cluster_rules if "endpointslices" in r["resources"])
    assert slices["resourceNames"] == ["kubernetes"] and slices["verbs"] == ["get"]
    volumes = next(r for r in cluster_rules if "persistentvolumes" in r["resources"])
    # Reading volumes only; deleting them is the --delete-volumes opt-in.
    assert volumes["verbs"] == ["get", "list"]
    assert not any(
        "persistentvolumes" in r["resources"] and "delete" in r["verbs"]
        for r in cluster_rules
    )
    bind = next(rule for rule in cluster_rules if "bind" in rule["verbs"])
    assert bind["resourceNames"] == [DEPLOYER] and bind["verbs"] == ["bind"]
    nodes = next(rule for rule in cluster_rules if "nodes" in rule["resources"])
    assert set(nodes["verbs"]) == {"get", "list", "watch"}
    assert "secrets" not in json.dumps(cluster_rules)
    role = _object(api, "Role", NAME)
    assert role["metadata"]["namespace"] == "piceli-system"
    assert "secrets" not in json.dumps(role["rules"])
    claim = _object(api, "PersistentVolumeClaim", "piceli-gitops-state")
    assert claim["spec"]["resources"]["requests"]["storage"] == "10Gi"

    again = _enable(kubeconfig)
    assert again.exit_code == 0, again.output
    assert json.loads(again.stdout)["state"] == "unchanged"


def test_enable_refuses_unpinned_images_and_credentials_in_urls(
    cluster: tuple[FakeAPI, Path],
) -> None:
    _, kubeconfig = cluster
    runner = CliRunner()
    base = [
        "gitops",
        "enable",
        "deploy/app.py:pipeline",
        "--kubeconfig",
        str(kubeconfig),
    ]
    base += ["--context", "fake", "--transport", "loopback-http"]
    latest = runner.invoke(
        app,
        [*base, "--repo", "https://git.example.com/a.git", "--image", "piceli:latest"],
    )
    assert json.loads(latest.stdout)["reason"] == "gitops-image-unpinned"
    leaked = runner.invoke(
        app,
        [
            *base,
            "--repo",
            f"https://bot:{SECRET_VALUE}@git.example.com/a.git",
            "--image",
            IMAGE,
        ],
    )
    assert json.loads(leaked.stdout)["reason"] == "gitops-repo-invalid"
    assert SECRET_VALUE not in leaked.output


def test_cluster_rbac_is_the_owners_opt_in(cluster: tuple[FakeAPI, Path]) -> None:
    _, kubeconfig = cluster
    plain = json.loads(_enable(kubeconfig).stdout)
    wider = json.loads(_enable(kubeconfig, "--cluster-rbac").stdout)
    assert plain["plan_hash"] != wider["plan_hash"]


def test_status_and_disable_keep_the_environments(
    cluster: tuple[FakeAPI, Path],
) -> None:
    api, kubeconfig = cluster
    runner = CliRunner()
    target = ["--kubeconfig", str(kubeconfig), "--context", "fake"]
    target += ["--transport", "loopback-http"]
    missing = runner.invoke(app, ["gitops", "status", *target])
    assert json.loads(missing.stdout)["reason"] == "gitops-not-installed"

    plan = json.loads(_enable(kubeconfig).stdout)
    assert _enable(kubeconfig, "--approve", plan["plan_hash"]).exit_code == 0
    status = {
        "schema": "piceli.gitops-status.v1",
        "controller": {"repo": "https://git.example.com/org/app.git", "branches": ["wp-*"], "last_poll": None, "last_error": None, "poll_seconds": 60},
        "envs": {"wp-1": {"branch": "wp-1", "commit": "a" * 40, "state": "approval-required", "plan_hash": "sha256:" + "d" * 64}},
        "rejected_requests": [],
    }  # fmt: skip
    api.put(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "piceli-gitops-status", "namespace": "piceli-system"},
            "data": {"status.json": json.dumps(status)},
        }
    )
    shown = runner.invoke(app, ["gitops", "status", *target, "--json"])
    assert shown.exit_code == 0, shown.output
    body = json.loads(shown.stdout)
    assert body["envs"]["wp-1"]["plan_hash"] == "sha256:" + "d" * 64
    assert body["deployment_ready"] is True
    assert "piceli gitops approve wp-1 sha256:" in shown.stderr

    approved = runner.invoke(
        app, ["gitops", "approve", "wp-1", "sha256:" + "d" * 64, *target]
    )
    assert approved.exit_code == 0, approved.output
    requests = _object(api, "ConfigMap", "piceli-gitops-requests")["data"]
    assert [json.loads(v)["kind"] for v in requests.values()] == ["approve"]

    planned = runner.invoke(app, ["gitops", "disable", *target])
    assert planned.exit_code == 3, planned.output
    body = json.loads(planned.stdout)
    deleted = {
        (c["kind"], c["name"]) for c in body["changes"] if c["operation"] == "delete"
    }
    assert ("Deployment", NAME) in deleted and ("ClusterRole", DEPLOYER) in deleted
    assert ("PersistentVolumeClaim", "piceli-gitops-state") not in deleted
    assert not any(c["kind"] == "Namespace" for c in body["changes"])
    removed = runner.invoke(
        app, ["gitops", "disable", *target, "--approve", body["plan_hash"]]
    )
    assert removed.exit_code == 0, removed.output
    assert ("Deployment", NAME) not in api.objects
    assert ("PersistentVolumeClaim", "piceli-gitops-state") in api.objects
    assert ("Namespace", "piceli-system") in api.objects
    assert ("Secret", "git-creds") in api.objects


def test_default_ports_wire_the_cluster_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from piceli.artifacts import cluster_build

    seen: dict[str, Any] = {}

    def run_build_job(pipeline: Any, commit: str, **kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs, commit=commit)
        return {"outputs": {"images": {}}, "job": {"commit": commit}}

    monkeypatch.setattr(cluster_build, "run_build_job", run_build_job)
    config = ControllerConfig(
        pipeline="deploy/app.py:pipeline",
        repo="https://git.example.com/org/app.git",
        branches=("wp-*",),
        builder_image=BUILDER,
        build_git_secret="git-creds",
        builder_selector=(("piceli.io/builder", "true"),),
    )
    ports = DefaultPorts(
        tmp_path / "kubeconfig",
        "fake",
        tmp_path,
        namespace="piceli-system",
        config=config,
    )
    receipt = ports.build(
        object(), "c" * 40, cache_key="wp-1", platforms=(), checkout=tmp_path
    )
    assert receipt["job"]["commit"] == "c" * 40
    build = seen["config"]
    assert build.image == BUILDER and build.git_secret == "git-creds"
    assert build.namespace == "piceli-system" and build.repo_root == tmp_path
    assert dict(build.selector) == {"piceli.io/builder": "true"}
    assert seen["cache_key"] == "wp-1" and seen["approve"] is None
    assert seen["platforms"] == cluster_build.DEFAULT_PLATFORMS


def test_default_ports_read_pushed_images(
    cluster: tuple[FakeAPI, Path], tmp_path: Path
) -> None:
    from piceli.k8s.cli.env_push import configmap_name

    api, kubeconfig = cluster
    images = {"web": {"digest": "sha256:" + "e" * 64}}
    api.put(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": configmap_name("wp-1"), "namespace": "piceli-system"},
            "data": {"images": json.dumps(images), "commit": "f" * 40},
        }
    )
    config = ControllerConfig(
        pipeline="deploy/app.py:pipeline",
        repo="https://git.example.com/a.git",
        branches=("wp-*",),
    )
    ports = DefaultPorts(
        kubeconfig,
        "fake",
        tmp_path,
        namespace="piceli-system",
        config=config,
        transport="loopback-http",
    )
    assert ports.pushed_images(None, "wp-1", "piceli-system") == {
        "commit": "f" * 40,
        "images": images,
    }
    assert ports.pushed_images(None, "wp-2", "piceli-system") is None


def test_a_config_change_rolls_the_controller(
    cluster: tuple[FakeAPI, Path],
) -> None:
    api, kubeconfig = cluster
    first = json.loads(_enable(kubeconfig).stdout)
    assert _enable(kubeconfig, "--approve", first["plan_hash"]).exit_code == 0
    template = _object(api, "Deployment", NAME)["spec"]["template"]
    before = template["metadata"].get("annotations", {}).get("piceli.io/config-hash")
    assert before is not None and before.startswith("sha256:")
    # Only the builder image changes: the Deployment must roll too, or the
    # running controller keeps reading the old configuration.
    builder = "registry.example.com/builder@sha256:" + "c" * 64
    args = [a if a != BUILDER else builder for a in _enable_args(kubeconfig)]
    planned = CliRunner().invoke(app, args)
    assert planned.exit_code == 3, planned.output
    changes = {
        (c["kind"], c["name"]): c["operation"]
        for c in json.loads(planned.stdout)["changes"]
    }
    assert changes[("ConfigMap", "piceli-gitops-config")] == "apply"
    assert changes[("Deployment", NAME)] == "apply"
    done = CliRunner().invoke(
        app, [*args, "--approve", json.loads(planned.stdout)["plan_hash"]]
    )
    assert done.exit_code == 0, done.output
    template = _object(api, "Deployment", NAME)["spec"]["template"]
    assert template["metadata"]["annotations"]["piceli.io/config-hash"] != before


def test_the_deployer_role_covers_metrics_and_scale_for_releases() -> None:
    from piceli.gitops.install import render_foundation

    deployer = next(
        item
        for item in render_foundation("piceli-system")
        if item["kind"] == "ClusterRole" and item["metadata"]["name"] == DEPLOYER
    )

    def allowed(group: str, resource: str, verb: str) -> bool:
        return any(
            group in rule["apiGroups"]
            and resource in rule["resources"]
            and verb in rule["verbs"]
            for rule in deployer["rules"]
        )

    # A release may create a Role granting metrics reads only when the
    # deployer holds them (the API refuses an escalation otherwise).
    for resource in ("pods", "nodes"):
        for verb in ("get", "list", "watch"):
            assert allowed("metrics.k8s.io", resource, verb), (resource, verb)
    # Restore points scale workloads down and back up.
    for resource in ("deployments/scale", "statefulsets/scale"):
        for verb in ("get", "update", "patch"):
            assert allowed("apps", resource, verb), (resource, verb)
    # Checks reach their targets through the API server proxy, read only.
    for resource in ("services/proxy", "pods/proxy"):
        assert allowed("", resource, "get"), resource
        assert not allowed("", resource, "create"), resource
    # First-install checks stage copies of the release's Secrets/ConfigMaps.
    for resource in ("secrets", "configmaps"):
        for verb in ("create", "delete", "list"):
            assert allowed("", resource, verb), (resource, verb)
    assert not allowed("metrics.k8s.io", "pods", "delete")


def test_default_ports_keep_a_failed_builds_details(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from piceli.artifacts import cluster_build
    from piceli.gitops import GitOpsError
    from piceli.pipeline.errors import PipelineError

    details = {"outcome": {"state": "failed", "log_tail": "  | error: x"}}

    def run_build_job(pipeline: Any, commit: str, **kwargs: Any) -> dict[str, Any]:
        raise PipelineError("cluster-build-failed", "ended failed", details=details)

    monkeypatch.setattr(cluster_build, "run_build_job", run_build_job)
    config = ControllerConfig(
        pipeline="deploy/app.py:pipeline",
        repo="https://git.example.com/org/app.git",
        branches=("wp-*",),
        builder_image=BUILDER,
    )
    ports = DefaultPorts(
        tmp_path / "kubeconfig", "fake", tmp_path, namespace="ns", config=config
    )
    with pytest.raises(GitOpsError) as failed:
        ports.build(object(), "c" * 40, cache_key="b", platforms=(), checkout=tmp_path)
    assert failed.value.code == "cluster-build-failed"
    assert failed.value.details == details


def test_delete_volumes_is_an_explicit_opt_in() -> None:
    from piceli.gitops.install import has_volume_delete, render_foundation
    from piceli.infra import Controller
    from piceli.infra.cluster import ClusterError

    def role(**options: bool) -> dict[str, object]:
        return next(
            item
            for item in render_foundation("piceli-system", **options)
            if item["kind"] == "ClusterRole" and item["metadata"]["name"] == NAME
        )

    assert not has_volume_delete(role())
    assert has_volume_delete(role(delete_volumes=True))
    assert Controller(on="node-a", delete_volumes=True).delete_volumes
    with pytest.raises(ClusterError):
        Controller(on="node-a", delete_volumes="yes")  # type: ignore[arg-type]


def test_enable_with_delete_volumes_grants_the_volume_delete(
    cluster: tuple[FakeAPI, Path],
) -> None:
    from piceli.gitops.install import has_volume_delete

    api, kubeconfig = cluster
    planned = _enable(kubeconfig, "--delete-volumes")
    assert planned.exit_code == 3, planned.output
    body = json.loads(planned.stdout)
    assert "--delete-volumes" in body["approve_command"]
    done = _enable(kubeconfig, "--delete-volumes", "--approve", body["plan_hash"])
    assert done.exit_code == 0, done.output
    assert has_volume_delete(_object(api, "ClusterRole", NAME))
