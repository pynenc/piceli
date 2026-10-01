"""``piceli cluster init|status`` and ``piceli secrets git`` against the fake API.

The cluster is declared in a composition module and reached through its
credential profile. Init plans (exit 3), applies only with the hash, labels
the nodes for their roles, installs the registry with its node agents, the
controller's foundation and (through the UI seam) the UI, and a re-run is
unchanged. A profile pointing at another API server is refused before any
request. ``secrets git`` reads the token from stdin and never prints it.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.infra import cluster_init
from piceli.k8s.cli import app
from piceli.profiles import save_profile
from piceli.testing import FakeAPI, fake_cluster, write_kubeconfig

TOKEN = "ghp-not-a-real-token-0123456789"


@pytest.fixture
def cluster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[FakeAPI, str]]:
    api = FakeAPI(namespace="piceli-system")
    api.add_node("node-a", architecture="amd64", uid="uid-a")
    api.add_node("node-b", architecture="arm64", uid="uid-b")
    api.nodes["node-b"]["status"]["nodeInfo"].update(
        {
            "kubeletVersion": "v1.32.5+k3s1",
            "containerRuntimeVersion": "containerd://2.0.5-k3s1",
        }
    )
    api.nodes["node-a"]["status"]["nodeInfo"]["containerRuntimeVersion"] = (
        "containerd://2.1.0"
    )
    monkeypatch.setenv("PICELI_PROFILES_DIR", str(tmp_path / "profiles"))
    monkeypatch.delenv("PICELI_GIT_TOKEN", raising=False)
    monkeypatch.setattr(cluster_init, "ui_renderer", lambda: None)
    with fake_cluster(api) as running:
        kubeconfig = write_kubeconfig(running.url, tmp_path / "kubeconfig")
        save_profile("my-cluster", kubeconfig, "fake")
        yield api, running.url


def _composition(
    tmp_path: Path,
    url: str,
    roles_b: str = '["workloads"]',
    name: str = "infra.py",
    controller: str = 'Controller(on="node-a", poll="1m")',
) -> str:
    (tmp_path / name).write_text(
        f"""
from piceli import Registry
from piceli.infra import Cluster, Controller, Node, Ui

