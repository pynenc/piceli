"""Several clusters from one composition controller (0.15), reproduced first.

One fake environment port per cluster (``for_cluster``), local Git sources,
the real controller: an environment placed on three edge clusters with a
canary rollout, an unreachable cluster, a failing canary, per-cluster
status and history, a cluster removed from the composition. A single-cluster
composition keeps its config (and so its hashes).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from piceli.envs import Environment, Placement, Rollout
from piceli.gitops.controller import ROLLED_BACK
from piceli.gitops.state import DirectoryChannel, approve_request, sync_request
from piceli.infra import Cluster, CompositionError, Node
from piceli.infra.composition import Composition, composition_from, load_composition
from piceli.infra.controller import CompositionConfig, CompositionController
from piceli.infra.sources import SourceSet
from piceli.pipeline.model import Registry
from tests.unit.infra.git_support import Repo, example_files
from tests.unit.infra.test_controller import EXAMPLE, FakeBuilder

HOME_REGISTRY = "piceli-registry.piceli-system.svc:5000"


def edge(name: str, node: str, registry: str = "piceli-registry") -> Cluster:
    return Cluster(
        name,
        api=f"https://100.64.0.{len(name)}:6443",
        credentials=name,
        nodes=[Node(node, arch="amd64", roles=["workloads", "registry"])],
        registry=Registry.in_cluster(
            on=node, repository="shop", storage="1Gi", name=registry
        ),
    )


class ClusterPorts:
    """One cluster's environment ports (a fake API of that cluster)."""

    def __init__(self, world: MultiPorts, name: str) -> None:
        self.world = world
        self.name = name
        self.builder = world.builder
        self.deployed: list[dict[str, Any]] = []
        self.removed: list[dict[str, Any]] = []
        self.namespaces: dict[str, list[str]] = {}

    def pipeline(self, composition: Any, env: Any, contracts: Any, images: Any) -> Any:
        return {
            "env": env.name,
            "namespace": env.namespace,
            "on_nodes": env.on_nodes,
            "settings": dict(env.settings),
            "images": dict(images),
        }

    def prepare_env(self, pipeline: Any, name: str) -> str:
        return str(pipeline["namespace"])

    def env_up(self, pipeline: Any, name: str, **kwargs: Any) -> dict[str, Any]:
        if self.name in self.world.down:
            raise ConnectionError("the API server went away")
        if self.name in self.world.failing:
            return {"state": "failed", "reason": ROLLED_BACK}
        if kwargs["approve"] is None:
            return {
                "state": "approval-required",
                "plan_hash": self.world.plan(self.name),
            }
        self.deployed.append(pipeline)
        self.world.order.append(self.name)
        return {"state": "ready", "namespace": pipeline["namespace"]}

    def env_down(self, pipeline: Any, name: str) -> None:
        self.removed.append({"env_down": name})

    def env_stop(self, pipeline: Any, name: str, reason: str = "idle") -> None:
        self.world.stopped.append((self.name, name, reason))

    def env_start(self, pipeline: Any, name: str) -> None:
        pass

    def fill_registry(
        self, refs: Mapping[str, str], *, home: Any, registry: Any, repositories: Any
    ) -> dict[str, str]:
        assert home.host == HOME_REGISTRY
        self.world.copied.setdefault(self.name, []).extend(sorted(refs))
        return {
            key: ref.replace(f"{HOME_REGISTRY}/", f"{registry.host}/")
            if ref.startswith(HOME_REGISTRY)
            else ref
            for key, ref in refs.items()
        }

    def remove_placement(self, *, namespace: str, app: str, env: str) -> dict[str, Any]:
        self.removed.append({"namespace": namespace, "app": app, "env": env})
        return {
            "deleted": [{"kind": "Deployment", "name": "web"}],
            "kept": [],
            "namespace": "deleted",
        }


