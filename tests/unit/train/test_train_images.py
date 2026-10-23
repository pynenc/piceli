"""The train's images handed to the GitOps controller (``Step.images``).

A shop repository (the composition example's ``web`` and ``api``) carries a
train spec whose publish step builds the controller's images. The build and
the registry are fakes (a set of pushed references); the controller is the
real :class:`~piceli.infra.controller.CompositionController` with the fake
ports of its own tests, reading the same bare Git remotes.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from piceli.gitops import GitOpsError
from piceli.gitops.state import DirectoryChannel, prebuilt_request
from piceli.infra import remote_builds
from piceli.infra.composition import load_composition
from piceli.infra.controller import CompositionConfig, CompositionController
from piceli.infra.sources import SourceSet
from piceli.train.engine import TrainRun, locate
from piceli.train.images import ImageTools
from piceli.train.state import TrainState
from tests.unit.infra.git_support import Repo, example_files, git
from tests.unit.infra.test_controller import EXAMPLE, FakePorts

IMAGE = "ghcr.io/example/builder@sha256:" + "a" * 64

SPEC = """\
import sys

from piceli.train import Step, Train

train = Train(
    "shop",
    base="main",
    tested="tested",
    steps=[
        Step.command("check", [sys.executable, "-c", "print('ok')"]),
        Step.images("infra.py"),
    ],
)
"""


def _infra(prebuilt: bool) -> str:
    text = (EXAMPLE / "infra.py").read_text()
    return text.replace(
        "controller=Controller(on=REGISTRY_NODE),",
        f"controller=Controller(on=REGISTRY_NODE, prebuilt={prebuilt}),",
    )


class World:
    def __init__(self, tmp_path: Path, prebuilt: bool = True) -> None:
        remotes = tmp_path / "remotes"
        self.shop = Repo(
            remotes,
            "shop",
            {
                **example_files(EXAMPLE / "shop"),
                "infra.py": _infra(prebuilt),
                "train_spec.py": SPEC,
            },
        )
        git(self.shop.work, "config", "commit.gpgSign", "false")
        self.catalog = Repo(remotes, "catalog", example_files(EXAMPLE / "catalog"))
        self.registry: set[str] = set()
        composition = load_composition(self.shop.work / "infra.py")
        state = tmp_path / "controller"
        self.ports = FakePorts()
        self.channel = DirectoryChannel(state)
        self.clock = {"now": 1_000_000.0}
        self.controller = CompositionController(
            CompositionConfig(composition=composition.to_dict(), poll_seconds=10),
            state_dir=state,
            sources=SourceSet(composition.sources, state / "sources"),
            ports=self.ports,
            channel=self.channel,
            clock=lambda: self.clock["now"],
        )
        # The registry the controller checks digests against: what was pushed.
        self.controller._in_registry = self.registry.__contains__  # type: ignore[method-assign]
        self.root = tmp_path
        self.built_by_train: list[list[str]] = []

    @contextmanager
    def tools(self, composition: Any, _step: Any, _ctx: Any) -> Iterator[ImageTools]:
        def build(wanted: Mapping[str, Any], _tail: Any) -> dict[str, Any]:
            self.built_by_train.append(
                sorted(item["component"] for item in wanted["components"])
            )
            images = {}
            for item in wanted["components"]:
                digest = "sha256:" + hashlib.sha256(item["digest"].encode()).hexdigest()
                ref = f"{composition.registry.host}/{item['repository']}@{digest}"
                self.registry.add(ref)
                images[item["component"]] = {
                    "key": item["digest"],
                    "pull_ref": ref,
                    "manifest_digest": digest,
                    "platforms": {"linux/amd64": digest},
                }
            return images

        yield ImageTools(
            host="server", image=IMAGE, build=build, offer=self.channel.add_request
        )

    def train(self, *branches: str) -> dict[str, Any]:
        loaded, repo, spec_path = locate(
            f"{self.shop.work / 'train_spec.py'}:train", self.shop.work
        )
        run = TrainRun(
            loaded, repo=repo, spec_path=spec_path, branches=list(branches),
            state=TrainState.for_train("shop", self.root / "train-state"),
            promote=True, images=self.tools,
        )  # fmt: skip
        return run.run()

    def poll(self) -> dict[str, Any]:
        self.clock["now"] += 60
        return self.controller.poll_once()


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    remotes = tmp_path / "remotes"
    monkeypatch.setenv("COMPOSITION_SHOP_URL", str(remotes / "shop.git"))
    monkeypatch.setenv("COMPOSITION_CATALOG_URL", str(remotes / "catalog.git"))
    return World(tmp_path)


def test_the_promoted_commit_deploys_with_the_trains_images_and_no_build(
    world: World,
) -> None:
    status = world.poll()
    assert status["envs"]["main"]["state"] == "deployed"
    assert world.ports.builder.built == [["api", "catalog", "web"]]
    # A branch changes api; the train tests it, promotes it and builds its
    # images before anything is pushed.
    world.shop.commit({"api/status.json": '{"ok": 2}\n'}, branch="feat-api")
    git(world.shop.work, "checkout", "-q", "main")
    result = world.train("feat-api")
    assert result["ok"], result
    (images,) = [s for s in result["promote"]["publish"] if s["name"] == "images"]
    assert images["verdict"] == "passed" and images["host"] == "server"
    assert world.built_by_train == [["api", "web"]]
    offered = images["detail"]["images"]["api"]
    # The push the controller reacts to comes after the offer.
    git(world.shop.work, "push", "-q", "origin", "main")
    status = world.poll()
    main = status["envs"]["main"]
    assert main["state"] == "deployed"
    assert main["revision"]["shop"] == result["head"]
    # No build: api's change key is the offered one; web's did not change.
    assert world.ports.builder.built == [["api", "catalog", "web"]]
    api = main["components"]["api"]
    assert api["image"] == offered["pull_ref"]
    assert api["source_digest"] == offered["key"]
    assert api["provenance"]["builder"] == "train"
    assert api["provenance"]["host"] == "server"
    assert api["provenance"]["train"] == {
        "name": "shop",
        "run": result["run"],
        "tag": result["promote"]["tag"],
    }
    assert main["components"]["web"]["state"] == "unchanged"
    assert {item["component"] for item in status["controller"]["prebuilt"]} >= {"api"}


def test_an_offer_under_another_key_is_never_used(world: World) -> None:
    world.poll()
    digest = "sha256:" + "e" * 64
    ref = f"piceli-registry.piceli-system.svc:5000/shop/api@{digest}"
    world.registry.add(ref)
    world.channel.add_request(
        *prebuilt_request(
            "server",
            {
                "api": {
                    "key": "sha256:" + "0" * 64,
                    "pull_ref": ref,
                    "manifest_digest": digest,
                }
            },
            {"host": "server", "train": {"name": "shop", "run": "r"}},
        )
    )
    world.shop.commit({"api/status.json": '{"ok": 3}\n'})
    status = world.poll()
    assert status["envs"]["main"]["components"]["api"]["image"] != ref
    assert world.ports.builder.built[-1] == ["api"]  # built as before


def test_a_controller_without_prebuilt_refuses_offers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("COMPOSITION_SHOP_URL", str(tmp_path / "remotes" / "shop.git"))
    monkeypatch.setenv(
        "COMPOSITION_CATALOG_URL", str(tmp_path / "remotes" / "catalog.git")
    )
    world = World(tmp_path, prebuilt=False)
    world.poll()
    world.shop.commit({"api/status.json": '{"ok": 2}\n'}, branch="feat-api")
    git(world.shop.work, "checkout", "-q", "main")
    result = world.train("feat-api")
    git(world.shop.work, "push", "-q", "origin", "main")
    world.poll()
    rejected = world.controller.state.get("rejected") or []
    assert rejected and rejected[-1]["kind"] == "prebuilt"
    assert rejected[-1]["reason"] == "gitops-request-invalid"
    assert world.ports.builder.built[-1] == ["api"]
    assert result["promote"]["state"] == "promoted"


def _offer(**image: Any) -> dict[str, Any]:
    digest = "sha256:" + "f" * 64
    body = {
        "key": "sha256:" + "1" * 64,
        "pull_ref": f"reg:5000/shop/api@{digest}",
        "manifest_digest": digest,
        **image,
    }
    return {"host": "server", "images": {"api": body}, "receipt": {}}


@pytest.mark.parametrize(
    ("image", "why"),
    [
        ({"pull_ref": "reg:5000/shop/web@sha256:" + "f" * 64}, "repository"),
        ({"pull_ref": "elsewhere:5000/shop/api@sha256:" + "f" * 64}, "repository"),
        ({"pull_ref": "reg:5000/shop/api:latest"}, "pinned"),
        ({"key": ""}, "change key"),
    ],
)
def test_offers_are_checked_before_they_are_kept(
    image: dict[str, Any], why: str
) -> None:
    state: dict[str, Any] = {}
    with pytest.raises(GitOpsError) as caught:
        remote_builds.offered(
            state, _offer(**image), present=lambda _ref: True,
            repository=lambda name: f"shop/{name}", components=["api", "web"],
            registry="reg:5000", now=0.0,
        )  # fmt: skip
    assert why in str(caught.value) and not state.get("prebuilt")


def test_an_offer_missing_from_the_registry_is_refused() -> None:
    with pytest.raises(GitOpsError, match="not in the registry"):
        remote_builds.offered(
            {}, _offer(), present=lambda _ref: False,
            repository=lambda name: f"shop/{name}", components=["api"],
            registry="reg:5000", now=0.0,
        )  # fmt: skip
    state: dict[str, Any] = {}
    remote_builds.offered(
        state, _offer(), present=lambda _ref: True,
        repository=lambda name: f"shop/{name}", components=["api"],
        registry="reg:5000", now=0.0,
    )  # fmt: skip
    assert remote_builds.take_offer(state, "api", "sha256:" + "1" * 64)
    assert remote_builds.take_offer(state, "api", "sha256:" + "2" * 64) is None


# ------------------------------------------------- pipeline environments

PIPELINE_EXAMPLE = EXAMPLE.parents[0] / "python_composition" / "infra"

#: The example's host build, its contexts all in the composition repository.
HOST_BUILD = """\
revision = "piceli.host-build.v1"
name = "example"

