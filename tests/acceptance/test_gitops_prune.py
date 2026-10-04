"""Acceptance: a GitOps sync deletes what the app no longer declares (0.14.7).

A local bare repository holds the pipeline; the fake API holds the
environment's namespace; ``env_up`` is real. The first commit declares
``api`` (a Deployment with its Service), ``worker`` (a Deployment, its
Service and a NetworkPolicy), ``db`` (a StatefulSet with a claim template)
and a retained claim ``scratch``. The second removes ``worker``, ``db`` and
``scratch`` and turns ``api`` into a StatefulSet of the same name behind the
same Service. One sync:

* plans the deletes in the same plan (and hash) as the new objects;
* applies the new objects, waits until they are ready, deletes the old ones
  (a workload's pods first: ``Foreground``), then runs the checks;
* keeps the claims (``scratch`` and the StatefulSet's ``data-db-0``) and
  lists them as ``kept_orphaned`` with the command deleting each;
* records ``deleted`` and ``kept_orphaned`` in the environment's status;
* never touches an object another owner wrote, or no owner at all.

The approval follows the apply rules: a branch with ``EnvConfig(auto_approve=
True)`` and main with the owner's policy delete without asking; main whose
policy denies ``prune`` asks, and the deletes are in the approved hash.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any

import pytest

from piceli.gitops.config import ControllerConfig
from piceli.gitops.controller import Controller
from piceli.gitops.ports import DefaultPorts
from piceli.gitops.repo import GitRemote
from piceli.gitops.state import DirectoryChannel, approve_request
from piceli.k8s.cli.env_push import configmap_name
from piceli.testing import TYPES, FakeAPI, manifest, serve, write_kubeconfig
from tests.unit.test_gitops_controller import Repo

BRANCH = "wp-login"
BRANCH_NS = "shop-wp-login"
DIGEST = "sha256:" + "5" * 64

PIPELINE = """
from piceli import (
    App, ApprovalPolicy, Build, Checks, ClaimTemplate, EnvConfig,
    NodeLoopbackRegistry, Pipeline, Target,
)

images = Build.spec("build.toml")
app = App("shop")
[APP]

pipeline = Pipeline(
    app,
    Target.kubeconfig(
        "kubeconfig", context="author", namespace="shop", transport="loopback-http",
        nodes={"primary": "node-a"},
    ),
    build=images,
    deliver=NodeLoopbackRegistry(),
    checks=[Checks.python("checks.py:probe")],
    rollback_on_failed_checks=True,
    execution={"max_seconds": 30, "readiness_seconds": 2, "poll_seconds": 0.05},
    envs=EnvConfig(prefix="shop-", branches=["wp-*"], auto_approve=True),
    auto_approve=ApprovalPolicy([POLICY]),
)
"""

FIRST = """
api = app.deployment("api", image=images["api"], ports=[8080])
app.service(api, port=8080)
worker = app.deployment("worker", image=images["api"], ports=[9000])
app.service(worker, port=9000)
app.network_policy(worker, allow_from=[api], ports=[9000])
app.stateful_set(
    "db", image=images["api"], ports=[5432],
    volumes={"/data": ClaimTemplate("data", size="1Gi")},
)
app.resource(
    "v1", "PersistentVolumeClaim", "scratch",
    {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "1Gi"}}},
)
"""

# ``api`` renamed to a StatefulSet of the same name behind the same Service.
SECOND = """
api = app.stateful_set(
    "api", image=images["api"], ports=[8080], headless=False, service_name="api",
)
app.service(api, port=8080)
"""

PROBE = """
import os
from pathlib import Path


def probe(context):
    with Path(os.environ["PRUNE_LOG"]).open("a") as log:
        log.write("check\\n")
    return not os.environ.get("PRUNE_FAIL") or "GET /health returned 500"