class MultiPorts:
    """The controller's ports: the home cluster's and one fake per other cluster."""

    def __init__(self) -> None:
        self.builder = FakeBuilder()
        self.clusters: dict[str, ClusterPorts] = {}
        self.down: set[str] = set()
        self.failing: set[str] = set()
        self.order: list[str] = []
        self.stopped: list[tuple[str, str, str]] = []
        self.copied: dict[str, list[str]] = {}
        self.probes: list[str] = []
        self.manual = False

    def plan(self, cluster: str) -> str:
        return "sha256:" + format(abs(hash(cluster)) % 16, "x") * 64

    def cluster(self, name: str) -> ClusterPorts:
        return self.clusters.setdefault(name, ClusterPorts(self, name))

    # the home cluster's single-cluster ports (environments without clusters=)
    def pipeline(self, *args: Any) -> Any:
        return self.cluster("home").pipeline(*args)

    def prepare_env(self, pipeline: Any, name: str) -> str:
        return self.cluster("home").prepare_env(pipeline, name)

    def env_up(self, pipeline: Any, name: str, **kwargs: Any) -> Any:
        return self.cluster("home").env_up(pipeline, name, **kwargs)

    def env_down(self, pipeline: Any, name: str) -> None:
        self.cluster("home").env_down(pipeline, name)

    def env_stop(self, pipeline: Any, name: str, reason: str = "idle") -> None:
        self.cluster("home").env_stop(pipeline, name, reason)

    # several clusters (0.15)
    def for_cluster(self, name: str, *, home: bool) -> ClusterPorts:
        return self.cluster(name)

    def probe_cluster(self, name: str) -> dict[str, Any]:
        self.probes.append(name)
        if name in self.down:
            return {"reachable": False, "reason": "cluster-unreachable"}
        return {"reachable": True, "reason": None}


class _Builder(FakeBuilder):
    def build(self, items: Any, checkout: Any) -> Any:
        built = super().build(items, checkout)
        return {
            key: type(image)(
                image.pull_ref.replace("registry.example:5000", HOME_REGISTRY),
                image.manifest_digest,
            )
            for key, image in built.items()
        }

    def mirror(self, items: Any) -> Any:
        found = super().mirror(items)
        return {
            key: type(image)(
                image.pull_ref.replace("registry.example:5000", HOME_REGISTRY),
                image.manifest_digest,
            )
            for key, image in found.items()
        }


def _module(base: Composition, *, rollout: bool = True, extra: bool = True) -> Any:
    """The example composition plus an ``edge`` environment on three clusters."""
    main = base.environment("main")
    assert isinstance(main, Environment)
    canary, b, c = (
        edge("edge-canary", "edge-1"),
        edge("edge-b", "edge-2", "edge-registry"),
        edge("edge-c", "edge-3"),
    )
    placements: list[Any] = [
        Placement(canary, on_nodes=["edge-1"]),
        Placement(b, namespace="shop", values={"web": {"greeting": "hello from b"}}),
    ]
    if extra:
        placements.append(c)
    edge_env = Environment(
        "edge",
        namespace="shop-edge",
        stack=main.stack,
        follow={source: list(rules) for source, rules in main.sources},
        clusters=placements,
        rollout=Rollout(order=["edge-canary", "edge-*"]) if rollout else None,
        auto_approve=True,
    )
    return {
        "name": base.name,
        "home": base.cluster,
        "environments": [main, edge_env],
        **{f"c{index}": item for index, item in enumerate(base.components)},
    }


