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