my_cluster = Cluster(
    "my-cluster",
    api="{url}",
    credentials="my-cluster",
    nodes=[
        Node("node-a", arch="amd64", roles=["builder", "controller", "registry"]),
        Node("node-b", arch="arm64", roles={roles_b}),
    ],
    registry=Registry.in_cluster(on="node-a", storage="1Gi"),
    controller={controller},
    ui=Ui(access="forward"),
)
"""
    )
    return f"{tmp_path / name}:my_cluster"


def _run(*args: str, stdin: str | None = None) -> Any:
    return CliRunner().invoke(app, [*args, "--transport", "loopback-http"], input=stdin)


def _init(ref: str, *extra: str) -> Any:
    return _run("cluster", "init", ref, "--wait", "0", *extra)


def test_init_plans_labels_installs_and_is_idempotent(
    cluster: tuple[FakeAPI, str], tmp_path: Path
) -> None:
    api, url = cluster
    ref = _composition(tmp_path, url)
    planned = _init(ref)
    assert planned.exit_code == 3, planned.output
    body = json.loads(planned.stdout)
    assert body["state"] == "approval-required"
    assert body["schema"] == "piceli.cluster-init-plan.v1"
    nodes = {n["name"]: n for n in body["nodes"]}
    assert nodes["node-a"]["set"] == {
        "piceli.io/builder": "true",
        "piceli.io/role-builder": "true",
        "piceli.io/role-controller": "true",
        "piceli.io/role-registry": "true",
        "piceli.io/runtime": "containerd",
    }
    assert nodes["node-b"]["set"] == {
        "piceli.io/role-workloads": "true",
        "piceli.io/runtime": "k3s",
    }
    kinds = {(c["kind"], c["name"]) for c in body["changes"]}
    assert ("DaemonSet", "piceli-registry-mirror-k3s") in kinds
    assert ("ClusterRole", "piceli-gitops") in kinds
    assert ("ConfigMap", "piceli-cluster") in kinds
    assert ("Deployment", "piceli-gitops") not in kinds  # gitops enable adds it
    assert body["ui"] == "unavailable"
    assert "piceli.io/role-builder" not in api.nodes["node-a"]["metadata"].get(
        "labels", {}
    )

    wrong = _init(ref, "--approve", "sha256:" + "0" * 64)
    assert wrong.exit_code == 2
    assert json.loads(wrong.stdout)["reason"] == "cluster-plan-changed"

    done = _init(ref, "--approve", body["plan_hash"])
    assert done.exit_code == 0, done.output
    assert json.loads(done.stdout)["state"] == "initialized"
    labels = api.nodes["node-a"]["metadata"]["labels"]
    assert labels["piceli.io/builder"] == "true"
    assert api.nodes["node-b"]["metadata"]["labels"]["piceli.io/runtime"] == "k3s"
    stored = api.objects[("ConfigMap", "piceli-cluster")]["data"]["cluster.json"]
    assert json.loads(stored)["nodes"][1] == {
        "name": "node-b",
        "arch": "arm64",
        "roles": ["workloads"],
    }
    assert ("Deployment", "piceli-registry") in api.objects

    again = _init(ref)
    assert again.exit_code == 0, again.output
    assert json.loads(again.stdout)["state"] == "unchanged"

    # A role removed: only the label init set goes; a label set by hand stays.
    api.nodes["node-b"]["metadata"]["labels"]["team"] = "web"
    changed = _composition(tmp_path, url, roles_b='["branch-envs"]', name="v2.py")
    replan = _init(changed)
    assert replan.exit_code == 3, replan.output
    node_b = {n["name"]: n for n in json.loads(replan.stdout)["nodes"]}["node-b"]
    assert node_b["set"] == {"piceli.io/role-branch-envs": "true"}
    assert node_b["remove"] == ["piceli.io/role-workloads"]
    applied = _init(changed, "--approve", json.loads(replan.stdout)["plan_hash"])
    assert applied.exit_code == 0, applied.output
    assert api.nodes["node-b"]["metadata"]["labels"] == {
        "piceli.io/role-branch-envs": "true",
        "piceli.io/runtime": "k3s",
        "team": "web",
    }


def test_a_profile_for_another_api_server_is_refused_before_any_request(
    cluster: tuple[FakeAPI, str], tmp_path: Path
) -> None:
    api, _url = cluster
    ref = _composition(tmp_path, "https://10.0.0.1:6443")
    before = len(api.requests)
    for command in (("cluster", "init", ref), ("cluster", "status", ref)):
        refused = _run(*command)
        assert refused.exit_code == 2, refused.output
        assert json.loads(refused.stdout)["reason"] == "cluster-api-mismatch"
    assert len(api.requests) == before


def test_missing_node_and_wrong_arch_are_refused(
    cluster: tuple[FakeAPI, str], tmp_path: Path
) -> None:
    api, url = cluster
    del api.nodes["node-b"]
    missing = _init(_composition(tmp_path, url))
    assert json.loads(missing.stdout)["reason"] == "cluster-node-missing"
    api.add_node("node-b", architecture="amd64")
    wrong = _init(_composition(tmp_path, url, name="v2.py"))
    assert json.loads(wrong.stdout)["reason"] == "cluster-node-arch-mismatch"


def test_the_ui_is_installed_through_its_renderer(
    cluster: tuple[FakeAPI, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api, url = cluster
    seen: list[Any] = []

    def render_ui(declared: Any) -> list[dict[str, Any]]:
        seen.append(declared)
        return [
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "piceli-ui-test", "namespace": "piceli-system"},
                "data": {"on": "node-a"},
            }
        ]

    monkeypatch.setattr(cluster_init, "ui_renderer", lambda: render_ui)
    ref = _composition(tmp_path, url)
    planned = _init(ref)
    body = json.loads(planned.stdout)
    assert body["ui"] == "included"
    assert seen[0].name == "my-cluster"
    done = _init(ref, "--approve", body["plan_hash"])
    assert done.exit_code == 0, done.output
    assert ("ConfigMap", "piceli-ui-test") in api.objects
    status = _run("cluster", "status", ref, "--json")
    assert status.exit_code == 0, status.output
    report = json.loads(status.stdout)
    assert report["ui"] == {
        "state": "included",
        "installed": True,
        "health": "not-installed",  # the stub renders no piceli-ui Deployment
    }


def test_status_before_and_after_init(
    cluster: tuple[FakeAPI, str], tmp_path: Path
) -> None:
    _api, url = cluster
    ref = _composition(tmp_path, url)
    before = _run("cluster", "status", ref, "--json")
    assert before.exit_code == 0, before.output
    assert json.loads(before.stdout)["state"] == "not-initialized"
    planned = json.loads(_init(ref).stdout)
    assert _init(ref, "--approve", planned["plan_hash"]).exit_code == 0
    after = json.loads(_run("cluster", "status", ref, "--json").stdout)
    assert after["schema"] == "piceli.cluster-status.v1"
    assert all(row["labels_ok"] for row in after["nodes"])
    assert after["controller"]["foundation"] == {
        "service_account": True,
        "cluster_role": True,
        "deployer_role": True,
        "state_claim": True,
    }
    assert after["controller"]["deployment"] is None
    assert after["git_secret"]["present"] is False
    assert after["registry"]["host"] == "piceli-registry.piceli-system.svc:5000"


def _secrets(ref: str, *extra: str, stdin: str | None = None) -> Any:
    return _run("secrets", "git", "--cluster", ref, *extra, stdin=stdin)


def test_secrets_git_reads_stdin_and_never_prints_the_token(
    cluster: tuple[FakeAPI, str], tmp_path: Path
) -> None:
    api, url = cluster
    ref = _composition(tmp_path, url)
    early = _secrets(ref, "--prompt", stdin=TOKEN + "\n")
    assert json.loads(early.stdout)["reason"] == "cluster-not-initialized"
    planned = json.loads(_init(ref).stdout)
    assert _init(ref, "--approve", planned["plan_hash"]).exit_code == 0

    created = _secrets(ref, "--prompt", "--username", "robot", stdin=TOKEN + "\n")
    assert created.exit_code == 0, created.output
    assert json.loads(created.stdout) == {
        "state": "created",
        "secret": {
            "namespace": "piceli-system",
            "name": "piceli-build-git",
            "keys": ["password", "username"],
        },
        "cluster": "my-cluster",
    }
    secret = api.objects[("Secret", "piceli-build-git")]
    assert base64.b64decode(secret["data"]["password"]).decode() == TOKEN
    assert base64.b64decode(secret["data"]["username"]).decode() == "robot"

    updated = _secrets(ref, "--prompt", stdin="second-" + TOKEN + "\n")
    assert updated.exit_code == 0, updated.output
    assert json.loads(updated.stdout)["state"] == "updated"
    secret = api.objects[("Secret", "piceli-build-git")]
    assert base64.b64decode(secret["data"]["password"]).decode() == "second-" + TOKEN

    status = _run("cluster", "status", ref, "--json")
    assert json.loads(status.stdout)["git_secret"]["keys"] == ["password", "username"]
    for result in (created, updated, status):
        assert TOKEN not in result.stdout and TOKEN not in result.stderr


@pytest.mark.parametrize(
    "extra",
    [
        ("--prompt", TOKEN),
        ("--token", TOKEN),
        (f"--token={TOKEN}",),
        ("--password", TOKEN, "--prompt"),
    ],
)
def test_secrets_git_refuses_a_token_argument_without_echoing_it(
    cluster: tuple[FakeAPI, str], tmp_path: Path, extra: tuple[str, ...]
) -> None:
    api, url = cluster
    before = len(api.requests)
    refused = _secrets(_composition(tmp_path, url), *extra)
    assert refused.exit_code == 2, refused.output
    assert json.loads(refused.stdout)["reason"] == "secrets-token-refused"
    assert TOKEN not in refused.stdout and TOKEN not in refused.stderr
    assert len(api.requests) == before


def test_secrets_git_refuses_the_environment_a_missing_prompt_and_no_token(
    cluster: tuple[FakeAPI, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _api, url = cluster
    ref = _composition(tmp_path, url)
    no_prompt = _secrets(ref, stdin=TOKEN + "\n")
    assert json.loads(no_prompt.stdout)["reason"] == "secrets-prompt-required"
    empty = _secrets(ref, "--prompt", stdin="\n")
    assert json.loads(empty.stdout)["reason"] == "secrets-token-empty"
    monkeypatch.setenv("PICELI_GIT_TOKEN", TOKEN)
    from_env = _secrets(ref, "--prompt", stdin=TOKEN + "\n")
    assert json.loads(from_env.stdout)["reason"] == "secrets-token-refused"
    assert TOKEN not in from_env.stdout and TOKEN not in from_env.stderr


def test_the_real_ui_installer_needs_a_pinned_image(
    cluster: tuple[FakeAPI, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from piceli.infra.ui_install import render_ui

    api, url = cluster
    monkeypatch.setattr(cluster_init, "ui_renderer", lambda: render_ui)
    unpinned = _init(_composition(tmp_path, url))
    assert unpinned.exit_code == 2, unpinned.output
    assert json.loads(unpinned.stdout)["reason"] == "ui-install-image-unpinned"

    image = "example.com/piceli@sha256:" + "a" * 64
    ref = _composition(
        tmp_path,
        url,
        name="pinned.py",
        controller=f'Controller(on="node-a", image="{image}")',
    )
    body = json.loads(_init(ref).stdout)
    names = [(c["kind"], c["name"]) for c in body["changes"]]
    assert names.index(("Deployment", "piceli-ui")) > names.index(
        ("ClusterRole", "piceli-gitops")
    )  # the UI after the controller's objects
    assert names.count(("ConfigMap", "piceli-gitops-requests")) == 1
    assert _init(ref, "--approve", body["plan_hash"]).exit_code == 0
    status = json.loads(_run("cluster", "status", ref, "--json").stdout)
    assert status["ui"]["installed"] is True
    assert status["ui"]["health"] in {"healthy", "starting"}
    assert status["controller"]["health"] == "not-enabled"
    assert status["controller"]["last_poll"] is None
    node_a = status["nodes"][0]
    assert {k: node_a[k] for k in ("name", "arch", "roles")} == {
        "name": "node-a",
        "arch": "amd64",
        "roles": ["builder", "controller", "registry"],
    }
    assert node_a["mirror"]["kind"] == "containerd"
    assert node_a["mirror"]["state"] in {"ready", "pending", "missing"}
    assert api.objects[("Deployment", "piceli-ui")]["spec"]["template"]["spec"]
