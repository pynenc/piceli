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


FAILING_SPEC = SPEC.replace(
    'tools = ["true"]\ncommands = [["true"]]',
    'tools = ["sh"]\ncommands = [["sh", "-c", "echo compiling; '
    'echo \\"error: expected item token=s3cr3t-not-printed\\" >&2; exit 1"]]',
)


def _failing_sources(tmp_path: Path) -> Path:
    sources = tmp_path / "sources"
    sources.mkdir()
    files = {
        "infra": {"host-build.toml": FAILING_SPEC, "config/api.json": "{}\n"},
        "api": {"site/index.html": "<h1>hi</h1>\n"},
        "worker": {"jobs/run.sh": "echo hi\n"},
    }
    for name, content in files.items():
        repo = Repo(tmp_path / "remotes", name, content)
        repo.work.rename(sources / name)
    return sources


def test_a_failed_composition_build_keeps_the_commands_output(tmp_path: Path) -> None:
    from piceli.infra import CompositionError

    with pytest.raises(CompositionError) as failed:
        job_run_spec(
            sources=_failing_sources(tmp_path),
            spec="infra/host-build.toml",
            images=[
                {
                    "image": "worker",
                    "repository": "x/worker",
                    "key": "sha256:" + "7" * 64,
                }
            ],
            platforms=("linux/arm64",),
            cache=tmp_path / "cache",
            out=tmp_path / "out",
            registry_url="oci://registry.example:5000",
            node_registry="registry.example:5000",
        )
    assert failed.value.code == "component-build-failed"
    tail = failed.value.output_tail
    assert "compiling" in tail and "error: expected item" in tail
    assert "s3cr3t-not-printed" not in tail
    # What `gitops status` shows (failure.log_tail) for a local build.
    from piceli.gitops.controller import failure_detail

    detail = failure_detail(failed.value)
    assert detail is not None and "error: expected item" in detail["log_tail"]


def test_the_composition_build_job_prints_the_failing_commands_tail(
    tmp_path: Path,
) -> None:
    from typer.testing import CliRunner

    from piceli.k8s.cli import app

    image = {"image": "worker", "repository": "x/worker", "key": "sha256:" + "7" * 64}
    result = CliRunner().invoke(
        app,
        [
            "build", "job-run",
            "--sources", str(_failing_sources(tmp_path)),
            "--spec", "infra/host-build.toml",
            "--image", json.dumps(image),
            "--platform", "linux/arm64",
            "--cache", str(tmp_path / "cache"),
            "--out", str(tmp_path / "out"),
            "--registry-url", "oci://registry.example:5000",
        ],
    )  # fmt: skip
    assert result.exit_code != 0, result.output
    # The pod log (the Job's output) holds what the command printed last.
    assert "error: expected item" in result.stderr
    assert "s3cr3t-not-printed" not in result.output + result.stderr


#: Two contexts of the one ``api`` source, each with its own ``include``: the
#: ``api`` image reads ``site/``, the ``worker`` image reads ``jobs/`` of the
#: same repository (and its own ``worker`` source).
ONE_SOURCE_SPEC = """
revision = "piceli.host-build.v1"
name = "example"

[build]
tools = ["true"]
commands = [["true"]]

[context.site]
source = "api"
include = ["site/**"]

[context.api_jobs]
source = "api"
include = ["jobs/**"]

[context.worker]
source = "worker"
include = ["jobs/**"]

[[output.image]]
name = "api"
repository = "example/api"
contexts = ["site"]
files = { "site/site" = "/srv/site" }

[[output.image]]
name = "worker"
repository = "example/worker"
contexts = ["api_jobs", "worker"]
files = { "api_jobs/jobs" = "/opt/api-jobs", "worker/jobs" = "/opt/worker" }
"""


def test_contexts_of_one_source_scope_each_image_to_its_own_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from piceli.artifacts.host_build import HostBuildSpec

    remotes = tmp_path / "remotes"
    files = example_files(EXAMPLE / "infra")
    files["host-build.toml"] = ONE_SOURCE_SPEC
    infra = Repo(remotes, "infra", files)
    api = Repo(
        remotes,
        "api",
        {**example_files(EXAMPLE / "api"), "jobs/nightly.sh": "#!/bin/sh\n"},
    )
    worker = Repo(remotes, "worker", example_files(EXAMPLE / "worker"))
    api.tag("v0.1.0")
    monkeypatch.setenv("EXAMPLE_INFRA_URL", infra.url)
    monkeypatch.setenv("EXAMPLE_API_URL", api.url)
    monkeypatch.setenv("EXAMPLE_WORKER_URL", worker.url)
    # The spec is valid as a host build too: one checkout serves both contexts.
    spec = HostBuildSpec.from_toml(infra.work / "host-build.toml")
    assert [c.source for c in spec.contexts] == ["api", "api", "worker"]
    ports = FakePorts()
    controller = _controller(tmp_path, infra, ports, {"now": 1_000_000.0})
    first = _digests(controller.poll_once(), "main")
    assert ports.builder.built == [["api", "worker"]]

    # A change under the worker image's include of `api`: only worker rebuilds.
    api.commit({"jobs/nightly.sh": "#!/bin/sh\necho v2\n"})
    status = controller.poll_once()
    assert ports.builder.built[-1] == ["worker"]
    assert _states(status, "main") == {"api": "unchanged", "worker": "synced"}
    second = _digests(status, "main")
    assert second["api"] == first["api"] and second["worker"] != first["worker"]

    # A change under the api image's include of the same source: only api.
    api.commit({"site/index.html": "<h1>v2</h1>\n"})
    status = controller.poll_once()
    assert ports.builder.built[-1] == ["api"]
    assert _states(status, "main") == {"api": "synced", "worker": "unchanged"}
    third = _digests(status, "main")
    assert third["worker"] == second["worker"] and third["api"] != second["api"]

    # A path neither include covers: nothing builds.
    api.commit({"README.md": "docs\n"})
    controller.poll_once()
    assert len(ports.builder.built) == 3