"""

#: Objects the app never owned: another owner's, and one without an owner.
FOREIGN = (
    ("Deployment", "other-app", "someone-else"),
    ("Service", "worker-twin", "someone-else"),
    ("ConfigMap", "hand-made", None),
)


def _push(repo: Repo, api: FakeAPI, branch: str, body: str, policy: str) -> str:
    repo.git("checkout", "--quiet", "-B", branch, "main")
    text = textwrap.dedent(PIPELINE).replace("[APP]", textwrap.dedent(body))
    (repo.work / "deploy" / "app.py").write_text(text.replace("[POLICY]", policy))
    (repo.work / "deploy" / "checks.py").write_text(textwrap.dedent(PROBE))
    repo.git("add", "-A")
    repo.git("commit", "--quiet", "-m", f"app on {branch}")
    sha = repo.git("rev-parse", "HEAD")
    repo.git("push", "--quiet", "--force", "origin", branch)
    repo.git("checkout", "--quiet", "main")
    pushed = manifest("ConfigMap", configmap_name(branch))
    pushed["data"] = {"images": json.dumps({"api": {"digest": DIGEST}}), "commit": sha}
    api.put(pushed)
    return sha


def _seed_foreign(api: FakeAPI) -> None:
    for kind, name, owner in FOREIGN:
        found = manifest(kind, name)
        found["metadata"]["namespace"] = api.namespace
        found["metadata"]["labels"] = {"app.kubernetes.io/part-of": "shop"}
        if owner is not None:
            found["metadata"]["annotations"] = {"piceli.io/owner": owner}
        api.put(found)


def _template_claim(api: FakeAPI) -> None:
    """What the StatefulSet controller creates from ``db``'s claim template."""
    claim = manifest("PersistentVolumeClaim", "data-db-0")
    claim["metadata"]["namespace"] = api.namespace
    claim["metadata"]["labels"] = {"app.kubernetes.io/name": "db"}
    claim["spec"] = {
        "accessModes": ["ReadWriteOnce"],
        "resources": {"requests": {"storage": "1Gi"}},
    }
    api.put(claim)


def _api(namespace: str) -> FakeAPI:
    api = FakeAPI(
        types={**TYPES, "resourcequotas": ("v1", "ResourceQuota", True)},
        namespace=namespace,
    )
    api.add_node("node-a")
    return api


def _controller(
    tmp_path: Path, repo: Repo, url: str, **config: Any
) -> tuple[Controller, DirectoryChannel]:
    kubeconfig = write_kubeconfig(
        url, tmp_path / "controller.kubeconfig", context="fake"
    )
    settings = ControllerConfig(
        pipeline="deploy/app.py:pipeline",
        repo=str(repo.remote),
        branches=("main", "wp-*"),
        **config,
    )
    state = tmp_path / "state"
    channel = DirectoryChannel(state)
    controller = Controller(
        settings,
        state_dir=state,
        source=GitRemote(settings.repo, state / "mirror"),
        ports=DefaultPorts(
            Path(kubeconfig),
            "fake",
            state,
            namespace="piceli-system",
            config=settings,
            transport="loopback-http",
        ),
        channel=channel,
        log=print,
    )
    return controller, channel


def _record(api: FakeAPI, log: Path) -> None:
    """Log every write, delete and workload read in order (checks log too)."""

    def intercept(request: dict[str, Any], phase: str) -> bool:
        if phase != "committed":
            return False
        path = str(request["path"]).split("?")[0]
        method = request["method"]
        if method == "GET" and not path.endswith(
            ("/statefulsets/api", "/deployments/api")
        ):
            return False
        if method in {"GET", "PATCH", "POST", "DELETE"}:
            with log.open("a") as handle:
                handle.write(f"{method} {path}\n")
        return False

    api.intercept = intercept


def _deleted(api: FakeAPI) -> dict[tuple[str, str], str]:
    """``(plural, name) -> propagationPolicy`` of every real delete request."""
    found = {}
    for request in api.requests:
        body = request.get("body") or {}
        if request["method"] != "DELETE" or body.get("dryRun"):
            continue
        parts = str(request["path"]).split("?")[0].rstrip("/").split("/")
        found[(parts[-2], parts[-1])] = body.get("propagationPolicy")
    return found


EXPECTED_DELETES = {
    ("deployments", "worker"): "Foreground",
    ("deployments", "api"): "Foreground",
    ("statefulsets", "db"): "Foreground",
    ("services", "worker"): "Orphan",
    ("services", "db"): "Orphan",
    ("networkpolicies", "worker-ingress"): "Orphan",
}
KEPT = {("PersistentVolumeClaim", "scratch"), ("PersistentVolumeClaim", "data-db-0")}


