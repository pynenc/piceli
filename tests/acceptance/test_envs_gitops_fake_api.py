"""Acceptance: one GitOps controller poll deploys a branch with the real ``env_up``.

A local bare repository holds the pipeline; the fake API holds the branch's
namespace. The controller prepares the namespace (its RoleBinding), reads
the digests ``piceli env push`` recorded for the commit, and ``env_up``
(approved by ``EnvConfig(auto_approve=True)``) adopts the namespace and
deploys the branch isolated. ``piceli envs`` then shows it.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

from typer.testing import CliRunner

from piceli.gitops.config import ControllerConfig
from piceli.gitops.controller import Controller
from piceli.gitops.ports import DefaultPorts
from piceli.gitops.repo import GitRemote
from piceli.gitops.state import DirectoryChannel
from piceli.k8s.cli import app as cli
from piceli.k8s.cli.env_push import configmap_name
from piceli.testing import TYPES, FakeAPI, manifest, serve, write_kubeconfig
from tests.unit.test_gitops_controller import Repo

BRANCH = "wp-login"
BRANCH_NS = "shop-wp-login"
DIGEST = "sha256:" + "5" * 64

PIPELINE = """
from piceli import App, Build, EnvConfig, NodeLoopbackRegistry, Pipeline, Target

images = Build.spec("build.toml")
app = App("shop")
app.deployment("api", image=images["api"], ports=[8080])

