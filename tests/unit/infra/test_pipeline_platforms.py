"""0.68.0: a pipeline environment builds only the platforms its pods can run on.

The nodes its pods can be placed on (:func:`placement`) are the
environment's ``on_nodes``, else the nodes every workload's node selector
matches, else every declared node. The build compiles the enabled platforms
of those nodes only (:func:`build_platforms`), a delivery into the nodes
goes only into them, and only their platforms must be enabled.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from piceli.app import App, PodDefaults
from piceli.infra import CompositionError, Node
from piceli.infra.builders import JobSettings, spec_job
from piceli.infra.cluster_builds import ClusterBuilds
from piceli.infra.pipelines import SpecBuildRequest, build_platforms, placement
from piceli.infra.remote_builds import STATE_KEY, BuildPending, HostBuilds

BOTH = ("linux/amd64", "linux/arm64")
IMAGE = "example/web@sha256:" + "1" * 64
NODES = (
    Node("builder-1", arch="amd64", roles=("builder",)),
    Node("arm-1", arch="arm64", roles=("web",)),
    Node("arm-2", arch="arm64"),
)
ARM = {"kubernetes.io/arch": "arm64"}


def _env(app: App | None, on_nodes: Any = None) -> Any:
    return SimpleNamespace(on_nodes=on_nodes, pipeline=SimpleNamespace(app=app))


def _app(**defaults: Any) -> App:
    return App("shop", pod_defaults=PodDefaults(**defaults) if defaults else None)


def test_pods_that_select_an_architecture_need_only_its_nodes() -> None:
    app = _app()
    app.deployment("web", image=IMAGE, node_selector=ARM)
    app.daemon_set("agent", image=IMAGE, node_selector=ARM)
    placed = placement(_env(app), NODES)
    assert placed == ("arm-1", "arm-2")
    assert build_platforms(BOTH, NODES, placed) == ("linux/arm64",)


def test_the_apps_pod_defaults_count_as_each_workloads_selector() -> None:
    app = _app(node_selector=ARM)
    app.deployment("web", image=IMAGE)
    assert placement(_env(app), NODES) == ("arm-1", "arm-2")


def test_one_workload_without_a_selector_needs_every_node() -> None:
    app = _app()
    app.deployment("web", image=IMAGE, node_selector=ARM)
    app.deployment("api", image=IMAGE)
    assert placement(_env(app), NODES) is None
    assert build_platforms(BOTH, NODES, None) == BOTH
    # A delivery into the nodes builds every declared node's platform.
    assert build_platforms(BOTH, NODES, None, every_node=True) == BOTH
    arm_only = NODES[1:]
    assert build_platforms(BOTH, arm_only, None, every_node=True) == ("linux/arm64",)


def test_an_unknown_label_is_ignored_and_a_role_label_selects() -> None:
    app = _app()
    app.deployment("web", image=IMAGE, node_selector={"tier": "edge"})
    assert placement(_env(app), NODES) is None  # cannot be told: every node
    app = _app()
    app.deployment("web", image=IMAGE, node_selector={"piceli.io/role-web": "true"})
    assert placement(_env(app), NODES) == ("arm-1",)


def test_on_nodes_places_every_workload_but_a_daemon_set() -> None:
    app = _app()
    app.deployment("web", image=IMAGE)
    assert placement(_env(app, on_nodes=["arm-2"]), NODES) == ("arm-2",)
    assert placement(_env(app, on_nodes=ARM), NODES) == ("arm-1", "arm-2")
    app.daemon_set("agent", image=IMAGE)  # runs where its own selector says
    assert placement(_env(app, on_nodes=["arm-2"]), NODES) is None
    app = _app()
    app.deployment("web", image=IMAGE)
    app.daemon_set(
        "agent", image=IMAGE, node_selector={"kubernetes.io/hostname": "arm-1"}
    )
    assert placement(_env(app, on_nodes=["arm-2"]), NODES) == ("arm-1", "arm-2")
    # Node names the composition does not declare: every node.
    assert placement(_env(app, on_nodes=["other-1"]), NODES) is None


def test_an_override_of_the_node_selector_needs_every_node() -> None:
    app = _app()
    web = app.deployment("web", image=IMAGE, node_selector=ARM)
    app.override(web, {"spec": {"template": {"spec": {"nodeSelector": {}}}}})
    assert placement(_env(app), NODES) is None


def test_without_declared_nodes_every_enabled_platform_is_built() -> None:
    app = _app()
    app.deployment("web", image=IMAGE, node_selector=ARM)
    assert placement(_env(app), ()) is None
    assert build_platforms(BOTH, (), None, every_node=True) == BOTH
    # No needed platform enabled: the enabled ones (the delivery refuses).
    assert build_platforms(("linux/amd64",), NODES, ("arm-1",)) == ("linux/amd64",)


# ---------------------------------------------------------------- the hosts


def _request(**placement: Any) -> SpecBuildRequest:
    return SpecBuildRequest(
        "host-build.toml",
        "infra",
        {"infra": "1" * 40},
        {"web": {"repository": "shop/web", "key": "sha256:" + "2" * 64}},
        urls={"infra": "https://git.example.org/infra.git"},
        **placement,
    )


def _hosts(platforms: tuple[str, ...] = BOTH) -> tuple[HostBuilds, dict[str, Any]]:
    state: dict[str, Any] = {}
    builds = HostBuilds(
        object(),
        state,
        platforms=platforms,
        clock=lambda: 1_000.0,
        nodes={node.name: f"linux/{node.arch}" for node in NODES},
    )
    return builds, state


def _wanted(builds: HostBuilds, state: dict[str, Any], request: Any) -> dict[str, Any]:
    with pytest.raises(BuildPending):
        builds.build_spec(request, None)
    (entry,) = state[STATE_KEY].values()
    return dict(entry)


def test_the_hosts_build_and_deliver_only_the_placed_nodes_platforms() -> None:
    builds, state = _hosts()
    request = _request(platforms=("linux/arm64",), nodes=("arm-1", "arm-2"))
    entry = _wanted(builds, state, request)
    assert entry["platforms"] == ["linux/arm64"]
    assert entry["deliver"]["nodes"] == {"arm-1": "linux/arm64", "arm-2": "linux/arm64"}
    # The same request names the same build (a deterministic identity) ...
    again, other = _hosts()
    assert _wanted(again, other, request)["id"] == entry["id"]
    # ... and every node is another build.
    whole, kept = _hosts()
    everything = _wanted(whole, kept, _request())
    assert everything["platforms"] == list(BOTH)
    assert set(everything["deliver"]["nodes"]) == {"builder-1", "arm-1", "arm-2"}
    assert everything["id"] != entry["id"]


def test_only_a_needed_platform_that_is_not_enabled_is_refused() -> None:
    builds, state = _hosts(platforms=("linux/arm64",))
    request = _request(platforms=("linux/arm64",), nodes=("arm-1",))
    assert _wanted(builds, state, request)["platforms"] == ["linux/arm64"]
    builds, state = _hosts(platforms=("linux/amd64",))
    with pytest.raises(CompositionError, match="the nodes run linux/arm64"):
        builds.build_spec(_request(platforms=("linux/amd64",), nodes=("arm-1",)), None)


def test_the_cluster_fallback_builds_the_requests_platforms() -> None:
    jobs = ClusterBuilds(object(), {}, platforms=BOTH, clock=lambda: 0.0)
    narrow = _request(platforms=("linux/arm64",))
    assert jobs.key("spec", narrow) != jobs.key("spec", _request())
    assert jobs.key("spec", _request(platforms=BOTH)) == jobs.key("spec", _request())
    settings = JobSettings(
        image="registry.example:5000/piceli@sha256:" + "3" * 64,
        namespace="piceli-system",
        registry_url="oci://registry.example:5000",
        node_registry="registry.example:5000",
        platforms=BOTH,
    )
    command = spec_job(settings, narrow, {})["spec"]["template"]["spec"]["containers"][
        0
    ]["command"]
    assert command.count("--platform") == 1
    assert command[command.index("--platform") + 1] == "linux/arm64"