[build]
tools = ["true"]
commands = [["true"]]

[context.infra]
include = ["config/**"]

[context.api]
path = "api"
include = ["site/**"]

[context.worker]
path = "worker"
include = ["jobs/**"]

[[output.image]]
name = "api"
repository = "example/api"
contexts = ["api", "infra"]
base = { image = "docker.io/library/busybox:1.37.0", digest = "sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e" }
files = { "api/site" = "/srv/site", "infra/config/api.json" = "/srv/site/config.json" }
user = "65534:65534"
entrypoint = ["httpd", "-f", "-p", "8080", "-h", "/srv/site"]

[[output.image]]
name = "worker"
repository = "example/worker"
contexts = ["worker"]
base = { image = "docker.io/library/busybox:1.37.0", digest = "sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e" }
files = { "worker/jobs" = "/opt/worker" }
user = "65534:65534"
entrypoint = ["sh", "/opt/worker/run.sh"]
"""

PIPELINE_INFRA = """\
import os

from example_app import REGISTRY_NODE, pipeline, registry

from piceli.envs import Environment, Stack
from piceli.infra import Cluster, Controller, Source

name = "example"

cluster = Cluster(
    "my-cluster",
    api="https://127.0.0.1:6443",
    credentials="my-cluster",
    registry=registry,
    controller=Controller(on=REGISTRY_NODE, prebuilt=True),
)
infra = Source(os.environ["EXAMPLE_INFRA_URL"], name="infra")
environments = [
    Environment(
        "main",
        namespace="example-main",
        pipeline=pipeline,
        stack=Stack("full", workloads=["api", "worker", "cache"]),
        cluster=cluster,
        follow={infra: "main"},
        auto_approve=True,
    ),
]
"""

PIPELINE_SPEC = """\
import sys

