"""0.22: build hosts deliver into the nodes (``Controller(delivery="nodes")``).

The controller names every declared node and its platform in the wanted
build; ``piceli builder serve`` imports each image into each node's
containerd (here the real validating relay and node verification of
:class:`~piceli.artifacts.delivery.NodeDelivery`, behind a fake SSH
transport), the archive of the node's architecture, and answers with each
node's receipt. The controller accepts the answer only when every node got
its own platform's config digest under the content-tagged name, and then
deploys it like any build.
"""

from __future__ import annotations

import copy
import time
from pathlib import Path
from typing import Any

import pytest

from piceli.artifacts.delivery import ArchiveSource, DeliveryGrant, NodeDelivery
from piceli.artifacts.node_transport import NodeTarget
from piceli.artifacts.process import ToolPin
from piceli.gitops.state import DirectoryChannel, built_request
from piceli.infra import Cluster, Controller, Node
from piceli.infra.cluster import ClusterError
from piceli.infra.composition import load_composition
from piceli.infra.controller import CompositionConfig, CompositionController
from piceli.infra.remote_builds import NODE_DOMAIN, node_identity, node_reference
from piceli.infra.sources import SourceSet
from tests.unit.infra.git_support import Repo, example_files
from tests.unit.infra.test_controller import EXAMPLE, FakePorts
from tests.unit.test_node_delivery import FakeNode, docker_archive

AMD = "ssh://ops@10.0.0.1?runtime=k3s-containerd"
ARM = "ssh://ops@10.0.0.2:2222?runtime=containerd&sudo=false"
NODES = (
    f'nodes=[Node("my-cluster-worker", arch="amd64", target="{AMD}"), '
    f'Node("my-cluster-worker2", arch="arm64", target="{ARM}")],\n    '
)


def _world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registry: bool) -> Any:
    shop = Repo(tmp_path / "remotes", "shop", example_files(EXAMPLE / "shop"))
    catalog = Repo(tmp_path / "remotes", "catalog", example_files(EXAMPLE / "catalog"))
    catalog.tag("v0.1.0")
    monkeypatch.setenv("COMPOSITION_SHOP_URL", shop.url)
    monkeypatch.setenv("COMPOSITION_CATALOG_URL", catalog.url)
    text = (EXAMPLE / "infra.py").read_text()
    text = text.replace(
        "from piceli.infra import Cluster, Component, Controller, Source",
        "from piceli.infra import Cluster, Component, Controller, Node, Source",
    ).replace(
        "controller=Controller(on=REGISTRY_NODE)",
        NODES + 'controller=Controller(on=REGISTRY_NODE, builds=("hosts",), '
        'delivery="nodes")',
    )
    if not registry:
        line = text[text.index("    registry=Registry.in_cluster(") :]
        text = text.replace(line[: line.index("\n") + 1], "")
    (tmp_path / "infra.py").write_text(text)
    composition = load_composition(tmp_path / "infra.py")
    config = CompositionConfig(
        composition=composition.to_dict(),
        poll_seconds=10,
        platforms=("linux/amd64", "linux/arm64"),
    )
    state = tmp_path / "state"
    ports = FakePorts()
    channel = DirectoryChannel(state)
    controller = CompositionController(
        config,
        state_dir=state,
        sources=SourceSet(composition.sources, state / "sources"),
        ports=ports,
        channel=channel,
        clock=lambda: 1_000_000.0,
    )
    return controller, ports, channel, config


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    return _world(tmp_path, monkeypatch, registry=True)


CONFIGS = {"linux/amd64": "sha256:" + "a" * 64, "linux/arm64": "sha256:" + "b" * 64}
NODE_PLATFORMS = {
    "my-cluster-worker": "linux/amd64",
    "my-cluster-worker2": "linux/arm64",
}