def _check_pruned(api: FakeAPI, entry: dict[str, Any], namespace: str) -> None:
    assert ("StatefulSet", "api") in api.objects
    assert ("Service", "api") in api.objects  # shared by the old and the new api
    for kind, name in (
        ("Deployment", "api"),
        ("Deployment", "worker"),
        ("Service", "worker"),
        ("NetworkPolicy", "worker-ingress"),
        ("StatefulSet", "db"),
        ("Service", "db"),
    ):
        assert (kind, name) not in api.objects, (kind, name)
    # The claims stay: listed, with the command that deletes each.
    assert {
        ("PersistentVolumeClaim", "scratch"),
        ("PersistentVolumeClaim", "data-db-0"),
    } <= set(api.objects)
    kept = {(item["kind"], item["name"]): item for item in entry["kept_orphaned"]}
    assert set(kept) == KEPT, entry["kept_orphaned"]
    assert kept["PersistentVolumeClaim", "data-db-0"]["command"] == (
        f"kubectl --namespace {namespace} delete persistentvolumeclaim data-db-0"
    )
    assert {item["why"] for item in kept.values()} == {"claim"}
    deleted = {(item["kind"], item["name"]) for item in entry["deleted"]}
    assert deleted == {
        ("Deployment", "api"),
        ("Deployment", "worker"),
        ("Service", "worker"),
        ("NetworkPolicy", "worker-ingress"),
        ("StatefulSet", "db"),
        ("Service", "db"),
    }
    # Nothing the app does not own was deleted, or even asked to be.
    for kind, name, _owner in FOREIGN:
        assert (kind, name) in api.objects, (kind, name)
    assert _deleted(api) == EXPECTED_DELETES


def test_a_branch_sync_prunes_what_the_app_no_longer_declares(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "order.log"
    monkeypatch.setenv("PRUNE_LOG", str(log))
    repo = Repo(tmp_path)
    api = _api(BRANCH_NS)
    del api.objects[("Namespace", BRANCH_NS)]
    _push(repo, api, BRANCH, FIRST, "")
    with serve(api) as (api, url):
        controller, _ = _controller(tmp_path, repo, url)
        entry = controller.poll_once()["envs"][BRANCH]
        assert entry["state"] == "deployed", json.dumps(entry)
        assert entry["deleted"] == [] and entry["kept_orphaned"] == []
        assert ("Deployment", "worker") in api.objects
        _template_claim(api)
        _seed_foreign(api)
        api.requests.clear()
        log.write_text("")
        _record(api, log)

        second = _push(repo, api, BRANCH, SECOND, "")
        entry = controller.poll_once()["envs"][BRANCH]
        assert entry["state"] == "deployed", json.dumps(entry)
        assert entry["deployed_commit"] == second
        _check_pruned(api, entry, BRANCH_NS)
        order = log.read_text().splitlines()
        created = order.index(f"POST /apis/apps/v1/namespaces/{BRANCH_NS}/statefulsets")
        ready = max(
            i
            for i, line in enumerate(order)
            if line == f"GET /apis/apps/v1/namespaces/{BRANCH_NS}/statefulsets/api"
        )
        deletes = [i for i, line in enumerate(order) if line.startswith("DELETE ")]
        checks = [i for i, line in enumerate(order) if line == "check"]
        assert created < ready < min(deletes), order
        assert checks and max(deletes) < min(checks), order

        # The next sync of the same commit deletes nothing and still lists
        # the kept claims.
        api.requests.clear()
        from piceli.gitops.state import sync_request

        controller.channel.add_request(*sync_request(BRANCH))  # type: ignore[attr-defined]
        entry = controller.poll_once()["envs"][BRANCH]
        assert entry["state"] == "deployed", json.dumps(entry)
        assert entry["deleted"] == []
        assert {(i["kind"], i["name"]) for i in entry["kept_orphaned"]} == KEPT
        assert _deleted(api) == {}


@pytest.mark.parametrize("policy", ["", 'deny={"prune"}'], ids=["policy", "asks"])
def test_main_prunes_inside_its_policy_or_asks_with_the_deletes_in_the_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: str
) -> None:
    log = tmp_path / "order.log"
    monkeypatch.setenv("PRUNE_LOG", str(log))
    repo = Repo(tmp_path)
    api = _api("shop")
    with serve(api) as (api, url):
        controller, channel = _controller(tmp_path, repo, url, main_auto_approve=True)
        controller.poll_once()  # the baseline: no tag yet
        _push(repo, api, "main", FIRST, policy)
        repo.tag("v1")
        entry = controller.poll_once()["envs"]["main"]
        assert entry["state"] == "deployed", json.dumps(entry)
        _template_claim(api)
        _seed_foreign(api)
        api.requests.clear()

        _push(repo, api, "main", SECOND, policy)
        repo.tag("v2")
        entry = controller.poll_once()["envs"]["main"]
        if policy:
            # Outside the policy: nothing applied, the owner approves the
            # plan whose hash covers the deletes.
            assert entry["state"] == "approval-required", json.dumps(entry)
            assert entry["reason"] == "approval-policy-exceeded"
            assert ("Deployment", "worker") in api.objects
            assert ("StatefulSet", "api") not in api.objects
            assert _deleted(api) == {}
            planned = _plan_only(controller, url, tmp_path)
            assert planned["plan_hash"] == entry["plan_hash"]
            stage = planned["deploy"]["stages"]["plan"]
            deletes = {
                (c["kind"], c["name"])
                for c in stage["changes"]
                if c["operation"] == "delete"
            }
            assert ("Deployment", "worker") in deletes and all(
                c.get("prune") for c in stage["changes"] if c["operation"] == "delete"
            )
            assert {(i["kind"], i["name"]) for i in stage["kept_orphaned"]} == KEPT
            channel.add_request(*approve_request("main", entry["plan_hash"]))
            entry = controller.poll_once()["envs"]["main"]
        assert entry["state"] == "deployed", json.dumps(entry)
        _check_pruned(api, entry, "shop")