pipeline = Pipeline(
    app,
    Target.kubeconfig(
        "kubeconfig", context="author", namespace="shop", transport="loopback-http",
        nodes={"primary": "node-a"},
    ),
    build=images,
    deliver=NodeLoopbackRegistry(),
    execution={"max_seconds": 30, "readiness_seconds": 1, "poll_seconds": 0.05},
    envs=EnvConfig(prefix="shop-", branches=["wp-*"], auto_approve=True),
)
"""


def test_controller_poll_deploys_a_branch_through_env_up(tmp_path: Path) -> None:
    repo = Repo(tmp_path)
    repo.git("checkout", "--quiet", "-B", BRANCH, "main")
    (repo.work / "deploy" / "app.py").write_text(textwrap.dedent(PIPELINE))
    repo.git("add", "-A")
    repo.git("commit", "--quiet", "-m", "pipeline")
    sha = repo.git("rev-parse", "HEAD")
    repo.git("push", "--quiet", "origin", BRANCH)

    api = FakeAPI(
        types={**TYPES, "resourcequotas": ("v1", "ResourceQuota", True)},
        namespace=BRANCH_NS,
    )
    del api.objects[("Namespace", BRANCH_NS)]
    api.add_node("node-a")
    pushed = manifest("ConfigMap", configmap_name(BRANCH))
    pushed["data"] = {"images": json.dumps({"api": {"digest": DIGEST}}), "commit": sha}
    api.put(pushed)
    with serve(api) as (api, url):
        kubeconfig = write_kubeconfig(
            url, tmp_path / "controller.kubeconfig", context="fake"
        )
        config = ControllerConfig(
            pipeline="deploy/app.py:pipeline",
            repo=str(repo.remote),
            branches=("main", "wp-*"),
        )
        state = tmp_path / "state"
        controller = Controller(
            config,
            state_dir=state,
            source=GitRemote(config.repo, state / "mirror"),
            ports=DefaultPorts(
                Path(kubeconfig),
                "fake",
                state,
                namespace="piceli-system",
                config=config,
                transport="loopback-http",
            ),
            channel=DirectoryChannel(state),
        )
        status = controller.poll_once()
        entry = status["envs"][BRANCH]
        assert entry["state"] == "deployed", entry
        assert entry["namespace"] == BRANCH_NS and entry["deployed_commit"] == sha
        labels = api.objects[("Namespace", BRANCH_NS)]["metadata"]["labels"]
        assert labels["piceli.io/env-of"] == "shop"
        assert ("RoleBinding", "piceli-gitops-deployer") in api.objects
        assert ("NetworkPolicy", "piceli-env-isolation") in api.objects
        container = api.objects[("Deployment", "api")]["spec"]["template"]["spec"][
            "containers"
        ][0]
        assert container["image"] == f"127.0.0.1:5000/shop/api@{DIGEST}"
        record = json.loads(api.objects[("ConfigMap", "piceli-env")]["data"]["record"])
        assert record["commit"] == sha and record["build"] == "prebuilt"

        # `piceli envs` answers what runs where (the author's kubeconfig).
        write_kubeconfig(url, repo.work / "deploy" / "kubeconfig", context="author")
        result = CliRunner().invoke(
            cli,
            [
                "envs",
                "--json",
                "--pipeline",
                f"{repo.work / 'deploy' / 'app.py'}:pipeline",
            ],
        )
        assert result.exit_code == 0, result.output
        rows = {row["branch"]: row for row in json.loads(result.stdout)["envs"]}
        assert rows[BRANCH]["commit"] == sha and rows[BRANCH]["health"] == "healthy"


def test_a_branch_with_allow_api_may_reach_the_api_server(tmp_path: Path) -> None:
    """The isolation allows the API server's endpoints, read from the cluster."""
    repo = Repo(tmp_path)
    repo.git("checkout", "--quiet", "-B", BRANCH, "main")
    (repo.work / "deploy" / "app.py").write_text(
        textwrap.dedent(PIPELINE).replace(
            "auto_approve=True)", "auto_approve=True, allow_api=True)"
        )
    )
    repo.git("add", "-A")
    repo.git("commit", "--quiet", "-m", "pipeline")
    sha = repo.git("rev-parse", "HEAD")
    repo.git("push", "--quiet", "origin", BRANCH)

    api = FakeAPI(
        types={
            **TYPES,
            "resourcequotas": ("v1", "ResourceQuota", True),
            # Served outside the fake's one namespace (it lives in default).
            "endpointslices": ("discovery.k8s.io/v1", "EndpointSlice", False),
        },
        namespace=BRANCH_NS,
    )
    del api.objects[("Namespace", BRANCH_NS)]
    api.add_node("node-a")
    pushed = manifest("ConfigMap", configmap_name(BRANCH))
    pushed["data"] = {"images": json.dumps({"api": {"digest": DIGEST}}), "commit": sha}
    api.put(pushed)
    slice_ = {
        "apiVersion": "discovery.k8s.io/v1",
        "kind": "EndpointSlice",
        "metadata": {"name": "kubernetes", "namespace": "default"},
    }
    slice_.update(
        {
            "addressType": "IPv4",
            "endpoints": [
                {"addresses": ["192.0.2.10"], "conditions": {"ready": True}},
                {"addresses": ["192.0.2.11"], "conditions": {"ready": True}},
            ],
            "ports": [{"name": "https", "port": 6443, "protocol": "TCP"}],
        }
    )
    api.put(slice_)
    with serve(api) as (api, url):
        kubeconfig = write_kubeconfig(
            url, tmp_path / "controller.kubeconfig", context="fake"
        )
        config = ControllerConfig(
            pipeline="deploy/app.py:pipeline",
            repo=str(repo.remote),
            branches=("main", "wp-*"),
        )
        state = tmp_path / "state"
        controller = Controller(
            config,
            state_dir=state,
            source=GitRemote(config.repo, state / "mirror"),
            ports=DefaultPorts(
                Path(kubeconfig),
                "fake",
                state,
                namespace="piceli-system",
                config=config,
                transport="loopback-http",
            ),
            channel=DirectoryChannel(state),
        )
        entry = controller.poll_once()["envs"][BRANCH]
        assert entry["state"] == "deployed", json.dumps(entry)
        policy = api.objects[("NetworkPolicy", "piceli-env-isolation")]
        assert {
            "to": [
                {"ipBlock": {"cidr": "192.0.2.10/32"}},
                {"ipBlock": {"cidr": "192.0.2.11/32"}},
            ],
            "ports": [{"protocol": "TCP", "port": 6443}],
        } in policy["spec"]["egress"]