def _answer(wanted: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """What an honest host answers: every node, its platform's config."""
    found = {}
    for item in wanted["components"]:
        reference = node_reference(item["repository"], CONFIGS)
        found[item["component"]] = {
            "key": item["digest"],
            "delivery": "nodes",
            "pull_ref": reference,
            "manifest_digest": node_identity(CONFIGS),
            "platforms": dict(CONFIGS),
            "nodes": {
                node: {
                    "platform": platform,
                    "result": "imported",
                    "reference": reference,
                    "config_digest": CONFIGS[platform],
                }
                for node, platform in NODE_PLATFORMS.items()
            },
        }
    return found


def test_the_wanted_build_names_every_node_and_its_platform(world: Any) -> None:
    controller, _, _, config = world
    (wanted,) = controller.poll_once()["controller"]["builds_wanted"]
    assert wanted["deliver"] == {"mode": "nodes", "nodes": NODE_PLATFORMS}
    assert wanted["platforms"] == ["linux/amd64", "linux/arm64"]
    # The controller's config keeps the nodes, never the SSH user.
    nodes = config.composition["cluster"]["nodes"]
    assert [item["target"] for item in nodes] == [
        "ssh://10.0.0.1?runtime=k3s-containerd&namespace=k8s.io&sudo=true",
        "ssh://10.0.0.2:2222?runtime=containerd&namespace=k8s.io&sudo=false",
    ]
    assert "ops@" not in str(config.to_dict())


def test_an_answer_with_every_node_deploys(world: Any) -> None:
    controller, ports, channel, _ = world
    (wanted,) = controller.poll_once()["controller"]["builds_wanted"]
    channel.add_request(*built_request(wanted["id"], "mini", _answer(wanted), {}))
    status = controller.poll_once()
    main = status["envs"]["main"]
    assert main["state"] == "deployed", main
    assert main["components"]["web"]["digest"] == node_identity(CONFIGS)
    _, images = ports.deployed[-1]
    assert images["web"].startswith(f"{NODE_DOMAIN}/shop/web:sha256-")
    assert images["web"] == node_reference("shop/web", CONFIGS)
    assert ports.builder.built == []


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda image: image["nodes"].pop("my-cluster-worker2"),
         "not delivered into node my-cluster-worker2"),
        (lambda image: image["nodes"]["my-cluster-worker"].update(
            config_digest="sha256:" + "c" * 64), "holds config sha256:ccc"),
        (lambda image: image["nodes"]["my-cluster-worker2"].update(
            platform="linux/amd64", config_digest=CONFIGS["linux/amd64"]),
         "runs linux/arm64"),
        (lambda image: image["nodes"]["my-cluster-worker"].update(result="failed"),
         "ended failed"),
        (lambda image: image.update(pull_ref=image["pull_ref"].replace(
            NODE_DOMAIN, "registry.example")), "content tag"),
        (lambda image: image.pop("delivery"), "delivery into the nodes"),
    ],
)  # fmt: skip
def test_a_node_answer_that_does_not_prove_every_node_is_refused(
    world: Any, change: Any, reason: str
) -> None:
    controller, ports, channel, _ = world
    (wanted,) = controller.poll_once()["controller"]["builds_wanted"]
    images = copy.deepcopy(_answer(wanted))
    change(images["web"])
    channel.add_request(*built_request(wanted["id"], "mini", images, {}))
    status = controller.poll_once()
    assert status["envs"]["main"]["reason"] == "build-waiting"
    (rejected,) = status["rejected_requests"]
    assert rejected["kind"] == "built" and reason in rejected["message"], rejected
    assert status["controller"]["builds_wanted"]  # still waiting for a good one
    assert ports.deployed == []


def test_a_registry_answer_is_refused_when_nodes_were_asked(world: Any) -> None:
    controller, _, channel, _ = world
    (wanted,) = controller.poll_once()["controller"]["builds_wanted"]
    images = {
        name: {
            "key": image["key"],
            "pull_ref": f"registry.example:5000/x@sha256:{'d' * 64}",
            "manifest_digest": "sha256:" + "d" * 64,
            "platforms": {},
        }
        for name, image in _answer(wanted).items()
    }
    channel.add_request(*built_request(wanted["id"], "mini", images, {}))
    status = controller.poll_once()
    assert "delivery into the nodes" in status["rejected_requests"][0]["message"]


