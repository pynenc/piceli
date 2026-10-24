"""The controller publishes NixHost releases (0.25.0, ``NixRelease``).

A real :class:`CompositionController` follows a local Git source with a
flake; a real ``BuilderServe`` with a :class:`Publisher` (its ``nix
build`` and ``nix copy`` faked, its keys real in a credentials directory in
``tmp_path``) claims, fetches with Git, checks the tag, signs and uploads the
manifest and the tag's pointer to a ``file://`` cache. Nothing reaches a
network or a cluster.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from piceli.dev import nix
from piceli.dev.model import DevError
from piceli.gitops.state import (
    DirectoryChannel,
    approve_request,
    build_claim_request,
    nix_published_request,
    sync_request,
)
from piceli.infra import CompositionError
from piceli.infra.builder_serve import BuilderServe
from piceli.infra.composition import Composition, load_composition
from piceli.infra.controller import CompositionConfig, CompositionController
from piceli.infra.nix_releases import Publisher, claim_message
from piceli.infra.sources import SourceSet
from tests.unit.infra.git_support import Repo, git
from tests.unit.infra.test_controller import FakePorts

SECRET = nix.generate_key("cache-1")
PUBLIC = nix.public_key(SECRET)
OTHER = nix.generate_key("cache-2")
STORE = "/nix/store/" + "a" * 32 + "-nixos-system-web-1"
ATTRIBUTE = ".#nixosConfigurations.web-1.config.system.build.toplevel"

INFRA = """
import os

from piceli.infra import NixCache, NixHost, NixRelease, Source, Tag

hosts = Source(os.environ["NIX_RELEASE_URL"], name="hosts")
cache = NixCache(os.environ["NIX_RELEASE_PUBLIC"],
                 url="file://" + os.environ["NIX_RELEASE_CACHE"])
web = NixHost("web-1", server="ssh://root@web-1.example.org", flake=".#web-1",
              follow=Tag("web-v*"), cache=cache)