def _plan_only(controller: Controller, url: str, tmp_path: Path) -> dict[str, Any]:
    """The environment plan ``env_up`` would ask the owner to approve now."""
    from piceli.envs.ops import env_up

    with controller.source.checkout(
        controller._envs()["main"]["commit"], tmp_path / "plan"
    ) as tree:
        pipeline = controller.ports.load_pipeline(
            tree, controller.config.pipeline, controller.config.env
        )
        write_kubeconfig(url, tree / "deploy" / "kubeconfig", context="author")
        record = controller._envs()["main"]
        return env_up(
            pipeline,
            "main",
            plan_only=True,
            commit=record["commit"],
            digests={"api": {"digest": DIGEST}},  # type: ignore[dict-item]
        )


def test_a_failed_check_after_a_removal_rolls_back_without_recreating_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rollback restores only what both releases declare, then stops.

    Nothing the failed release removed comes back (by its prune or by hand),
    nothing it added is deleted, and the controller does not try the
    revision again (``checks-failed-rolled-back``) until ``gitops sync``.
    """
    log = tmp_path / "order.log"
    monkeypatch.setenv("PRUNE_LOG", str(log))
    repo = Repo(tmp_path)
    api = _api(BRANCH_NS)
    del api.objects[("Namespace", BRANCH_NS)]
    _push(repo, api, BRANCH, FIRST, "")
    with serve(api) as (api, url):
        controller, channel = _controller(tmp_path, repo, url)
        assert controller.poll_once()["envs"][BRANCH]["state"] == "deployed"
        # Removed by hand before the release that removes it from the app.
        del api.objects[("NetworkPolicy", "worker-ingress")]
        monkeypatch.setenv("PRUNE_FAIL", "1")
        _push(repo, api, BRANCH, SECOND, "")
        api.requests.clear()
        entry = controller.poll_once()["envs"][BRANCH]
        assert entry["state"] == "failed", json.dumps(entry)
        assert entry["reason"] == "checks-failed-rolled-back"
        assert entry["next_attempt_at"] is None
        for kind, name in (
            ("Deployment", "api"),
            ("Deployment", "worker"),
            ("StatefulSet", "db"),
            ("Service", "worker"),
            ("NetworkPolicy", "worker-ingress"),
        ):
            assert (kind, name) not in api.objects, (kind, name)  # not re-created
        assert ("StatefulSet", "api") in api.objects  # what it added stays
        assert ("Service", "api") in api.objects
        checks = log.read_text().count("check")

        # No loop: later polls apply, check and roll back nothing.
        api.requests.clear()
        for _ in range(3):
            controller.clock = lambda: 4_000_000_000.0  # far past any backoff
            assert controller.poll_once()["envs"][BRANCH]["state"] == "failed"
        writes = [r for r in api.requests if r["method"] in {"POST", "PATCH", "DELETE"}]
        assert writes == [] and log.read_text().count("check") == checks

        # `piceli gitops sync` tries the same revision again.
        monkeypatch.delenv("PRUNE_FAIL")
        from piceli.gitops.state import sync_request

        channel.add_request(*sync_request(BRANCH))
        entry = controller.poll_once()["envs"][BRANCH]
        assert entry["state"] == "deployed", json.dumps(entry)