def test_a_node_whose_platform_is_not_built_fails_the_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, ports, *_ = _world(tmp_path, monkeypatch, registry=True)
    controller.config = CompositionConfig(
        composition=controller.config.composition,
        poll_seconds=10,
        platforms=("linux/amd64",),
    )
    main = controller.poll_once()["envs"]["main"]
    assert main["reason"] == "composition-invalid"
    assert main["failed_stage"] == "build" and ports.deployed == []


# ------------------------------------------------------------ builder serve


class FakeNodes:
    """``Backend.node_deliver`` with the real NodeDelivery behind a fake
    transport: one emulated containerd per node address."""

    def __init__(self, ssh: ToolPin) -> None:
        self.ssh = ssh
        self.nodes = {"10.0.0.1": FakeNode(), "10.0.0.2": FakeNode()}
        self.agents: list[Path | None] = []

    def node_deliver(
        self,
        url: str,
        image_id: str,
        reference: str,
        *,
        archive: Path,
        ssh_agent_socket: Path | None = None,
    ) -> dict[str, Any]:
        self.agents.append(ssh_agent_socket)
        target = NodeTarget.parse(url)
        delivery = NodeDelivery(
            ssh=self.ssh,
            ssh_agent_socket=ssh_agent_socket,
            runner=self.nodes[target.address],
        )
        return delivery.deliver(
            ArchiveSource(archive.absolute()),
            target,
            DeliveryGrant(image_id, url, time.time() + 60),
            reference=reference,
        )


def _container(tamper: dict[str, str] | None = None) -> Any:
    """A builder container that writes one docker archive per image and
    platform (another content per architecture) and its receipt."""
    from piceli.artifacts.cluster_build import encode_receipt

    def run(argv: list[str], sources: Any, out: Path, line: Any) -> int:
        import json

        archives = []
        for spec in (a for a in argv if a.startswith("{")):
            item = json.loads(spec)
            for platform in ("linux/amd64", "linux/arm64"):
                arch = platform.split("/")[1]
                body, config = docker_archive(
                    f"{item['component']}-{arch}".encode(), architecture=arch
                )
                relative = f"{platform.replace('/', '-')}/{item['component']}.tar"
                (out / relative).parent.mkdir(parents=True, exist_ok=True)
                (out / relative).write_bytes(body)
                archives.append(
                    {
                        "archive": relative,
                        "platform": platform,
                        "repository": item["repository"],
                        "image_id": (tamper or {}).get(relative, config),
                        "digest": "sha256:" + "f" * 64,
                    }
                )
        line(encode_receipt({"components": {}, "archives": archives}))
        return 0

    return run


@pytest.fixture
def ssh_pin(tmp_path: Path) -> ToolPin:
    tool = tmp_path / "ssh"
    tool.write_bytes(b"#!/bin/sh\nexit 0\n")
    return ToolPin.capture(tool)


def _serve(
    world: Any, tmp_path: Path, nodes: FakeNodes, container: Any, push: Any = None
) -> Any:
    from piceli.infra.builder_serve import BuilderServe, node_importer

    controller, _, channel, _ = world
    controller.poll_once()
    return BuilderServe(
        host="mini",
        image="ghcr.io/x/builder@sha256:" + "e" * 64,
        channel=channel,
        container=container,
        push=push,
        index=None,
        root=tmp_path / "builder",
        sleep=lambda _s: controller.poll_once(),
        node_import=node_importer(
            {"my-cluster-worker": AMD, "my-cluster-worker2": ARM},
            nodes,
            Path("/run/agent.sock"),
        ),
    )