web_release = NixRelease(web, source=hosts, builder="mini", auto_approve=AUTO)
"""


def _world(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    auto: bool = False,
    infra: str = INFRA,
    credentials_dir: Path | None = None,
) -> dict[str, Any]:
    from piceli.infra.machines.credentials import save_credential

    monkeypatch.setenv("PICELI_CREDENTIALS_DIR", str(tmp_path / "credentials"))
    save_credential("cache-1", "nix-signing-key", SECRET)
    repo = Repo(tmp_path / "remotes", "hosts", {"flake.nix": "{ outputs = _: {}; }"})
    repo.tag("web-v1.0.0")
    cache_dir = tmp_path / "cache"
    monkeypatch.setenv("NIX_RELEASE_URL", repo.url)
    monkeypatch.setenv("NIX_RELEASE_PUBLIC", PUBLIC)
    monkeypatch.setenv("NIX_RELEASE_CACHE", str(cache_dir))
    (tmp_path / "infra.py").write_text(infra.replace("AUTO", str(auto)))
    composition = load_composition(tmp_path / "infra.py")
    config = CompositionConfig(composition=composition.to_dict(), poll_seconds=10)
    state = tmp_path / "state"
    channel = DirectoryChannel(state)
    clock = {"now": 1_000_000.0}
    controller = CompositionController(
        config,
        state_dir=state,
        sources=SourceSet(
            composition.sources, state / "sources", credentials_dir=credentials_dir
        ),
        ports=FakePorts(),
        channel=channel,
        clock=lambda: clock["now"],
    )
    return {"repo": repo, "controller": controller, "channel": channel,
            "clock": clock, "cache": cache_dir, "state": state,
            "composition": composition}  # fmt: skip


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    return _world(tmp_path, monkeypatch)


def _cache(world: dict[str, Any]) -> Any:
    return nix.NixCache(PUBLIC, url=f"file://{world['cache']}")


def _worker(
    world: dict[str, Any],
    tmp_path: Path,
    *,
    build: Callable[..., str] | None = None,
    caches: list[Any] | None = None,
    host: str = "mini",
    said: list[str] | None = None,
    fetch: Callable[..., Any] | str | None = "fake",
) -> tuple[BuilderServe, list[dict[str, Any]]]:
    """``builder serve`` on ``host`` with a publisher whose Nix steps are fakes."""
    seen: list[dict[str, Any]] = []

    def fake_build(checkout: Path, attribute: str, say: Any) -> str:
        head = git(checkout, "rev-parse", "HEAD")
        seen.append({"checkout": checkout, "head": head, "attribute": attribute})
        say("building " + attribute)
        return STORE

    def fake_push(request: dict[str, Any], say: Any) -> dict[str, Any]:
        seen.append({"push": dict(request)})
        return {"state": "published", "store_path": request["store_path"],
                "nar_hash": "sha256:" + nix.nix32(b"\x01" * 32), "nar_size": 7,
                "signatures": ["cache-1:x"], "system": "x86_64-linux",
                "closure_size": 4096}  # fmt: skip

    publisher = Publisher(
        caches if caches is not None else [_cache(world)],
        build=build or fake_build,
        push=fake_push,
        host=host,
        fetch=(lambda pairs, _say, **_kw: {"state": "fetched", "inputs": []})
        if fetch == "fake"
        else fetch,  # type: ignore[arg-type]
    )
    controller = world["controller"]
    worker = BuilderServe(
        host=host,
        image="",
        channel=world["channel"],
        container=lambda *_a: 1,
        push=None,
        index=None,
        root=tmp_path / "builder",
        say=(said.append if said is not None else lambda _l: None),
        sleep=lambda _s: controller.poll_once(),
        publisher=publisher,
        images=False,
    )
    return worker, seen


def _release(status: dict[str, Any]) -> dict[str, Any]:
    return status["nix_releases"]["web-1"]


# ------------------------------------------------------------------ declaration


def test_a_release_is_declared_with_its_host_source_and_builder(
    world: dict[str, Any],
) -> None:
    composition = world["composition"]
    (target,) = composition.nix_releases
    body = composition.to_dict()
    assert body["environments"] == []
    assert body["nix_releases"] == [{
        "name": "web-1", "build": "web-1", "attribute": ATTRIBUTE,
        "follow": {"kind": "tag", "pattern": "web-v*", "prereleases": False},
        "source": "hosts", "builder": "mini", "auto_approve": False,
        "cache": {"store_url": f"file://{world['cache']}", "public_key": PUBLIC},
    }]  # fmt: skip
    assert {"name": "hosts", "url": world["repo"].url} in body["sources"]
    assert Composition.from_dict(body).nix_releases == (target,)
    assert "credentials" not in json.dumps(body)  # names of keys stay local too


def test_declarations_that_do_not_hold_are_refused(world: dict[str, Any]) -> None:
    from piceli.envs import Environment
    from piceli.hosts.model import Branch, NixHost, Tag
    from piceli.infra import NixRelease, Source

    hosts = Source(world["repo"].url, name="hosts")
    web = NixHost("web-1", server="ssh://root@web-1.example.org", flake=".#web-1",
                  follow=Tag("web-v*"), cache=_cache(world))  # fmt: skip
    branch = NixHost("web-1", server="ssh://root@web-1.example.org",
                     flake=".#web-1", follow=Branch("main"), cache=_cache(world))  # fmt: skip
    # 0.26.0: a branch's commits are published too.
    assert NixRelease(branch, source=hosts, builder="mini").target().branch == "main"
    for kwargs, words in (
        ({"host": web, "source": hosts, "builder": "mini", "schedule": "x"}, "cron"),
        ({"host": web, "source": "hosts", "builder": "mini"}, "Source"),
        ({"host": web, "source": hosts, "builder": "Mini Host"}, "build host"),
        ({"host": web, "source": hosts, "builder": "mini", "auto_approve": 1},
         "True or False"),
    ):  # fmt: skip
        with pytest.raises(CompositionError, match=words) as refused:
            NixRelease(**kwargs)
        assert refused.value.code == "composition-invalid"
    release = NixRelease(web, source=hosts, builder="mini")
    env = Environment("web-1", follow={hosts: "main"}, namespace="web")
    with pytest.raises(CompositionError, match="environment's name"):
        Composition("shop", (env,), (), nix_releases=(release.target(),))
    with pytest.raises(CompositionError, match="same host"):
        Composition("shop", (), (), nix_releases=(release.target(),) * 2)


# ------------------------------------------------------------------ the controller


def test_a_new_tag_waits_for_approval_then_the_build_host_publishes_it(
    world: dict[str, Any], tmp_path: Path
) -> None:
    controller, channel, repo = world["controller"], world["channel"], world["repo"]
    status = controller.poll_once()
    first = _release(status)
    assert first["state"] == "baseline" and first["latest_tag"] == "web-v1.0.0"
    assert status["controller"]["builds_wanted"] == []

    commit = repo.commit({"hosts/web-1.nix": "{ }\n"})
    for tag in ("web-v1.1.0", "api-v9.0.0", "v5.0.0", "web-v1.2.0-rc.1"):
        repo.tag(tag)
    waiting = _release(controller.poll_once())
    assert waiting["state"] == "approval-required"
    assert (waiting["tag"], waiting["commit"]) == ("web-v1.1.0", commit)
    digest = waiting["plan_digest"]
    assert waiting["approve_command"] == f"piceli gitops approve web-1 {digest}"
    assert waiting["plan"] == {
        "schema": "piceli.nix-release-plan.v1", "release": "web-1",
        "attribute": ATTRIBUTE, "name": "web-1", "commit": commit,
        "tag": "web-v1.1.0", "source": "hosts", "builder": "mini",
        "cache": {"store_url": f"file://{world['cache']}", "public_key": PUBLIC},
        "manifest": f"piceli/web-1/{commit}.json",
        "pointers": ["piceli/web-1/refs/tags/web-v1.1.0.json"],
    }  # fmt: skip
    assert nix.plan_digest(waiting["plan"]) == digest
    assert controller.poll_once()["controller"]["builds_wanted"] == []

    channel.add_request(*approve_request("web-1", "sha256:" + "0" * 64, via="cli"))
    status = controller.poll_once()
    assert status["rejected_requests"][-1]["reason"] == "gitops-approval-stale"
    channel.add_request(*approve_request("web-1", digest, via="cli"))
    status = controller.poll_once()
    assert _release(status)["state"] == "waiting-build"
    (wanted,) = status["controller"]["builds_wanted"]
    assert wanted["kind"] == "nix-publish" and wanted["builder"] == "mini"
    assert wanted["plan"]["commit"] == commit and wanted["claimed"] is None

    world["clock"]["now"] += 60
    worker, seen = _worker(world, tmp_path)
    assert worker.once() == wanted["id"]
    assert seen[0]["head"] == commit and seen[0]["attribute"] == ATTRIBUTE
    assert seen[1]["push"]["signing_key"] == SECRET  # on the host, never sent
    status = controller.poll_once()
    done = _release(status)
    assert done["state"] == "published" and done["reason"] is None
    assert done["published_tag"] == "web-v1.1.0"
    assert status["controller"]["builds_wanted"] == []

    manifest = json.loads((world["cache"] / f"piceli/web-1/{commit}.json").read_text())
    assert nix.verify_manifest(manifest, PUBLIC)
    assert (manifest["commit"], manifest["tag"]) == (commit, "web-v1.1.0")
    assert manifest["host"] == "mini" and manifest["store_path"] == STORE
    pointer = json.loads(
        (world["cache"] / "piceli/web-1/refs/tags/web-v1.1.0.json").read_text()
    )
    assert nix.verify_pointer(pointer, PUBLIC) and pointer["commit"] == commit
    published = done["published"]
    assert published["manifest_sha256"] == nix.manifest_digest(manifest)
    assert published["store_path"] == STORE and published["host"] == "mini"
    assert published["pointers"] == [{"ref": "refs/tags/web-v1.1.0",
        "commit": commit, "published_at": pointer["published_at"]}]  # fmt: skip
    assert set(done["stages"]) == {"approval", "queue", "fetch", "build", "publish"}
    assert all(item["seconds"] >= 0 for item in done["stages"].values())
    assert done["approval"]["via"] == "cli"

    history = json.loads((world["state"] / "history.json").read_text())
    (run,) = history["nix_releases"]["web-1"]["runs"]
    assert run["state"] == "published" and run["tag"] == "web-v1.1.0"
    assert run["manifest_sha256"] == published["manifest_sha256"]
    for text in (json.dumps(status), json.dumps(history)):
        assert SECRET.partition(":")[2] not in text
    assert worker.once() is None  # nothing left


def test_auto_approve_publishes_a_new_tag_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(tmp_path, monkeypatch, auto=True)
    controller, repo = world["controller"], world["repo"]
    controller.poll_once()
    commit = repo.commit({"a": "1"})
    repo.tag("web-v1.0.1")
    record = _release(controller.poll_once())
    assert record["state"] == "waiting-build" and record["approval"]["via"] == "policy"
    worker, _ = _worker(world, tmp_path)
    assert worker.once() is not None
    done = _release(controller.poll_once())
    assert done["state"] == "published" and done["commit"] == commit
    assert "approval" not in done["stages"]  # no wait for an owner


def test_a_failed_build_is_failed_with_its_tail_until_a_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(tmp_path, monkeypatch, auto=True)
    controller, channel, repo = world["controller"], world["channel"], world["repo"]
    controller.poll_once()
    repo.commit({"a": "1"})
    repo.tag("web-v1.0.1")
    controller.poll_once()

    def broken(checkout: Path, attribute: str, say: Any) -> str:
        say("error: attribute 'web-1' missing")
        raise DevError("dev-command-failed", "the Nix build did not pass")

    worker, _ = _worker(world, tmp_path, build=broken)
    assert worker.once() is not None
    status = controller.poll_once()
    failed = _release(status)
    assert failed["state"] == "failed" and failed["reason"] == (
        "gitops-nix-release-failed"
    )
    assert failed["failure"]["host"] == "mini"
    assert "dev-command-failed" in failed["failure"]["reason"]
    assert "attribute 'web-1' missing" in failed["failure"]["log_tail"]
    assert status["controller"]["builds_wanted"] == []
    world["clock"]["now"] += 600
    assert _release(controller.poll_once())["state"] == "failed"  # not retried
    assert controller.poll_once()["controller"]["builds_wanted"] == []
    history = json.loads((world["state"] / "history.json").read_text())
    assert history["nix_releases"]["web-1"]["runs"][0]["state"] == "failed"

    channel.add_request(*sync_request())  # every failed release again
    record = _release(controller.poll_once())
    assert record["state"] == "waiting-build" and record["trigger"] == "sync"
    assert record.get("failure") is None


def test_sync_publishes_the_baseline_tag(world: dict[str, Any], tmp_path: Path) -> None:
    controller, channel = world["controller"], world["channel"]
    assert _release(controller.poll_once())["state"] == "baseline"
    channel.add_request(*sync_request("web-1"))
    record = _release(controller.poll_once())
    assert record["state"] == "approval-required" and record["tag"] == "web-v1.0.0"
    assert record["trigger"] == "sync"


def test_a_host_without_the_caches_keys_claims_nothing(
    world: dict[str, Any], tmp_path: Path
) -> None:
    from piceli.infra.machines.credentials import save_credential
    from piceli.infra.machines.model import InfraError

    controller, channel = world["controller"], world["channel"]
    controller.poll_once()
    channel.add_request(*sync_request("web-1"))
    digest = _release(controller.poll_once())["plan_digest"]
    channel.add_request(*approve_request("web-1", digest))
    controller.poll_once()
    save_credential("cache-2", "nix-signing-key", OTHER)
    other = nix.NixCache(nix.public_key(OTHER), url=f"file://{world['cache']}")
    said: list[str] = []
    worker, _ = _worker(world, tmp_path, caches=[other], said=said)
    assert worker.once() is None
    assert not [key for key in channel.requests() if key.startswith("build-claim")]
    assert any("no keys for it" in line for line in said)
    assert _release(controller.poll_once())["state"] == "waiting-build"
    # Another build host than the release's builder never claims it either.
    elsewhere, _ = _worker(world, tmp_path, host="laptop")
    assert elsewhere.once() is None
    # A publisher refuses to start without a cache's key.
    missing = nix.NixCache(PUBLIC, url="file:///nowhere", signing_key="absent")
    with pytest.raises(InfraError) as refused:
        Publisher([missing], build=lambda *_a: STORE, push=lambda *_a: {}, host="mini")
    assert refused.value.code == "infra-credential-missing"
    mismatch = nix.NixCache(PUBLIC, url="file:///nowhere", signing_key="cache-2")
    with pytest.raises(DevError) as wrong:
        Publisher([mismatch], build=lambda *_a: STORE, push=lambda *_a: {}, host="mini")
    assert wrong.value.code == "dev-nix-key-mismatch"


def test_the_controller_refuses_claims_and_answers_it_cannot_trust(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(tmp_path, monkeypatch, auto=True)
    controller, channel, repo = world["controller"], world["channel"], world["repo"]
    controller.poll_once()
    commit = repo.commit({"a": "1"})
    repo.tag("web-v1.0.1")
    (wanted,) = controller.poll_once()["controller"]["builds_wanted"]
    build = wanted["id"]

    def last_rejection() -> str:
        return str(controller.poll_once()["rejected_requests"][-1]["message"])

    channel.add_request(*build_claim_request(build, "mini"))
    assert "no proof" in last_rejection()
    forged = nix.sign(OTHER, claim_message(build, "mini"))
    channel.add_request(*build_claim_request(build, "mini", forged))
    assert "no proof" in last_rejection()
    proof = nix.sign(SECRET, claim_message(build, "laptop"))
    channel.add_request(*build_claim_request(build, "laptop", proof))
    assert "built by mini, not laptop" in last_rejection()
    channel.add_request(
        *build_claim_request(
            build, "mini", nix.sign(SECRET, claim_message(build, "mini"))
        )
    )
    assert _release(controller.poll_once())["state"] == "publishing"

    def answer(**changes: Any) -> str:
        body = nix.manifest(
            name="web-1", commit=commit, tag="web-v1.0.1", target=ATTRIBUTE,
            published={"store_path": STORE, "nar_hash": "sha256:" + "0" * 52,
                       "nar_size": 1}, host="mini", key="cache-1",
        )  # fmt: skip
        body.update(changes.pop("fields", {}))
        signed = nix.sign_manifest(body, changes.pop("secret", SECRET))
        pointers = changes.pop(
            "pointers", [{"ref": "refs/tags/web-v1.0.1", "commit": commit}]
        )
        channel.add_request(
            *nix_published_request(build, changes.pop("host", "mini"), signed,
                                   pointers, {})
        )  # fmt: skip
        return last_rejection()

    assert "not signed by the cache's key" in answer(secret=nix.generate_key("cache-1"))
    assert "another tag" in answer(fields={"tag": "web-v9.9.9"})
    assert "another commit" in answer(fields={"commit": "f" * 40})
    assert "another pointer" in answer(
        pointers=[{"ref": "refs/tags/api-v1.0.0", "commit": commit}]
    )
    assert "holds no claim" in answer(host="laptop")
    assert _release(controller.poll_once())["state"] == "publishing"


def test_a_tag_moved_to_another_commit_is_not_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(tmp_path, monkeypatch, auto=True)
    controller, repo = world["controller"], world["repo"]
    controller.poll_once()
    repo.commit({"a": "1"})
    repo.tag("web-v1.0.1")
    controller.poll_once()
    moved = repo.commit({"a": "2"})
    git(repo.work, "tag", "-f", "web-v1.0.1", moved)
    git(repo.work, "push", "-q", "-f", "origin", "web-v1.0.1")
    worker, seen = _worker(world, tmp_path)
    assert worker.once() is not None
    assert not seen  # nothing was built
    failure = _release(controller.poll_once())["failure"]
    assert "points at" in failure["reason"] and moved[:12] in failure["reason"]


def test_a_quarantined_build_host_claims_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(tmp_path, monkeypatch, auto=True)
    controller, channel, repo = world["controller"], world["channel"], world["repo"]
    controller.poll_once()
    repo.commit({"a": "1"})
    repo.tag("web-v1.0.1")
    controller.poll_once()

    class Sick:
        def quarantined(self) -> dict[str, Any]:
            return {"reason": "segfault", "since": "2026-10-01T00:00:00Z"}

    worker, _ = _worker(world, tmp_path)
    worker.health = Sick()
    assert worker.once() is None
    assert not channel.requests()


def test_gitops_status_shows_each_release(
    world: dict[str, Any], tmp_path: Path
) -> None:
    from typer.testing import CliRunner

    from piceli.k8s.cli import app

    controller, repo = world["controller"], world["repo"]
    controller.poll_once()
    repo.commit({"a": "1"})
    repo.tag("web-v1.0.1")
    controller.poll_once()
    result = CliRunner().invoke(
        app, ["gitops", "status", "--state-dir", str(world["state"]), "--json"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    record = json.loads(result.stdout)["nix_releases"]["web-1"]
    assert record["state"] == "approval-required"
    assert "nix release web-1: approval-required web-v1.0.1" in result.stderr
    assert record["approve_command"] in result.stderr


def test_a_finished_release_is_a_trace_with_its_stages(tmp_path: Path) -> None:
    pytest.importorskip("opentelemetry.sdk")
    from piceli.infra.nix_releases import Releases
    from tests.unit.infra.test_controller_otel import Signals

    signals = Signals()
    telemetry = signals.telemetry(tmp_path, {"now": 1_790_000_000.0})
    record = {
        "state": "published", "tag": "web-v1.1.0", "commit": "c" * 40,
        "builder": "mini", "trigger": "tag web-v1.1.0", "plan_digest": "sha256:1",
        "requested_at": "2026-10-23T10:00:00Z", "started_at": "2026-10-23T10:05:00Z",
        "finished_at": "2026-10-23T10:20:00Z", "approval": {"via": "cli"},
        "stages": {
            "approval": {"started_at": "2026-10-23T10:00:00Z",
                         "finished_at": "2026-10-23T10:04:00Z"},
            "build": {"started_at": "2026-10-23T10:05:10Z",
                      "finished_at": "2026-10-23T10:15:00Z"},
        },
        "published": {"host": "mini", "manifest_sha256": "sha256:" + "e" * 64},
    }  # fmt: skip
    telemetry.nix_release(Releases.event("web-1", record))
    spans = {s.name: s for s in signals.spans.get_finished_spans()}
    root = spans["PUBLISH web-1 web-v1.1.0"]
    assert root.attributes["piceli.nix.state"] == "published"
    assert root.attributes["piceli.nix.manifest_sha256"] == "sha256:" + "e" * 64
    assert (root.end_time - root.start_time) / 1e9 == 1200.0
    assert {n for n, s in spans.items() if s.parent is not None} == {
        "approval",
        "build",
    }
    (event,) = signals.events("piceli.nix.release.finished")
    assert event.trace_id == root.context.trace_id


def test_builder_serve_refuses_a_publish_cache_it_has_no_key_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    import piceli.k8s.cli.builder as builder_cli
    import piceli.k8s.cli.dev_nix as dev_nix
    from piceli.dev.model import BuildHost, DevBuilds, DevProfile
    from piceli.infra import Cluster
    from piceli.k8s.cli import app

    monkeypatch.setenv("PICELI_CREDENTIALS_DIR", str(tmp_path / "credentials"))
    dev = DevBuilds(
        node=None, image="ghcr.io/example/builder@sha256:" + "a" * 64,
        profiles=[DevProfile.for_nix()],
        hosts=[BuildHost.native("mini", state_dir=str(tmp_path / "mini"))],
    )  # fmt: skip
    cluster = Cluster("c", api="https://10.255.0.1:6443", credentials="c", dev=dev)
    monkeypatch.setattr(builder_cli, "load_cluster", lambda _ref: cluster)
    monkeypatch.setattr(
        dev_nix, "load_cache", lambda _r: nix.NixCache(PUBLIC, url="file:///c")
    )
    result = CliRunner().invoke(
        app, ["builder", "serve", "--cluster", "infra.py:c", "--host", "mini",
              "--publish", "infra.py:cache", "--once"],
    )  # fmt: skip
    assert result.exit_code == 2, result.stdout + result.stderr
    body = json.loads(result.stdout)
    assert body["state"] == "rejected"
    assert body["reason"] == "infra-credential-missing"
    # Without --publish a native host is no build host for images.
    plain = CliRunner().invoke(
        app, ["builder", "serve", "--cluster", "infra.py:c", "--host", "mini"]
    )
    assert json.loads(plain.stdout)["reason"] == "dev-host-unknown"


class _Wanted:
    """A controller status with the given ``builds_wanted``."""

    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self.entries = entries

    def read_status(self) -> dict[str, Any]:
        return {"controller": {"builds_wanted": self.entries}}


def test_a_publish_only_host_never_claims_an_image_build_and_says_so_once(
    world: dict[str, Any], tmp_path: Path
) -> None:
    said: list[str] = []
    worker, _ = _worker(world, tmp_path, said=said)
    image_build = {"id": "build-1", "kind": "spec", "claimed": {}}
    worker.channel = _Wanted([image_build])
    assert worker.pick() is None
    assert worker.pick() is None
    skipped = [line for line in said if "image build" in line]
    assert len(skipped) == 1, said
    assert "only publishes" in skipped[0]
    publish = {"id": "nix-1", "kind": "nix-publish", "builder": "mini",
               "release": "web-1", "claimed": {},
               "plan": {"cache": {"store_url": _cache(world).store_url,
                                  "public_key": PUBLIC}}}  # fmt: skip
    worker.channel = _Wanted([image_build, publish])
    assert worker.pick() == publish
    assert len([line for line in said if "image build" in line]) == 1


def _serve_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *args: str,
    runs: bool = True,
    host: str | None = "nix-builder",
) -> tuple[Any, dict[str, Any]]:
    """Run ``builder serve`` on a docker host of a cluster with an in-cluster
    registry, up to the worker it starts (no cluster, no docker)."""
    import contextlib
    from types import SimpleNamespace

    from typer.testing import CliRunner

    import piceli.dev.health as health
    import piceli.infra.builder_serve as serve_module
    import piceli.k8s.cli.builder as builder_cli
    import piceli.profiles as profiles
    from piceli.dev.hosts import ContainerCli
    from piceli.dev.model import BuildHost, DevBuilds, DevProfile
    from piceli.infra import Cluster
    from piceli.k8s.cli import app
    from piceli.pipeline.model import Registry

    dev = DevBuilds(
        node=None, image="ghcr.io/example/builder@sha256:" + "a" * 64,
        profiles=[DevProfile.for_nix()],
        hosts=[BuildHost.docker("nix-builder", runs=runs,
                                context="ssh://builder@builder-1.example.org"),
               BuildHost.docker("laptop")],
    )  # fmt: skip
    cluster = Cluster(
        "c", api="https://10.255.0.1:6443", credentials="c", dev=dev,
        registry=Registry.in_cluster(on="node-a"),
    )  # fmt: skip
    started: dict[str, Any] = {}

    class Worker:
        def __init__(self, **kwargs: Any) -> None:
            started.update(kwargs)
            self.quarantine = None

        def once(self) -> None:
            return None

    monkeypatch.setattr(builder_cli, "load_cluster", lambda _ref: cluster)
    monkeypatch.setattr(
        builder_cli, "_publisher", lambda *_a: SimpleNamespace(caches=[])
    )
    monkeypatch.setattr(
        builder_cli,
        "connect",
        lambda *_a: contextlib.nullcontext(SimpleNamespace(client=None)),
    )
    monkeypatch.setattr(serve_module, "BuilderServe", Worker)
    monkeypatch.setattr(serve_module, "ensure_image", lambda *_a: None)
    monkeypatch.setattr(serve_module, "prepare_cache", lambda *_a: None)
    monkeypatch.setattr(ContainerCli, "platform", lambda _self: "linux/amd64")
    monkeypatch.setattr(health.Health, "of", classmethod(lambda *_a: None))
    monkeypatch.setattr(
        profiles, "resolve", lambda _p: SimpleNamespace(kubeconfig=None, context=None)
    )
    result = CliRunner().invoke(
        app, ["builder", "serve", "--cluster", "infra.py:c", "--once",
              *(("--host", host) if host else ()), *args],
    )  # fmt: skip
    return result, started


def test_builder_serve_publish_only_on_a_docker_host_builds_no_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, started = _serve_cli(
        tmp_path, monkeypatch, "--publish", "infra.py:cache", "--publish-only"
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert started["images"] is False
    assert started["push"] is None
    assert started["publisher"] is not None


def test_builder_serve_publish_only_needs_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, started = _serve_cli(tmp_path, monkeypatch, "--publish-only")
    assert result.exit_code == 2, result.stdout + result.stderr
    assert json.loads(result.stdout)["reason"] == "dev-nix-invalid"
    assert not started


def test_a_host_declared_runs_false_only_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # --publish-only is implied.
    result, started = _serve_cli(
        tmp_path, monkeypatch, "--publish", "infra.py:cache", runs=False
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert started["host"] == "nix-builder" and started["images"] is False
    # It takes no image build: without --publish, or told to build images,
    # it is refused.
    for args in ((), ("--publish", "infra.py:cache", "--no-publish-only")):
        refused, nothing = _serve_cli(tmp_path, monkeypatch, *args, runs=False)
        assert refused.exit_code == 2, refused.stdout + refused.stderr
        assert json.loads(refused.stdout)["reason"] == "dev-host-publish-only"
        assert "runs=False" in refused.stderr
        assert not nothing
    # The default host of image builds is never it.
    plain, other = _serve_cli(tmp_path, monkeypatch, runs=False, host=None)
    assert plain.exit_code == 0, plain.stdout + plain.stderr
    assert other["host"] == "laptop" and other["images"] is True


UNSET = "dev-nix-cache-key-unset"
KEYLESS = INFRA.replace(
    'NixCache(os.environ["NIX_RELEASE_PUBLIC"],', 'NixCache(key_name="cache-1",'
)


def test_a_release_whose_cache_has_no_key_is_rejected_and_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from piceli.k8s.cli import app

    assert KEYLESS != INFRA
    world = _world(tmp_path, monkeypatch, infra=KEYLESS)  # the composition loads
    (target,) = world["composition"].nix_releases
    assert target.public_key is None
    controller, repo = world["controller"], world["repo"]
    status = controller.poll_once()
    record = _release(status)
    assert (record["state"], record["reason"]) == ("rejected", UNSET)
    assert "piceli secrets setup MODULE" in record["refusal"]["message"]
    assert record["plan"] is None and record["latest_tag"] == "web-v1.0.0"
    repo.commit({"hosts/web-1.nix": "{ }\n"})
    repo.tag("web-v1.1.0")
    status = controller.poll_once()
    assert _release(status)["state"] == "rejected"
    assert status["controller"]["builds_wanted"] == []
    result = CliRunner().invoke(
        app, ["gitops", "status", "--state-dir", str(world["state"]), "--json"]
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    assert f"nix release web-1: rejected ({UNSET}: NixCache cache-1" in result.stderr
    # gitops enable warns about it and goes on.
    image = "ghcr.io/example/piceli@sha256:" + "0" * 64
    enabled = CliRunner().invoke(
        app, ["gitops", "enable", str(tmp_path / "infra.py"), "--image", image]
    )
    assert "warning: nix release web-1: NixCache cache-1 has no public key" in (
        enabled.stderr
    ), enabled.output
    assert json.loads(enabled.stdout)["reason"] != UNSET
    # Once the key is declared, the tags it sees are the baseline.
    import dataclasses

    keyed = dataclasses.replace(target, public_key=PUBLIC, key_unset=None)
    controller.nix.poll([keyed], controller.refs)
    record = controller.nix.records()["web-1"]
    assert (record["state"], record["tag"]) == ("baseline", "web-v1.1.0")
    assert "reason" not in record and "refusal" not in record


# ------------------------------------------------------------------ 0.26.0: inputs

APP = "ssh://git@github.com/org/app"
FLAKE_LOCK = json.dumps(
    {
        "version": 7,
        "root": "root",
        "nodes": {
            "root": {"inputs": {"app": "app", "nixpkgs": "nixpkgs"}},
            "app": {
                "locked": {
                    "type": "git",
                    "url": APP,
                    "rev": "c" * 40,
                    "narHash": "sha256-AAAA",
                }
            },
            "nixpkgs": {
                "locked": {
                    "type": "github",
                    "owner": "NixOS",
                    "repo": "nixpkgs",
                    "rev": "d" * 40,
                    "narHash": "sha256-BBBB",
                }
            },
        },
    }
)


def _inputs_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    infra = INFRA.replace(
        "auto_approve=AUTO)", 'auto_approve=AUTO, inputs={"app": "app-token"})'
    )
    world = _world(tmp_path, monkeypatch, auto=True, infra=infra)
    world["controller"].poll_once()
    world["commit"] = world["repo"].commit({"flake.lock": FLAKE_LOCK})
    world["repo"].tag("web-v1.1.0")
    return world


def test_the_plan_names_the_private_inputs_and_the_host_fetches_them_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _inputs_world(tmp_path, monkeypatch)
    controller = world["controller"]
    record = _release(controller.poll_once())
    assert record["state"] == "waiting-build"
    assert record["plan"]["inputs"] == [
        {"input": "app", "url": f"git+{APP}", "credential": "app-token",
         "why": "declared"},
    ]  # fmt: skip
    assert world["composition"].to_dict()["nix_releases"][0]["inputs"] == {
        "app": "app-token"
    }
    order: list[str] = []

    reference = f"/nix/store/{'l' * 32}-flake.lock"

    def fetch(pairs: Any, say: Any, **kw: Any) -> dict[str, Any]:
        ((entry, locked),) = pairs
        order.append("fetch")
        assert entry["credential"] == "app-token" and locked["url"] == APP
        assert b'"nodes"' in kw["lock"]  # 0.26.3: the checkout's flake.lock
        return {"state": "fetched", "inputs": [{"name": "app"}], "lock": reference}

    def build(checkout: Path, attribute: str, say: Any, lock: str) -> str:
        # 0.26.3: the build reads the fetched trees from the reference lock.
        assert lock == reference
        order.append("build")
        return STORE

    worker, _ = _worker(world, tmp_path, fetch=fetch, build=build)
    assert worker.once() is not None
    done = _release(controller.poll_once())
    assert done["state"] == "published", done.get("failure")
    assert order == ["fetch", "build"]
    assert {"inputs", "build", "publish"} <= set(done["stages"])


def test_an_unreachable_input_fails_the_release_before_any_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _inputs_world(tmp_path, monkeypatch)
    controller = world["controller"]
    controller.poll_once()
    built: list[str] = []

    def fetch(pairs: Any, say: Any, **_kw: Any) -> dict[str, Any]:
        raise DevError(
            "infra-nix-input-unreachable",
            f"app git+{APP} (credential app-token over HTTPS): fatal: "
            "Authentication failed",
        )

    worker, _ = _worker(
        world, tmp_path, fetch=fetch, build=lambda *_a: built.append("x") or STORE
    )
    assert worker.once() is not None
    failed = _release(controller.poll_once())
    assert failed["state"] == "failed" and not built
    assert failed["failure"]["reason"] == (
        f"infra-nix-input-unreachable: app git+{APP} (credential app-token over "
        "HTTPS): fatal: Authentication failed"
    )
    # A host without a fetcher refuses the plan the same way.
    controller.channel.add_request(*sync_request())
    controller.poll_once()
    worker, _ = _worker(
        world, tmp_path, fetch=None, build=lambda *_a: built.append("x") or STORE
    )
    assert worker.once() is not None
    failed = _release(controller.poll_once())
    assert failed["failure"]["reason"].startswith(
        f"infra-nix-input-unreachable: app git+{APP}: this build host fetches no"
    )
    assert not built


def test_a_release_without_private_inputs_keeps_its_plan(world: dict[str, Any]) -> None:
    world["controller"].poll_once()
    world["repo"].commit({"flake.lock": FLAKE_LOCK})
    world["repo"].tag("web-v1.1.0")
    record = _release(world["controller"].poll_once())
    assert "inputs" not in record["plan"]  # nixpkgs is public; nothing declared
    assert "inputs" not in world["composition"].to_dict()["nix_releases"][0]


def test_declared_inputs_are_checked(world: dict[str, Any]) -> None:
    from piceli.infra import NixRelease, Source

    release = next(iter(world["composition"].nix_releases))
    host = _host_of(world)
    with pytest.raises(CompositionError):
        NixRelease(host, source=Source(release.source.url, name="hosts"),
                   builder="mini", inputs={"app": "Not A Name"})  # fmt: skip
    found = NixRelease(host, source=Source(release.source.url, name="hosts"),
                       builder="mini", inputs={"app": "git"})  # fmt: skip
    assert found.target().inputs == (("app", "git"),)


def _host_of(world: dict[str, Any]) -> Any:
    from piceli.infra import NixCache, NixHost, Tag

    cache = NixCache(PUBLIC, url=f"file://{world['cache']}")
    return NixHost("web-1", server="ssh://root@web-1.example.org", flake=".#web-1",
                   follow=Tag("web-v*"), cache=cache)  # fmt: skip


GIT_TOKEN = "ghs_" + "c0ntr0ll3r" * 3


def test_a_controller_release_seals_its_git_token_to_the_claiming_host_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import piceli.dev.nix_inputs as nix_inputs

    secret = tmp_path / "git-secret"
    secret.mkdir()
    (secret / "username").write_text("x-access-token")
    (secret / "password").write_text(GIT_TOKEN)
    monkeypatch.setattr(nix_inputs, "git_hosts", lambda _sources: ["github.com"])
    infra = INFRA.replace(
        "auto_approve=AUTO)",
        'auto_approve=AUTO, inputs={"app": "unused"}, credentials="controller")',
    )
    world = _world(tmp_path, monkeypatch, auto=True, infra=infra,
                   credentials_dir=secret)  # fmt: skip
    controller = world["controller"]
    controller.poll_once()
    world["repo"].commit({"flake.lock": FLAKE_LOCK})
    world["repo"].tag("web-v1.1.0")
    record = _release(controller.poll_once())
    plan = record["plan"]
    assert plan["credentials"] == "controller" and plan["git_hosts"] == ["github.com"]
    assert plan["inputs"][0]["credential"] == "controller"
    (wanted,) = controller.poll_once()["controller"]["builds_wanted"]

    # A claim without a key to seal to is refused; nothing is sealed.
    proof = Publisher([_cache(world)], build=lambda *_a: STORE,
                      push=lambda *_a: {}, host="mini").proof(wanted, "mini")  # fmt: skip
    world["channel"].add_request(*build_claim_request(wanted["id"], "mini", proof))
    status = controller.poll_once()
    assert status["rejected_requests"][-1]["reason"] == "gitops-request-invalid"

    seen: list[Any] = []

    def fetch(
        pairs: Any, say: Any, controller: Any = None, **_kw: Any
    ) -> dict[str, Any]:
        seen.append(controller)
        return {"state": "fetched", "inputs": []}

    worker, _ = _worker(world, tmp_path, fetch=fetch)
    assert worker.once() is not None
    done = _release(controller.poll_once())
    assert done["state"] == "published", done.get("failure")
    assert seen == [{"hosts": ["github.com"], "username": "x-access-token",
                     "token": GIT_TOKEN}]  # fmt: skip
    assert worker._openers == {} and worker._sealed == {}  # forgotten
    # The token is in no status, history, request or state file.
    for path in world["state"].rglob("*"):
        if path.is_file():
            assert GIT_TOKEN.encode() not in path.read_bytes(), path


def test_the_sealed_token_opens_only_with_the_claims_key_and_build() -> None:
    from piceli.dev import nix_inputs

    private, public = nix_inputs.new_recipient()
    other, _ = nix_inputs.new_recipient()
    aad = nix_inputs.seal_aad("b-1", "mini")
    sealed = nix_inputs.seal(public, {"token": GIT_TOKEN}, aad)
    assert GIT_TOKEN not in json.dumps(sealed)
    assert nix_inputs.unseal(private, sealed, aad) == {"token": GIT_TOKEN}
    for key, extra in ((other, aad), (private, nix_inputs.seal_aad("b-1", "evil"))):
        with pytest.raises(ValueError):
            nix_inputs.unseal(key, sealed, extra)


def test_builder_check_lists_each_release_and_its_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from piceli.dev import nix_inputs

    world = _inputs_world(tmp_path, monkeypatch)
    status = world["controller"].poll_once()
    (found,) = nix_inputs.host_check(status, "mini")
    assert found["credentials"] == "host" and found["planned"]
    assert found["inputs"] == [{"input": "app", "url": f"git+{APP}",
                                "credential": "app-token", "state": "missing"}]  # fmt: skip
    nix_inputs.save_git_token("app-token", "github.com", "git", "tok-" + "q" * 20)
    assert nix_inputs.host_check(status, "mini")[0]["inputs"][0]["state"] == "present"
    assert nix_inputs.host_check(status, "other") == []
