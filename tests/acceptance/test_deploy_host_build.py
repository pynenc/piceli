"""Acceptance: ``piceli deploy`` with a host build (``Build.spec(builder="host")``).

The build runs on the host (a declared ``sh`` stands in for the toolchain),
the image is an OCI archive and delivery pushes it by digest to an
in-process registry. No docker is reachable: the backend refuses to find it.
Node facts come from the fake API's Node.
"""

from __future__ import annotations

import json
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from piceli.artifacts.node_facts import PAGE_SIZE_LABEL
from piceli.pipeline.backend import Backend
from tests.acceptance.fake_api import TARGET, serve
from tests.acceptance.test_deploy_pipeline import deploy
from tests.unit.host_build_support import publish_base, write_project
from tests.unit.test_registry_delivery import FakeRegistry

NODE = "worker-1"

PIPELINE = """
from piceli import App, Build, Pipeline, Registry, Target

target = Target.kubeconfig(
    "kubeconfig", context="fake", namespace="{namespace}",
    transport="loopback-http", nodes={{"worker": "{node}"}},
)
app = App("shop")
images = Build.spec("host-build.toml", builder="host", cache_dir="cache")
app.deployment("web", image=images["web"], ports=[8080])

pipeline = Pipeline(
    app, target, build=images, deliver=Registry("oci://127.0.0.1:{port}/shop"),
    state_dir="state",
    execution={{"max_seconds": 30, "readiness_seconds": 1, "poll_seconds": 0.05}},
)
"""


class NoDockerBackend(Backend):
    """The real backend, except that docker must never be looked up."""

    docker_calls: list[str] = []

    def docker(self):  # type: ignore[no-untyped-def]
        self.docker_calls.append("docker")
        raise AssertionError("a host build pipeline must not need docker")

    def build_tool(self):  # type: ignore[no-untyped-def]
        self.docker_calls.append("build_tool")
        raise AssertionError("a host build pipeline must not need docker")

    def image_present(self, image_id: str) -> bool:
        self.docker_calls.append("image_present")
        raise AssertionError("a host build pipeline must not query docker")


@pytest.fixture
def host_shop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Any, FakeRegistry, Path]]:
    NoDockerBackend.docker_calls = []
    monkeypatch.setattr("piceli.pipeline.runner.Backend", NoDockerBackend)
    monkeypatch.setenv("DOCKER_HOST", "unix:///nonexistent/docker.sock")
    registry = FakeRegistry()
    try:
        with serve() as (api, url):
            api.add_node(NODE, kernel_version="6.6.51+rpt-rpi-2712")
            (tmp_path / "kubeconfig").write_text(
                textwrap.dedent(
                    f"""
                    apiVersion: v1
                    kind: Config
                    current-context: must-not-be-used
                    clusters: [{{name: fake, cluster: {{server: "{url}"}}}}]
                    users: [{{name: nobody, user: {{}}}}]
                    contexts:
                    - {{name: fake, context: {{cluster: fake, user: nobody}}}}
                    - {{name: must-not-be-used, context: {{cluster: fake, user: nobody}}}}
                    """
                )
            )
            write_project(tmp_path, registry.port, publish_base(registry))
            (tmp_path / "app.py").write_text(
                PIPELINE.format(
                    namespace=TARGET.namespace, node=NODE, port=registry.port
                )
            )
            yield api, registry, tmp_path
    finally:
        registry.close()


def _plan(tmp_path: Path) -> dict[str, Any]:
    """``{"hash": combined hash, "stages": {stage: detail}}`` of ``--plan``."""
    code, events, result = deploy(tmp_path, "--plan", "--json")
    assert code == 0, result.stdout + result.stderr
    stages = {
        event["stage"]: event["detail"]
        for event in events
        if event.get("event") == "stage" and "stage" in event
    }
    return {"hash": events[-1]["combined_hash"], "stages": stages}


def test_plan_shows_the_host_builder_and_node_facts(host_shop) -> None:  # type: ignore[no-untyped-def]
    api, _, tmp_path = host_shop
    plan = _plan(tmp_path)
    build = plan["stages"]["build"]["builds"]["shop"]
    assert build["builder"] == "host" and build["builder_kind"] == "host"
    assert set(build["tools"]) == {"sh"}
    assert build["node_facts"]["page_size"] == 16384
    assert build["node_facts"]["page_size_source"] == "kernel"
    assert build["platform"] == "linux/arm64"
    inputs = plan["stages"]["inputs"]["builds"]["shop"]
    assert inputs["node_facts"]["node"] == NODE
    # The page size is part of the plan hash: relabel the node, plan again.
    api.nodes[NODE]["metadata"]["labels"] = {PAGE_SIZE_LABEL: "4096"}
    relabeled = _plan(tmp_path)
    assert (
        relabeled["stages"]["build"]["builds"]["shop"]["node_facts"]["page_size"]
        == 4096
    )
    assert relabeled["hash"] != plan["hash"]
    assert NoDockerBackend.docker_calls == []