from piceli.train import Step, Train

train = Train(
    "example",
    steps=[
        Step.command("check", [sys.executable, "-c", "print('ok')"]),
        Step.images("infra.py", platforms=["linux/amd64", "linux/arm64"]),
    ],
)
"""

TWO = ("linux/amd64", "linux/arm64")


class PipelineWorld:
    def __init__(self, tmp_path: Path, platforms: tuple[str, ...] = TWO) -> None:
        from piceli.infra.pipelines import RepoSettings
        from tests.unit.infra.test_pipeline_composition import SUMMARY
        from tests.unit.infra.test_pipeline_composition import (
            FakePorts as PipelinePorts,
        )

        files = {
            "example_app.py": (PIPELINE_EXAMPLE / "example_app.py").read_text(),
            "config/api.json": (PIPELINE_EXAMPLE / "config/api.json").read_text(),
            "config/worker.json": (PIPELINE_EXAMPLE / "config/worker.json").read_text(),
            "api/site/index.html": "<h1>api</h1>\n",
            "worker/jobs/run.sh": "echo work\n",
            "host-build.toml": HOST_BUILD,
            "infra.py": PIPELINE_INFRA,
            "train_spec.py": PIPELINE_SPEC,
        }
        self.infra = Repo(tmp_path / "remotes", "infra", files)
        git(self.infra.work, "config", "commit.gpgSign", "false")
        self.registry: set[str] = set()
        self.ports = PipelinePorts()
        self.channel = DirectoryChannel(tmp_path / "controller")
        self.clock = {"now": 1_000_000.0}
        settings = RepoSettings(name="infra", url=self.infra.url, entry="infra.py")
        config = CompositionConfig(
            composition=SUMMARY, repo=settings.to_dict(), poll_seconds=10,
            platforms=platforms,
        )  # fmt: skip
        state = tmp_path / "controller"
        self.controller = CompositionController(
            config,
            state_dir=state,
            sources=SourceSet(config.sources, state / "sources"),
            ports=self.ports,
            channel=self.channel,
            clock=lambda: self.clock["now"],
        )
        self.controller._in_registry = self.registry.__contains__  # type: ignore[method-assign]
        self.root = tmp_path
        self.builds: list[Mapping[str, Any]] = []

    @contextmanager
    def tools(self, composition: Any, _step: Any, _ctx: Any) -> Iterator[ImageTools]:
        def build(wanted: Mapping[str, Any], _tail: Any) -> dict[str, Any]:
            self.builds.append(wanted)
            images = {}
            for name, item in wanted["keys"].items():
                per = {
                    p: "sha256:"
                    + hashlib.sha256(f"{item['key']}{p}".encode()).hexdigest()
                    for p in wanted["platforms"]
                }
                index = "sha256:" + hashlib.sha256(item["key"].encode()).hexdigest()
                ref = f"{composition.registry.host}/{item['repository']}@{index}"
                self.registry.add(ref)
                images[name] = {
                    "key": item["key"], "pull_ref": ref, "manifest_digest": index,
                    "platforms": per,
                }  # fmt: skip
            return images

        yield ImageTools(
            host="server", image=IMAGE, build=build, offer=self.channel.add_request,
            toolchains=lambda: {"rustc": "1.90.0"},
        )  # fmt: skip

    def train(self, *branches: str) -> dict[str, Any]:
        loaded, repo, spec_path = locate(
            f"{self.infra.work / 'train_spec.py'}:train", self.infra.work
        )
        return TrainRun(
            loaded, repo=repo, spec_path=spec_path, branches=list(branches),
            state=TrainState.for_train("example", self.root / "train-state"),
            promote=True, images=self.tools,
        ).run()  # fmt: skip

    def poll(self) -> dict[str, Any]:
        self.clock["now"] += 60
        return self.controller.poll_once()


@pytest.fixture
def pipeline_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PipelineWorld:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("EXAMPLE_INFRA_URL", str(tmp_path / "remotes" / "infra.git"))
    return PipelineWorld(tmp_path)


def test_a_pipeline_environment_deploys_the_trains_two_platform_images(
    pipeline_world: PipelineWorld,
) -> None:
    world = pipeline_world
    status = world.poll()
    assert status["envs"]["main"]["state"] == "deployed", status["envs"]["main"]
    assert world.ports.builder.built == [["api", "worker"]]
    world.infra.commit({"api/site/index.html": "<h1>api v2</h1>\n"}, branch="feat")
    git(world.infra.work, "checkout", "-q", "main")
    result = world.train("feat")
    assert result["ok"], result
    (images,) = [s for s in result["promote"]["publish"] if s["name"] == "images"]
    assert images["verdict"] == "passed", images
    (wanted,) = world.builds
    assert wanted["kind"] == "spec" and wanted["spec"] == "host-build.toml"
    assert wanted["repo"] == "infra" and wanted["platforms"] == list(TWO)
    api = images["detail"]["images"]["api"]
    assert api["platforms"] == list(TWO)
    git(world.infra.work, "push", "-q", "origin", "main")
    status = world.poll()
    main = status["envs"]["main"]
    assert main["state"] == "deployed", main
    # The controller's own key for each image is the offered one: no build.
    assert world.ports.builder.built == [["api", "worker"]]
    component = main["components"]["api"]
    assert component["image"] == api["pull_ref"]
    assert component["source_digest"] == api["key"]
    assert component["provenance"]["builder"] == "train"
    assert component["provenance"]["toolchains"] == {"rustc": "1.90.0"}
    assert component["provenance"]["train"]["run"] == result["run"]


def test_a_pipeline_offer_for_other_platforms_is_not_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("EXAMPLE_INFRA_URL", str(tmp_path / "remotes" / "infra.git"))
    # The controller builds for amd64 only; the train offers both platforms:
    # the keys differ, so the controller builds as before.
    world = PipelineWorld(tmp_path, platforms=("linux/amd64",))
    world.poll()
    world.infra.commit({"api/site/index.html": "<h1>api v2</h1>\n"}, branch="feat")
    git(world.infra.work, "checkout", "-q", "main")
    assert world.train("feat")["ok"]
    git(world.infra.work, "push", "-q", "origin", "main")
    world.poll()
    assert world.ports.builder.built[-1] == ["api"]


# ------------------------------------------- two repositories (0.26.0)

#: The spec lives in the infra repository; its ``app`` contexts read the
#: product repository the train runs on.
TWO_REPO_BUILD = HOST_BUILD.replace(
    '[context.api]\npath = "api"', '[context.api]\nsource = "product"\npath = "api"'
).replace(
    '[context.worker]\npath = "worker"',
    '[context.worker]\nsource = "product"\npath = "worker"',
)

TWO_REPO_INFRA = PIPELINE_INFRA.replace(
    'infra = Source(os.environ["EXAMPLE_INFRA_URL"], name="infra")',
    'infra = Source(os.environ["EXAMPLE_INFRA_URL"], name="infra")\n'
    'product = Source(os.environ["EXAMPLE_PRODUCT_URL"], name="product")',
).replace('follow={infra: "main"}', 'follow={infra: "main", product: "main"}')

TWO_REPO_SPEC = """\
import os
import sys