def test_the_job_builds_two_contexts_of_one_source_from_one_checkout(
    tmp_path: Path,
) -> None:
    sources = tmp_path / "sources"
    sources.mkdir()
    files = {
        "infra": {"host-build.toml": ONE_SOURCE_SPEC},
        "api": {"site/index.html": "<h1>hi</h1>\n", "jobs/nightly.sh": "echo n\n"},
        "worker": {"jobs/run.sh": "echo hi\n"},
    }
    for name, content in files.items():
        repo = Repo(tmp_path / "remotes", name, content)
        repo.work.rename(sources / name)
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
            {"image": name, "repository": f"example/{name}", "key": "sha256:" + k * 64}
            for name, k in (("api", "6"), ("worker", "7"))
        ],
        platforms=("linux/arm64",),
        cache=tmp_path / "cache",
        out=tmp_path / "out",
        registry_url="oci://registry.example:5000",
        node_registry="registry.example:5000",
        deliver=deliver,
    )
    assert sorted(pushed) == ["example/api", "example/worker"]
    assert set(receipt["images"]) == {"api", "worker"}


def test_the_job_run_reports_each_images_build_times(tmp_path: Path) -> None:
    sources = tmp_path / "sources"
    files = {
        "infra": {"host-build.toml": SPEC, "config/api.json": "{}\n"},
        "api": {"site/index.html": "<h1>hi</h1>\n"},
        "worker": {"jobs/run.sh": "echo hi\n"},
    }
    sources.mkdir()
    for name, content in files.items():
        repo = Repo(tmp_path / "remotes", name, content)
        repo.work.rename(sources / name)

    def deliver(
        archive: Any, target: Any, grant: Any, *, node_registry: Any
    ) -> dict[str, Any]:
        return {
            "state": "succeeded",
            "pull_ref": f"{node_registry}/{target.repository}@sha256:" + "8" * 64,
        }

    receipt = job_run_spec(
        sources=sources,
        spec="infra/host-build.toml",
        images=[
            {"image": name, "repository": f"example/{name}", "key": "sha256:" + c * 64}
            for name, c in (("api", "6"), ("worker", "7"))
        ],
        platforms=("linux/arm64",),
        cache=tmp_path / "cache",
        out=tmp_path / "out",
        registry_url="oci://registry.example:5000",
        node_registry="registry.example:5000",
        deliver=deliver,
    )
    commands = receipt["commands"]
    assert commands and all(
        set(item) == {"index", "tool", "seconds", "state"} for item in commands
    )
    for name in ("api", "worker"):
        timings = receipt["images"][name]["timings"]
        assert set(timings) == {"commands_seconds", "assemble_seconds", "push_seconds"}
        assert all(value >= 0 for value in timings.values())
        # The commands build every image of the spec: their time is shared.
        assert timings["commands_seconds"] == round(
            sum(item["seconds"] for item in commands), 3
        )


def test_the_history_shows_each_images_build_times(world: dict[str, Any]) -> None:
    controller, ports = world["controller"], world["ports"]
    timings = {
        "api": {"commands_seconds": 30.0, "assemble_seconds": 2.0, "push_seconds": 1.5},
        "worker": {
            "commands_seconds": 30.0,
            "assemble_seconds": 4.0,
            "push_seconds": 0.5,
        },
    }
    ports.builder.timings = timings
    lines: list[str] = []
    controller.log = lines.append
    controller.poll_once()
    from piceli.gitops import history as history_module

    controller._runs = history_module.RunReader()
    history = controller._history_document()
    (run,) = [r for r in history["envs"]["main"]["runs"] if r.get("builds")]
    assert run["builds"]["api"]["timings"] == timings["api"]
    assert run["builds"]["worker"]["shared_with"] == ["api"]
    assert any(
        "built worker in 34.5s (commands 30.0s, shared with api; assembly 4.0s, "
        "push 0.5s)" in line
        for line in lines
    )


def test_the_build_job_keeps_cargos_downloads_on_its_cache_claim() -> None:
    from piceli.infra.builders import JobSettings, spec_job

    request = SpecBuildRequest(
        "host-build.toml",
        "infra",
        {"infra": "a" * 40},
        {"api": {"repository": "example/api", "key": "sha256:" + "1" * 64}},
        None,
        {"infra": "https://example.com/infra.git"},
    )
    job = spec_job(
        JobSettings(
            image="example/builder@sha256:" + "2" * 64,
            namespace="piceli-system",
            registry_url="oci://registry:5000",
            node_registry="registry:5000",
        ),
        request,
        {},
    )
    script = job["spec"]["template"]["spec"]["containers"][0]["command"][2]
    assert 'export CARGO_HOME="${CARGO_HOME:-/cache/cargo-home}"' in script
