"""A Python composition: pipeline environments built from many sources (no cluster).

The composition repository (``examples/python_composition/infra``) holds the
app, its host build spec and ``infra.py``; ``api`` and ``worker`` are two
product repositories. All three are local bare remotes. Git, the import of
the composition at its commit, the change keys, triggers and the status are
real; builds and deployments are fakes.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from piceli.gitops.state import DirectoryChannel, request
from piceli.infra.builders import (
    BuiltImage,
    JobSettings,
    MirrorItem,
    job_run_spec,
    spec_job,
)
from piceli.infra.controller import CompositionConfig, CompositionController
from piceli.infra.pipelines import RepoSettings, SpecBuildRequest
from piceli.infra.sources import SourceSet
from tests.unit.infra.git_support import Repo, example_files

EXAMPLE = Path(__file__).resolve().parents[3] / "examples" / "python_composition"
SUMMARY = {
    "schema": "piceli.composition.v1",
    "name": "example",
    "cluster": {
        "name": "my-cluster",
        "api": "https://127.0.0.1:6443",
        "registry": {
            "on": "my-cluster-worker",
            "port": 5000,
            "namespace": "piceli-system",
            "name": "piceli-registry",
            "repository": "example",
            "storage": "1Gi",
        },
    },
    "sources": [],
    "components": [],
    "environments": [],
}


class FakeBuilder:
    def __init__(self) -> None:
        self.built: list[list[str]] = []
        self.requests: list[SpecBuildRequest] = []
        self.mirrored: list[str] = []

    def build_spec(self, request: SpecBuildRequest, checkout: Any) -> dict[str, Any]:
        self.requests.append(request)
        self.built.append(sorted(request.images))
        return {
            name: BuiltImage(
                f"registry.example:5000/{wanted['repository']}@{wanted['key']}",
                wanted["key"],
            )
            for name, wanted in request.images.items()
        }

    def mirror(self, items: Sequence[MirrorItem]) -> dict[str, BuiltImage]:
        self.mirrored += [item.reference for item in items]
        return {
            item.component: BuiltImage(
                f"registry.example:5000/{item.repository}@sha256:" + "c" * 64,
                "sha256:" + "c" * 64,
            )
            for item in items
        }


class FakePorts:
    def __init__(self) -> None:
        self.builder = FakeBuilder()
        self.deployed: list[tuple[str, dict[str, str]]] = []
        self.envs: list[Any] = []
        self.removed: list[str] = []

    def pipeline(self, *args: Any) -> Any:  # pragma: no cover - contract envs only
        raise AssertionError("a pipeline environment renders no contracts")

    def pipeline_env(
        self, composition: Any, env: Any, images: Mapping[str, str]
    ) -> Any:
        self.envs.append(env)
        return {"env": env.name, "app": env.pipeline.name}

    def prepare_env(self, pipeline: Any, name: str) -> str:
        return f"ns-{name}"

    def env_up(self, pipeline: Any, name: str, **kwargs: Any) -> dict[str, Any]:
        if kwargs["approve"] is None:
            return {"state": "approval-required", "plan_hash": "sha256:" + "d" * 64}
        self.deployed.append((name, dict(kwargs["digests"])))
        return {"state": "ready", "namespace": f"ns-{name}"}

    def env_down(self, pipeline: Any, name: str) -> None:
        self.removed.append(name)

    def env_stop(self, pipeline: Any, name: str) -> None:
        pass


def _controller(
    root: Path, infra: Repo, ports: FakePorts, clock: dict[str, float]
) -> CompositionController:
    settings = RepoSettings(
        name="infra", url=infra.url, branch="main", entry="infra.py"
    )
    config = CompositionConfig(
        composition=SUMMARY, repo=settings.to_dict(), poll_seconds=10
    )
    state = root / "state"
    return CompositionController(
        config,
        state_dir=state,
        sources=SourceSet(config.sources, state / "sources"),
        ports=ports,
        channel=DirectoryChannel(state),
        clock=lambda: clock["now"],
    )


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    remotes = tmp_path / "remotes"
    infra = Repo(remotes, "infra", example_files(EXAMPLE / "infra"))
    api = Repo(remotes, "api", example_files(EXAMPLE / "api"))
    worker = Repo(remotes, "worker", example_files(EXAMPLE / "worker"))
    api.tag("v0.1.0")
    monkeypatch.setenv("EXAMPLE_INFRA_URL", infra.url)
    monkeypatch.setenv("EXAMPLE_API_URL", api.url)
    monkeypatch.setenv("EXAMPLE_WORKER_URL", worker.url)
    ports = FakePorts()
    clock = {"now": 1_000_000.0}
    controller = _controller(tmp_path, infra, ports, clock)
    return {
        "infra": infra,
        "api": api,
        "worker": worker,
        "controller": controller,
        "ports": ports,
        "state": tmp_path / "state",
    }


def _states(status: Mapping[str, Any], env: str) -> dict[str, str]:
    return {k: v["state"] for k, v in status["envs"][env]["components"].items()}


def _digests(status: Mapping[str, Any], env: str) -> dict[str, str]:
    return {k: v["digest"] for k, v in status["envs"][env]["components"].items()}


def test_the_controller_imports_the_composition_and_builds_every_image(
    world: dict[str, Any],
) -> None:
    controller, ports = world["controller"], world["ports"]
    status = controller.poll_once()
    main = status["envs"]["main"]
    assert main["state"] == "deployed", main
    # rc follows main of infra and worker (first sight) and api's latest tag;
    # it has no auto_approve: it waits for the owner's hash.
    rc = status["envs"]["rc"]
    assert (
        rc["state"] == "approval-required" and rc["refs"]["api"] == "refs/tags/v0.1.0"
    )
    # One build of the spec, every image; the third-party image mirrored once.
    assert ports.builder.built == [["api", "worker"]]
    (built,) = ports.builder.requests
    assert built.spec == "host-build.toml" and built.repo == "infra"
    assert set(built.commits) == {"infra", "api", "worker"}
    assert ports.builder.mirrored == [
        "docker.io/library/busybox@sha256:"
        "bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e"
    ]
    # The status: one component per output image, the WP-D schema.
    assert set(main["revision"]) == {"infra", "api", "worker"}
    api = main["components"]["api"]
    assert set(api) >= {
        "source",
        "commit",
        "digest",
        "source_digest",
        "image",
        "state",
        "health",
        "updated_at",
    }
    assert api["sources"] == {
        "api": main["revision"]["api"],
        "infra": main["revision"]["infra"],
    }
    assert main["components"]["worker"]["sources"] == {
        "worker": main["revision"]["worker"]
    }
    assert set(_states(status, "main").values()) == {"synced"}
    assert ports.deployed[-1][0] == "main"
    assert set(ports.deployed[-1][1]) == {"api", "worker"}
    repo = status["controller"]["composition_repo"]
    assert repo["source"] == "infra" and repo["commit"] == main["revision"]["infra"]
    assert status["controller"]["composition"] == "example"
    assert status["sources"]["infra"]["refs"] == {
        "refs/heads/main": main["revision"]["infra"]
    }
    # Nothing moved: nothing built or deployed again.
    controller.poll_once()
    assert len(ports.deployed) == 1 and len(ports.builder.built) == 1


def test_a_change_in_one_source_rebuilds_only_the_images_that_read_it(
    world: dict[str, Any],
) -> None:
    controller, ports = world["controller"], world["ports"]
    first = _digests(controller.poll_once(), "main")
    world["worker"].commit({"jobs/run.sh": "#!/bin/sh\necho v2\n"})
    status = controller.poll_once()
    assert ports.builder.built[-1] == ["worker"]
    assert _states(status, "main") == {"api": "unchanged", "worker": "synced"}
    after = _digests(status, "main")
    assert after["api"] == first["api"] and after["worker"] != first["worker"]
    # A file no context includes: no build; the environment applies as no-op.
    world["api"].commit({"README.md": "docs\n"})
    status = controller.poll_once()
    assert len(ports.builder.built) == 2
    assert set(_states(status, "main").values()) == {"unchanged"}
    assert len(ports.deployed) == 3


def test_a_change_in_the_composition_rerenders_without_rebuilding(
    world: dict[str, Any],
) -> None:
    controller, ports, infra = world["controller"], world["ports"], world["infra"]
    controller.poll_once()
    text = (EXAMPLE / "infra" / "infra.py").read_text()
    sha = infra.commit({"infra.py": text.replace("limit=2", "limit=3")})
    status = controller.poll_once()
    main = status["envs"]["main"]
    assert main["revision"]["infra"] == sha and main["trigger"] == "push infra/main"
    assert len(ports.builder.built) == 1  # same keys: nothing rebuilt
    assert set(_states(status, "main").values()) == {"unchanged"}
    assert len(ports.deployed) == 2  # re-rendered with the new module
    assert status["controller"]["composition_repo"]["commit"] == sha
    # A file of the composition repository that only the api image reads.
    infra.commit({"config/api.json": '{"greeting": "hi"}\n'})
    status = controller.poll_once()
    assert ports.builder.built[-1] == ["api"]
    assert _states(status, "main") == {"api": "synced", "worker": "unchanged"}


def test_a_broken_composition_keeps_the_last_good_one(world: dict[str, Any]) -> None:
    controller, infra = world["controller"], world["infra"]
    good = controller.poll_once()["controller"]["composition_repo"]["commit"]
    broken = infra.commit({"infra.py": "environments = [\n"})
    status = controller.poll_once()
    repo = status["controller"]["composition_repo"]
    assert repo["commit"] == good and repo["failed"] == broken
    assert repo["error"] == "composition-invalid"
    # main still deploys with the last good module (it follows infra's main:
    # the build spec and contexts are read at the new commit).
    assert status["envs"]["main"]["state"] == "deployed"
    assert controller.repo.commit == good
    controller.poll_once()  # not retried until the branch moves
    fixed = infra.commit({"infra.py": (EXAMPLE / "infra" / "infra.py").read_text()})
    status = controller.poll_once()
    assert status["controller"]["composition_repo"]["commit"] == fixed
    assert status["controller"]["composition_repo"]["error"] is None


def test_branch_and_tag_environments_of_a_pipeline(world: dict[str, Any]) -> None:
    controller, ports, api = world["controller"], world["ports"], world["api"]
    controller.poll_once()
    api.commit({"site/index.html": "<h1>login</h1>\n"}, branch="wp-login")
    status = controller.poll_once()
    env = status["envs"]["wp-login"]
    assert env["state"] == "deployed", env
    assert env["refs"]["api"] == "refs/heads/wp-login"
    assert env["refs"]["infra"] == "refs/heads/main"
    assert ports.builder.built[-1] == ["api"]  # worker's key is main's
    rule = ports.envs[-1]
    assert rule.stack.workloads == ("api", "cache") and rule.pipeline.name == "example"
    sha = api.tag("v1.0.0")
    status = controller.poll_once()
    rc = status["envs"]["rc"]
    assert rc["state"] == "approval-required"  # rc has no auto_approve
    assert rc["revision"]["api"] == sha
    api.delete_branch("wp-login")
    status = controller.poll_once()
    assert "wp-login" not in status["envs"] and ports.removed == ["wp-login"]


def test_sync_rebuilds_one_image_on_request(world: dict[str, Any]) -> None:
    controller, ports = world["controller"], world["ports"]
    controller.poll_once()
    channel = DirectoryChannel(world["state"])
    channel.add_request(*request("sync", env="main", component="worker"))
    status = controller.poll_once()
    assert ports.builder.built[-1] == ["worker"]
    assert status["envs"]["main"]["trigger"] == "sync worker"


def test_the_image_build_job_fetches_every_source_and_pushes_changed_images() -> None:
    settings = JobSettings(
        image="registry.example/builder@sha256:" + "f" * 64,
        namespace="piceli-system",
        registry_url="oci://piceli-registry.piceli-system.svc:5000/example",
        node_registry="piceli-registry.piceli-system.svc:5000",
        git_secret="git-token",
    )
    built = SpecBuildRequest(
        spec="host-build.toml",
        repo="infra",
        commits={"infra": "1" * 40, "worker": "2" * 40},
        images={
            "worker": {"repository": "example/worker", "key": "sha256:" + "3" * 64}
        },
        urls={"infra": "https://git.example/infra.git"},
    )
    job = spec_job(settings, built, {"worker": "https://git.example/worker.git"})
    container = job["spec"]["template"]["spec"]["containers"][0]
    env = {item["name"]: item for item in container["env"]}
    assert env["PICELI_BUILD_SOURCES"]["value"].splitlines() == [
        f"infra https://git.example/infra.git {'1' * 40}",
        f"worker https://git.example/worker.git {'2' * 40}",
    ]
    assert env["PICELI_BUILD_REGISTRY"]["value"] == (
        "oci://piceli-registry.piceli-system.svc:5000"
    )
    command = container["command"]
    assert command[command.index("--spec") + 1] == "infra/host-build.toml"
    image = json.loads(command[command.index("--image") + 1])
    assert image == {
        "image": "worker",
        "repository": "example/worker",
        "key": "sha256:" + "3" * 64,
    }
    secrets = {
        item["valueFrom"]["secretKeyRef"]["name"]
        for item in env.values()
        if "valueFrom" in item
    }
    assert secrets == {"git-token"}


SPEC = """
revision = "piceli.host-build.v1"
name = "two"