from piceli.train import Step, Train

train = Train(
    "product",
    steps=[
        Step.command("check", [sys.executable, "-c", "print('ok')"]),
        Step.images(
            "infra.py",
            repository=os.environ["EXAMPLE_INFRA_URL"],
            branch="main",
            platforms=["linux/amd64", "linux/arm64"],
        ),
    ],
)
"""


class TwoRepoWorld(PipelineWorld):
    """The composition and its spec in ``infra``; the train on ``product``."""

    def __init__(self, tmp_path: Path) -> None:
        from piceli.infra.pipelines import RepoSettings
        from tests.unit.infra.test_pipeline_composition import SUMMARY
        from tests.unit.infra.test_pipeline_composition import (
            FakePorts as PipelinePorts,
        )

        remotes = tmp_path / "remotes"
        self.infra = Repo(
            remotes,
            "infra",
            {
                "example_app.py": (PIPELINE_EXAMPLE / "example_app.py").read_text(),
                "config/api.json": (PIPELINE_EXAMPLE / "config/api.json").read_text(),
                "config/worker.json": (
                    PIPELINE_EXAMPLE / "config/worker.json"
                ).read_text(),
                "host-build.toml": TWO_REPO_BUILD,
                "infra.py": TWO_REPO_INFRA,
            },
        )
        self.product = Repo(
            remotes,
            "product",
            {
                "api/site/index.html": "<h1>api</h1>\n",
                "worker/jobs/run.sh": "echo work\n",
                "train_spec.py": TWO_REPO_SPEC,
            },
        )
        for repo in (self.infra, self.product):
            git(repo.work, "config", "commit.gpgSign", "false")
        self.registry = set()
        self.ports = PipelinePorts()
        self.channel = DirectoryChannel(tmp_path / "controller")
        self.clock = {"now": 1_000_000.0}
        settings = RepoSettings(name="infra", url=self.infra.url, entry="infra.py")
        config = CompositionConfig(
            composition=SUMMARY, repo=settings.to_dict(), poll_seconds=10,
            platforms=TWO,
        )  # fmt: skip
        state = tmp_path / "controller"
        self.controller = CompositionController(
            config,
            state_dir=state,
            sources=SourceSet(config.sources, state / "sources"),
            ports=self.ports,
            channel=self.channel,
            clock=lambda: self.clock["now"],
        )
        self.controller._in_registry = self.registry.__contains__  # type: ignore[method-assign]
        self.root = tmp_path
        self.builds = []

    def train(self, *branches: str) -> dict[str, Any]:
        loaded, repo, spec_path = locate(
            f"{self.product.work / 'train_spec.py'}:train", self.product.work
        )
        return TrainRun(
            loaded, repo=repo, spec_path=spec_path, branches=list(branches),
            state=TrainState.for_train("product", self.root / "train-state"),
            promote=True, images=self.tools,
        ).run()  # fmt: skip

    def change_api(self) -> dict[str, Any]:
        """A product branch changes api; the train tests and promotes it."""
        self.product.commit({"api/site/index.html": "<h1>v2</h1>\n"}, branch="feat")
        git(self.product.work, "checkout", "-q", "main")
        result = self.train("feat")
        assert result["ok"], result
        return result


@pytest.fixture
def two_repos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TwoRepoWorld:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("EXAMPLE_INFRA_URL", str(tmp_path / "remotes" / "infra.git"))
    monkeypatch.setenv("EXAMPLE_PRODUCT_URL", str(tmp_path / "remotes" / "product.git"))
    return TwoRepoWorld(tmp_path)


def test_two_repositories_deploy_the_trains_images_with_no_build(
    two_repos: TwoRepoWorld,
) -> None:
    world = two_repos
    status = world.poll()
    assert status["envs"]["main"]["state"] == "deployed", status["envs"]["main"]
    assert world.ports.builder.built == [["api", "worker"]]
    infra_tip = git(world.infra.work, "rev-parse", "HEAD")
    result = world.change_api()
    (images,) = [s for s in result["promote"]["publish"] if s["name"] == "images"]
    assert images["verdict"] == "passed", images
    (wanted,) = world.builds
    # The spec is the infra repository's; the product at the promoted head,
    # the infra repository at the tip of the branch the controller follows.
    assert wanted["spec"] == "host-build.toml" and wanted["repo"] == "infra"
    assert wanted["commits"] == {"infra": infra_tip, "product": result["head"]}
    assert wanted["urls"]["infra"] == world.infra.url
    assert wanted["platforms"] == list(TWO)
    assert images["detail"]["sources"] == {
        "infra": infra_tip,
        "product": result["head"],
    }
    api = images["detail"]["images"]["api"]
    assert api["platforms"] == list(TWO)
    git(world.product.work, "push", "-q", "origin", "main")
    status = world.poll()
    main = status["envs"]["main"]
    assert main["state"] == "deployed", main
    assert main["revision"] == {"infra": infra_tip, "product": result["head"]}
    # The controller's own keys are the offered ones: zero builds.
    assert world.ports.builder.built == [["api", "worker"]]
    component = main["components"]["api"]
    assert component["image"] == api["pull_ref"]
    assert component["source_digest"] == api["key"]
    assert component["provenance"]["builder"] == "train"
    assert component["provenance"]["commits"] == {
        "infra": infra_tip,
        "product": result["head"],
    }
    assert component["provenance"]["platforms"] == list(TWO)
    (offer,) = [o for o in status["controller"]["prebuilt"] if o["component"] == "api"]
    assert offer["sources"] == {"infra": infra_tip, "product": result["head"]}


def test_an_offer_is_not_used_when_the_other_repository_moved(
    two_repos: TwoRepoWorld,
) -> None:
    world = two_repos
    world.poll()
    offered_at = git(world.infra.work, "rev-parse", "HEAD")
    result = world.change_api()
    # The infra repository moves after the offer: api reads its config.
    moved = world.infra.commit({"config/api.json": '{"greeting": "moved"}\n'})
    git(world.product.work, "push", "-q", "origin", "main")
    status = world.poll()
    main = status["envs"]["main"]
    assert main["state"] == "deployed", main
    assert main["revision"] == {"infra": moved, "product": result["head"]}
    assert world.ports.builder.built[-1] == ["api"]  # built as before
    api = main["components"]["api"]
    assert (api.get("provenance") or {}).get("builder") != "train"
    assert api["prebuilt"] == {
        "used": False,
        "key": api["prebuilt"]["key"],
        "differs": {"infra": {"offered": offered_at, "here": moved}},
    }
    assert api["prebuilt"]["key"] != api["source_digest"]


def test_the_composition_repository_is_declared_on_the_step() -> None:
    from piceli.train import Step
    from piceli.train.model import TrainError, step_from_dict

    step = Step.images(
        "deploy/infra.py",
        repository="https://git.example.com/me/infra.git",
        branch="release",
    )
    data = step.to_dict()
    assert data["repository"] == "https://git.example.com/me/infra.git"
    assert data["branch"] == "release"
    assert step_from_dict(data) == step
    assert "repository" not in Step.images("infra.py").to_dict()
    with pytest.raises(TrainError, match="without credentials"):
        Step.images("infra.py", repository="https://user:token@git.example.com/x.git")
    with pytest.raises(TrainError, match="branch"):
        Step.images("infra.py", repository="https://git.example.com/x.git", branch="*")