@pytest.fixture
def multi(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    shop = Repo(tmp_path / "remotes", "shop", example_files(EXAMPLE / "shop"))
    catalog = Repo(tmp_path / "remotes", "catalog", example_files(EXAMPLE / "catalog"))
    monkeypatch.setenv("COMPOSITION_SHOP_URL", shop.url)
    monkeypatch.setenv("COMPOSITION_CATALOG_URL", catalog.url)
    base = load_composition(EXAMPLE / "infra.py")
    composition = composition_from(_module(base))
    config = CompositionConfig(composition=composition.to_dict(), poll_seconds=10)
    state = tmp_path / "state"
    ports = MultiPorts()
    ports.builder = _Builder()
    channel = DirectoryChannel(state)
    clock = {"now": 1_000_000.0}
    controller = CompositionController(
        config,
        state_dir=state,
        sources=SourceSet(composition.sources, state / "sources"),
        ports=ports,
        channel=channel,
        clock=lambda: clock["now"],
    )
    return {
        "controller": controller,
        "ports": ports,
        "channel": channel,
        "shop": shop,
        "clock": clock,
        "base": base,
        "composition": composition,
        "tmp": tmp_path,
    }


def _clusters(status: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return dict(status["envs"]["edge"]["clusters"])


def _later(world: dict[str, Any], seconds: float = 3600) -> None:
    world["clock"]["now"] += seconds


# ---------------------------------------------------------------- model


def test_the_composition_declares_several_clusters(multi: dict[str, Any]) -> None:
    composition = multi["composition"]
    assert composition.cluster.name == "my-cluster"  # the one with Controller
    assert [c.name for c in composition.clusters] == [
        "my-cluster",
        "edge-canary",
        "edge-b",
        "edge-c",
    ]
    data = composition.to_dict()
    assert [item["name"] for item in data["clusters"]] == [
        "edge-canary",
        "edge-b",
        "edge-c",
    ]
    edge_dict = data["environments"][1]
    assert edge_dict["clusters"][0] == {
        "cluster": "edge-canary",
        "on_nodes": ["edge-1"],
    }
    assert edge_dict["rollout"] == {"order": ["edge-canary", "edge-*"]}
    # The plain data reads back: the controller never imports user code.
    again = Composition.from_dict(data)
    assert again.to_dict() == data
    assert [p.name for p in again.placements_of(again.environment("edge"))] == [
        "edge-canary",
        "edge-b",
        "edge-c",
    ]


def test_a_single_cluster_composition_keeps_its_config() -> None:
    import hashlib
    import json

    base = load_composition(EXAMPLE / "infra.py")
    data = base.to_dict()
    assert "clusters" not in data
    assert all(
        "clusters" not in env and "rollout" not in env for env in data["environments"]
    )
    assert all("replicas" not in env for env in data["environments"])
    # Golden: the example's config as 0.14.7 renders it (hash of its JSON).
    digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    assert digest == GOLDEN_SINGLE


def test_placement_and_rollout_refusals() -> None:
    from piceli.envs import EnvError

    a, b = edge("edge-a", "n-a"), edge("edge-b", "n-b")
    with pytest.raises(EnvError):
        Environment("x", namespace="x", clusters=[a, a])
    with pytest.raises(EnvError):
        Environment("x", namespace="x", clusters=[a], rollout=Rollout(order=["nope-*"]))
    with pytest.raises(EnvError):
        Environment("x", namespace="x", rollout=Rollout(order=["edge-a"]))
    with pytest.raises(EnvError):
        Environment("x", namespace="x", cluster=a, clusters=[b])
    with pytest.raises(EnvError):
        Placement(a, namespace="Bad_Name")
    with pytest.raises(CompositionError) as error:
        composition_from(
            {"environments": [Environment("x", namespace="x", clusters=[a, b])], "a": a}
        )
    assert "Controller" in str(error.value)


# ---------------------------------------------------------------- rollout


def test_the_canary_deploys_first_then_the_rest(multi: dict[str, Any]) -> None:
    controller, ports = multi["controller"], multi["ports"]
    status = controller.poll_once()
    assert status["envs"]["main"]["state"] == "deployed"
    assert "clusters" not in status["envs"]["main"]  # single-cluster record unchanged
    edge_order = [name for name in ports.order if name.startswith("edge")]
    assert edge_order[0] == "edge-canary"
    assert sorted(edge_order[1:]) == ["edge-b", "edge-c"]
    clusters = _clusters(status)
    assert {name: entry["state"] for name, entry in clusters.items()} == {
        "edge-canary": "deployed",
        "edge-b": "deployed",
        "edge-c": "deployed",
    }
    assert status["envs"]["edge"]["state"] == "deployed"
    # Each cluster runs its own placement: namespace, nodes, values.
    b = ports.cluster("edge-b").deployed[-1]
    assert b["namespace"] == "shop"
    assert b["settings"]["web"] == {"greeting": "hello from b"}
    canary = ports.cluster("edge-canary").deployed[-1]
    assert canary["namespace"] == "shop-edge" and canary["on_nodes"] == ("edge-1",)
    # Each pulls from its own registry: copied there by digest, never the home one.
    # (The same registry name on each cluster resolves to that cluster's own.)
    for name in ("edge-canary", "edge-b", "edge-c"):
        assert sorted(ports.copied[name]) == ["api", "cache", "catalog", "web"]
    images = ports.cluster("edge-b").deployed[-1]["images"]
    assert all(
        ref.startswith("edge-registry.piceli-system.svc:5000/shop/")
        for ref in images.values()
    ), images
    assert "home" not in ports.copied  # the home cluster pulls where it built
    # Built once, on the home cluster.
    assert len(ports.builder.built) == 1
    # The status JSON is additive: per cluster state, health, checks, revision,
    # reason, last contact.
    entry = clusters["edge-b"]
    for key in ("state", "health", "checks", "revision", "reason", "last_contact"):
        assert key in entry
    assert entry["last_contact"] is not None and entry["namespace"] == "shop"
    assert status["clusters"]["edge-b"]["reachable"] is True
    assert status["clusters"]["my-cluster"]["home"] is True


def test_a_failing_canary_stops_the_rollout(multi: dict[str, Any]) -> None:
    controller, ports = multi["controller"], multi["ports"]
    ports.failing.add("edge-canary")
    status = controller.poll_once()
    clusters = _clusters(status)
    assert clusters["edge-canary"]["state"] == "failed"
    assert clusters["edge-canary"]["reason"] == ROLLED_BACK
    for name in ("edge-b", "edge-c"):
        assert clusters[name]["state"] == "held"
        assert clusters[name]["reason"] == "rollout-stopped"
        assert clusters[name]["held_by"] == "edge-canary"
        assert not ports.cluster(name).deployed
    assert status["envs"]["edge"]["state"] == "failed"
    # No loop: later polls deploy nothing anywhere.
    for _ in range(3):
        _later(multi)
        controller.poll_once()
    assert not any(
        ports.cluster(n).deployed for n in ("edge-canary", "edge-b", "edge-c")
    )
    # A fixed revision rolls out again, canary first.
    ports.failing.clear()
    multi["shop"].commit({"web/index.html": "<h1>fixed</h1>\n"})
    status = controller.poll_once()
    assert {e["state"] for e in _clusters(status).values()} == {"deployed"}
    assert [n for n in ports.order if n.startswith("edge")][0] == "edge-canary"


def test_the_rest_waits_for_the_canary_to_pass(multi: dict[str, Any]) -> None:
    """Mutation target: "rest only after the canary's checks pass"."""
    controller, ports = multi["controller"], multi["ports"]
    ports.manual = True
    real = ports.cluster("edge-canary").env_up
    asked: list[str] = []

    def manual(pipeline: Any, name: str, **kwargs: Any) -> Any:
        asked.append(str(kwargs["approve"]))
        return {"state": "approval-required", "plan_hash": ports.plan("edge-canary")}

    ports.cluster("edge-canary").env_up = manual  # type: ignore[method-assign]
    status = controller.poll_once()
    clusters = _clusters(status)
    assert clusters["edge-canary"]["state"] == "approval-required"
    assert status["envs"]["edge"]["state"] == "approval-required"
    for name in ("edge-b", "edge-c"):
        assert clusters[name]["state"] == "pending"
        assert clusters[name]["reason"] == "rollout-waiting"
        assert not ports.cluster(name).deployed
    # The canary is approved and passes: the rest follows in the same poll.
    ports.cluster("edge-canary").env_up = real  # type: ignore[method-assign]
    multi["channel"].add_request(*approve_request("edge", ports.plan("edge-canary")))
    status = controller.poll_once()
    assert {e["state"] for e in _clusters(status).values()} == {"deployed"}
    assert [n for n in ports.order if n.startswith("edge")][0] == "edge-canary"


def test_a_degraded_canary_holds_the_rest(multi: dict[str, Any]) -> None:
    controller = multi["controller"]
    controller.poll_once()
    record = controller._envs()["edge@edge-canary"]
    record["health"] = "degraded"
    assert controller._gate({**controller._envs()["edge@edge-b"]}) == "stop:edge-canary"


# ---------------------------------------------------------------- reach


def test_an_unreachable_cluster_does_not_block_the_others(
    multi: dict[str, Any],
) -> None:
    controller, ports = multi["controller"], multi["ports"]
    controller.poll_once()
    first_contact = _clusters(controller.status())["edge-c"]["last_contact"]
    ports.down.add("edge-c")
    _later(multi, 60)
    multi["shop"].commit({"web/index.html": "<h1>v2</h1>\n"})
    before = len(ports.cluster("edge-c").deployed)
    status = controller.poll_once()
    clusters = _clusters(status)
    assert clusters["edge-canary"]["state"] == "deployed"
    assert clusters["edge-b"]["state"] == "deployed"
    assert clusters["edge-c"]["state"] == "unreachable"
    assert clusters["edge-c"]["reason"] == "cluster-unreachable"
    assert clusters["edge-c"]["last_contact"] == first_contact
    assert len(ports.cluster("edge-c").deployed) == before
    assert status["clusters"]["edge-c"]["reachable"] is False
    record = controller._envs()["edge@edge-c"]
    assert record["state"] == "retrying" and int(record.get("attempts") or 0) == 0
    # Never final: many polls later it still waits, then converges when back.
    for _ in range(8):
        _later(multi)
        controller.poll_once()
    assert controller._envs()["edge@edge-c"]["state"] == "retrying"
    ports.down.clear()
    _later(multi)
    status = controller.poll_once()
    clusters = _clusters(status)
    assert clusters["edge-c"]["state"] == "deployed"
    assert clusters["edge-c"]["revision"] == clusters["edge-canary"]["revision"]
    assert clusters["edge-c"]["last_contact"] != first_contact


def test_a_cluster_lost_mid_rollout_is_retried_not_failed(
    multi: dict[str, Any],
) -> None:
    controller, ports = multi["controller"], multi["ports"]
    original = ports.probe_cluster
    calls = {"n": 0}

    def flaky(name: str) -> dict[str, Any]:
        # Reachable at the poll's probe, gone during the deploy step.
        if name == "edge-b":
            calls["n"] += 1
            if calls["n"] == 1:
                ports.down.add("edge-b")
                return {"reachable": True, "reason": None}
        return original(name)

    ports.probe_cluster = flaky  # type: ignore[method-assign]
    status = controller.poll_once()
    clusters = _clusters(status)
    assert clusters["edge-b"]["state"] == "unreachable"
    assert controller._envs()["edge@edge-b"]["state"] == "retrying"
    assert clusters["edge-c"]["state"] == "deployed"
    ports.down.clear()
    _later(multi)
    assert _clusters(controller.poll_once())["edge-b"]["state"] == "deployed"


# ---------------------------------------------------------------- requests, history


def test_sync_and_stop_apply_to_every_cluster(multi: dict[str, Any]) -> None:
    from piceli.gitops.state import request

    controller, ports, channel = multi["controller"], multi["ports"], multi["channel"]
    controller.poll_once()
    channel.add_request(*sync_request("edge"))
    status = controller.poll_once()
    assert {e["state"] for e in _clusters(status).values()} == {"deployed"}
    assert {"edge-canary", "edge-b", "edge-c"} <= set(ports.order[-3:])
    channel.add_request(*request("stop", env="edge", via="cli"))
    status = controller.poll_once()
    assert {e["state"] for e in _clusters(status).values()} == {"stopped"}
    assert sorted(c for c, _, _ in ports.stopped) == ["edge-b", "edge-c", "edge-canary"]
    assert {name for _, name, _ in ports.stopped} == {"edge"}


def test_history_lists_each_clusters_runs(multi: dict[str, Any]) -> None:
    from tests.gitops_history_fixture import write_run

    controller, channel = multi["controller"], multi["channel"]
    status = controller.poll_once()
    assert "edge@edge-b" not in status["envs"]  # one entry per environment
    document = channel.read_history()
    assert document is not None
    assert "edge" in document["envs"] and "edge@edge-b" not in document["envs"]
    clusters = {run.get("cluster") for run in document["envs"]["edge"]["runs"]}
    assert clusters == {"edge-canary", "edge-b", "edge-c"}
    assert write_run  # the fixture module is importable for richer runs


# ---------------------------------------------------------------- removal


def test_removing_a_cluster_tears_down_only_there(multi: dict[str, Any]) -> None:
    controller, ports = multi["controller"], multi["ports"]
    controller.poll_once()
    smaller = composition_from(_module(multi["base"], extra=False))
    controller._composition = smaller
    status = controller.poll_once()
    assert ports.cluster("edge-c").removed == [
        {"namespace": "shop-edge", "app": "shop", "env": "edge"}
    ]
    assert not ports.cluster("edge-b").removed and not ports.cluster("home").removed
    assert "edge-c" not in _clusters(status)
    assert status["removals"][-1]["cluster"] == "edge-c"
    assert status["removals"][-1]["deleted"] == 1


GOLDEN_SINGLE = "20e5b5cfaec65f83bd121279506ad8f425f35cb18f5cbf0263d14c15b55d913f"


def test_the_status_names_the_cluster_at_work(multi: dict[str, Any]) -> None:
    """0.15.0: ``in_progress`` per cluster and on the environment, while it runs."""
    import json

    controller, ports = multi["controller"], multi["ports"]
    seen: list[dict[str, Any]] = []
    canary = ports.cluster("edge-canary")
    real = canary.env_up

    def watching(pipeline: Any, name: str, **kwargs: Any) -> Any:
        status = json.loads((controller.state_dir / "status.json").read_text())
        seen.append(status["envs"].get("edge") or {})
        return real(pipeline, name, **kwargs)

    canary.env_up = watching  # type: ignore[method-assign]
    status = controller.poll_once()
    busy = [env for env in seen if env.get("in_progress")]
    assert busy, "no status was published while the canary deployed"
    assert busy[0]["in_progress"]["cluster"] == "edge-canary"
    assert busy[0]["in_progress"]["action"] == "deploy"
    assert busy[0]["clusters"]["edge-canary"]["in_progress"]["action"] == "deploy"
    assert "in_progress" not in status["envs"]["edge"]
    assert all("in_progress" not in view for view in _clusters(status).values())