def test_deploy_builds_on_the_host_and_pushes_one_changed_layer(host_shop) -> None:  # type: ignore[no-untyped-def]
    api, registry, tmp_path = host_shop
    code, events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 0, result.stdout + result.stderr
    assert events[-1]["state"] == "ready"
    assert NoDockerBackend.docker_calls == []
    image = api.objects[("Deployment", "web")]["spec"]["template"]["spec"][
        "containers"
    ][0]["image"]
    assert image.startswith(f"127.0.0.1:{registry.port}/shop/web@sha256:")
    receipt = json.loads((tmp_path / "state/builds/shop/receipt.json").read_text())
    assert receipt["builder"]["kind"] == "host"
    assert receipt["node_facts"]["page_size"] == 16384
    first = _delivery(tmp_path)
    assert first["source"]["kind"] == "archive"
    assert first["blobs"]["uploaded"] == first["blobs"]["total"]

    # Re-running changes nothing and pushes nothing.
    code, events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 0, result.stdout + result.stderr
    assert len(list((tmp_path / "state/deliveries").rglob("*.json"))) == 1

    # A one-line change: one layer (plus the config and manifest) is pushed.
    (tmp_path / "src" / "static.txt").write_text("static 2\n")
    code, events, result = deploy(tmp_path, "--auto-approve", "--json")
    assert code == 0, result.stdout + result.stderr
    second = _delivery(tmp_path, newest=True)
    uploaded = [
        item
        for item in second["blobs"]["items"]
        if item["action"] == "uploaded" and "layer" in item["media_type"]
    ]
    assert len(uploaded) == 1
    assert second["blobs"]["uploaded"] == 2  # that layer and the new config
    assert second["image"]["manifest_digest"] != first["image"]["manifest_digest"]
    assert NoDockerBackend.docker_calls == []


def _delivery(tmp_path: Path, *, newest: bool = False) -> dict[str, Any]:
    paths = sorted(
        (tmp_path / "state/deliveries").rglob("*.json"),
        key=lambda path: path.stat().st_mtime_ns,
    )
    return json.loads(paths[-1 if newest else 0].read_text())


def _outage(monkeypatch: pytest.MonkeyPatch) -> None:
    """The API refuses the node-facts read (the rest of the plan is unaffected)."""

    def unreachable(self, target, node):  # type: ignore[no-untyped-def]
        raise OSError("connection refused")

    monkeypatch.setattr(NoDockerBackend, "node_facts", unreachable)


def test_an_api_outage_uses_the_cached_node_facts_and_says_so(
    host_shop, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    """B22: a plan or build after one successful read needs no node read."""
    _, _, tmp_path = host_shop
    first = _plan(tmp_path)
    assert (tmp_path / "state" / "node-facts" / f"{NODE}.json").is_file()
    _outage(monkeypatch)
    code, events, result = deploy(tmp_path, "--plan", "--json")
    assert code == 0, result.stdout + result.stderr
    assert events[-1]["combined_hash"] == first["hash"]
    assert "cached" in result.stderr and "unreachable" in result.stderr


def test_no_cache_and_no_declared_facts_refuses_with_a_code(
    host_shop, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    _, _, tmp_path = host_shop
    _outage(monkeypatch)
    code, _, result = deploy(tmp_path, "--plan", "--json")
    assert code != 0
    assert "node-facts-unavailable" in result.stdout + result.stderr


def test_declared_node_facts_need_no_api_and_no_cache(host_shop, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    _, _, tmp_path = host_shop
    app = tmp_path / "app.py"
    app.write_text(
        app.read_text().replace(
            'cache_dir="cache")',
            'cache_dir="cache", node_facts={"node": "worker-1", "os": "linux", '
            '"architecture": "arm64", "kernel_version": "6.6.51+rpt-rpi-2712", '
            '"page_size": 16384, "page_size_source": "kernel"})',
        )
    )
    _outage(monkeypatch)
    plan = _plan(tmp_path)
    assert plan["stages"]["build"]["builds"]["shop"]["node_facts"]["page_size"] == 16384
    assert not (tmp_path / "state" / "node-facts").exists()