def test_builder_serve_imports_each_nodes_platform_and_the_controller_deploys(
    world: Any, tmp_path: Path, ssh_pin: ToolPin
) -> None:
    controller, ports, _, _ = world
    nodes = FakeNodes(ssh_pin)

    def push(*_args: Any) -> str:
        raise AssertionError("nothing goes to a registry")

    worker = _serve(world, tmp_path, nodes, _container(), push=push)
    assert worker.once() is not None
    status = controller.poll_once()
    main = status["envs"]["main"]
    assert main["state"] == "deployed", status.get("rejected_requests")
    _, images = ports.deployed[-1]
    amd, arm = nodes.nodes["10.0.0.1"], nodes.nodes["10.0.0.2"]
    # Each node holds every image under the deployed name, its own platform.
    for component in ("api", "catalog", "web"):
        reference = images[component]
        assert reference.startswith(f"{NODE_DOMAIN}/shop/{component}:sha256-")
        assert reference in amd.names and reference in arm.names
    assert len(amd.imports) == 3 and len(arm.imports) == 3
    assert all(b"amd64" in body and b"arm64" not in body for body in amd.imports)
    assert all(b"arm64" in body and b"amd64" not in body for body in arm.imports)
    # Over SSH with the host's agent, k3s ctr on one node, ctr on the other.
    assert set(nodes.agents) == {Path("/run/agent.sock")}
    assert any(c[-1] == "sudo -n k3s ctr -n k8s.io images import -" for c in amd.calls)
    assert any(c[-1] == "ctr -n k8s.io images import -" for c in arm.calls)
    assert worker.once() is None


def test_an_image_whose_config_does_not_match_fails_on_the_host(
    world: Any, tmp_path: Path, ssh_pin: ToolPin
) -> None:
    """The relay withholds the index of an archive that is not the approved
    config: the node creates nothing and the host answers build-failed."""
    controller, ports, _, _ = world
    nodes = FakeNodes(ssh_pin)
    tamper = {"linux-arm64/web.tar": "sha256:" + "0" * 64}
    worker = _serve(world, tmp_path, nodes, _container(tamper))
    said: list[str] = []
    worker.say = said.append
    worker.once()
    main = controller.poll_once()["envs"]["main"]
    assert main["reason"] == "component-build-failed", main
    assert "into node my-cluster-worker2 ended rejected (digest-mismatch)" in said[-1]
    assert ports.deployed == []
    assert not any("/shop/web:" in name for name in nodes.nodes["10.0.0.2"].names)


def test_without_a_registry_the_controller_still_builds_on_hosts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ssh_pin: ToolPin
) -> None:
    world = _world(tmp_path, monkeypatch, registry=False)
    controller, ports, _, config = world
    assert config.composition["cluster"]["registry"] is None
    worker = _serve(world, tmp_path, FakeNodes(ssh_pin), _container())
    assert worker.once() is not None
    assert controller.poll_once()["envs"]["main"]["state"] == "deployed"


# ------------------------------------------------------------ declaration


def _cluster(**options: Any) -> Cluster:
    nodes = options.pop(
        "nodes",
        [Node("n1", arch="amd64", target=AMD), Node("n2", arch="arm64", target=ARM)],
    )
    return Cluster(
        "c", api="https://10.0.0.1:6443", credentials="c", nodes=nodes, **options
    )


def test_the_declaration_needs_hosts_and_every_nodes_target() -> None:
    cluster = _cluster(
        controller=Controller(on="n1", builds=("hosts",), delivery="nodes")
    )
    described = cluster.describe()
    assert described["controller"]["delivery"] == "nodes"
    assert "ops" not in str(described)
    with pytest.raises(ClusterError, match="builds="):
        Controller(on="n1", delivery="nodes")
    with pytest.raises(ClusterError, match=r"is .registry. or .nodes."):
        Controller(on="n1", builds=("hosts",), delivery="ftp")  # type: ignore[arg-type]
    with pytest.raises(ClusterError, match="node n2 has no target"):
        _cluster(
            nodes=[Node("n1", arch="amd64", target=AMD), Node("n2", arch="arm64")],
            controller=Controller(on="n1", builds=("hosts",), delivery="nodes"),
        )
    with pytest.raises(ClusterError, match="falls back"):
        _cluster(
            controller=Controller(
                on="n1", builds=("hosts", "cluster"), delivery="nodes"
            )
        )
    with pytest.raises(ClusterError, match="target="):
        Node("n3", arch="amd64", target="ssh://10.0.0.3")  # no runtime
    # The registry delivery (default) keeps its description (same hashes).
    plain = _cluster(controller=Controller(on="n1", builds=("hosts",)))
    assert "delivery" not in plain.describe()["controller"]