[build]
tools = ["true"]
commands = [["true"]]

[context.config]
include = ["config/**"]

[context.api]
source = "api"
include = ["site/**"]

[context.worker]
source = "worker"
include = ["jobs/**"]

[[output.image]]
name = "api"
repository = "two/api"
contexts = ["api", "config"]
files = { "api/site" = "/srv/site", "config/config/api.json" = "/srv/api.json" }

[[output.image]]
name = "worker"
repository = "two/worker"
contexts = ["worker"]
files = { "worker/jobs" = "/opt/worker" }
"""


def test_the_job_run_builds_the_spec_and_pushes_only_the_named_images(
    tmp_path: Path,
) -> None:
    sources = tmp_path / "sources"
    files = {
        "infra": {"host-build.toml": SPEC, "config/api.json": "{}\n"},
        "api": {"site/index.html": "<h1>hi</h1>\n"},
        "worker": {"jobs/run.sh": "echo hi\n"},
    }
    sources.mkdir()
    for name, content in files.items():
        repo = Repo(tmp_path / "remotes", name, content)
        repo.work.rename(sources / name)  # a Git work tree, as the Job fetches it
    pushed: list[str] = []

    def deliver(
        archive: Any, target: Any, grant: Any, *, node_registry: Any
    ) -> dict[str, Any]:
        pushed.append(target.repository)
        return {
            "state": "succeeded",
            "pull_ref": f"{node_registry}/{target.repository}@sha256:" + "8" * 64,
        }

    receipt = job_run_spec(
        sources=sources,
        spec="infra/host-build.toml",
        images=[
            {
                "image": "worker",
                "repository": "example/worker",
                "key": "sha256:" + "7" * 64,
            }
        ],
        platforms=("linux/arm64",),
        cache=tmp_path / "cache",
        out=tmp_path / "out",
        registry_url="oci://registry.example:5000",
        node_registry="registry.example:5000",
        deliver=deliver,
    )
    assert pushed == ["example/worker"]
    entry = receipt["images"]["worker"]
    assert entry["key"] == "sha256:" + "7" * 64
    assert entry["pull_ref"].startswith("registry.example:5000/example/worker@")


def test_a_pipeline_environment_renders_into_its_namespace_with_given_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    from piceli.envs.ops import env_pipeline
    from piceli.infra.composition import load_composition
    from piceli.infra.pipelines import environment_pipeline
    from piceli.pipeline.compose import offline_composition

    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    composition = load_composition(EXAMPLE / "infra" / "infra.py")
    registry = "piceli-registry.piceli-system.svc:5000"
    refs = {
        "api": f"{registry}/example/api@sha256:" + "1" * 64,
        "worker": f"{registry}/example/worker@sha256:" + "2" * 64,
    }

    def rendered(env: Any, name: str) -> dict[tuple[str, str], Any]:
        declared = environment_pipeline(
            composition,
            env,
            kubeconfig=tmp_path / "kubeconfig",
            context="in-cluster",
            state_dir=tmp_path / "state",
        )
        derived = env_pipeline(declared, name, digests=refs)
        _, objects = offline_composition(derived)
        return {
            (item.manifest["kind"], item.manifest["metadata"]["name"]): item.manifest
            for component in objects.components
            for item in component.resources
        }

    main = rendered(composition.environment("main"), "main")
    images = {
        name: manifest["spec"]["template"]["spec"]["containers"][0]["image"]
        for (kind, name), manifest in main.items()
        if kind == "Deployment"
    }
    assert images == {
        **refs,
        # The third-party image is pulled from the in-cluster registry's copy.
        "cache": f"{registry}/example/mirror/docker.io/library/busybox@sha256:"
        "bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e",
    }
    assert {m["metadata"]["namespace"] for m in main.values()} == {"example-main"}
    branch = rendered(composition.branch_rule, "wp-login")
    assert ("Deployment", "worker") not in branch  # the small stack
    assert ("NetworkPolicy", "piceli-env-isolation") in branch
    assert branch[("Deployment", "api")]["metadata"]["namespace"] == "example-wp-login"
    assert not (tmp_path / "state").exists()  # rendering writes nothing


def test_pipeline_environments_refuse_contract_components() -> None:
    from piceli.envs import EnvError, Environment, Stack
    from piceli.infra import Component, Source

    source = Source("https://example.com/shop.git")
    module: dict[str, Any] = {}
    exec(
        (EXAMPLE / "infra" / "example_app.py").read_text(),
        module,
    )
    with pytest.raises(EnvError) as raised:
        Environment(
            "main",
            namespace="shop",
            pipeline=module["pipeline"],
            stack=Stack("mixed", [Component("web", source=source)]),
            follow={source: "main"},
        )
    assert raised.value.code == "env-config-invalid"
    with pytest.raises(EnvError):
        Environment(
            "main",
            namespace="shop",
            pipeline=module["pipeline"],
            settings={"web": {"a": "b"}},
            follow={source: "main"},
        )
    with pytest.raises(EnvError):
        Environment("main", namespace="shop", pipeline="app.py:pipeline")
