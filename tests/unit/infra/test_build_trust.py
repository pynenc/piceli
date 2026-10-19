"""0.22: provenance per deployed image, host quarantine, suspects and rebuilds.

The composition controller against local bare Git sources (no cluster):
builds are fakes that report where they ran, or build hosts answered by the
test. Provenance is kept with each component (status, cache, history); the
images one host built are listed and rebuilt; repeated machine-health signs
quarantine the builder node or a host.
"""

from __future__ import annotations

import io
import json
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.gitops.state import (
    DirectoryChannel,
    build_claim_request,
    build_failed_request,
    built_request,
    host_release_request,
    rebuild_request,
    sync_request,
)
from piceli.infra import CompositionError, provenance
from piceli.infra.builders import BuildItem, BuiltImage
from piceli.infra.composition import load_composition
from piceli.infra.controller import CompositionConfig, CompositionController
from piceli.infra.sources import SourceSet
from tests.unit.infra.git_support import Repo, example_files
from tests.unit.infra.test_controller import EXAMPLE, FakeBuilder, FakePorts

BUILDER = "ghcr.io/example/builder@sha256:" + "e" * 64
SEGFAULT = "collect2: fatal error: ld terminated with signal 11 [Segmentation fault]"


class _Node:
    def __init__(self, name: str) -> None:
        self.node_name = name


class NodeBuilder(FakeBuilder):
    """An in-cluster builder that reports the node its Job ran on, or fails
    with ``tail`` as the Job's output."""

    def __init__(self, node: str) -> None:
        super().__init__()
        self.cluster = _Node(node)
        self.tail: str | None = None

    def build(self, items: Sequence[BuildItem], checkout: Any) -> dict[str, BuiltImage]:
        if self.tail is not None:
            error = CompositionError("component-build-failed", "the build Job failed")
            error.output_tail = self.tail  # type: ignore[attr-defined]
            self.built.append(["failed"])
            raise error
        built = super().build(items, checkout)
        for item in items:
            built[item.component].extra["provenance"] = provenance.record(
                builder="cluster-job",
                host=self.cluster.node_name,
                node=self.cluster.node_name,
                builder_image=BUILDER,
                commits={item.source: item.commit},
                started_at="2026-10-18T10:00:00Z",
                finished_at="2026-10-18T10:04:00Z",
            )
        return built


def _world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, controller: str | None = None
) -> dict[str, Any]:
    shop = Repo(tmp_path / "remotes", "shop", example_files(EXAMPLE / "shop"))
    catalog = Repo(tmp_path / "remotes", "catalog", example_files(EXAMPLE / "catalog"))
    catalog.tag("v0.1.0")
    monkeypatch.setenv("COMPOSITION_SHOP_URL", shop.url)
    monkeypatch.setenv("COMPOSITION_CATALOG_URL", catalog.url)
    text = (EXAMPLE / "infra.py").read_text()
    if controller is not None:
        text = text.replace("controller=Controller(on=REGISTRY_NODE)", controller)
    (tmp_path / "infra.py").write_text(text)
    composition = load_composition(tmp_path / "infra.py")
    config = CompositionConfig(composition=composition.to_dict(), poll_seconds=10)
    state = tmp_path / "state"
    ports = FakePorts()
    ports.builder = NodeBuilder("amedes")  # type: ignore[assignment]
    channel = DirectoryChannel(state)
    clock = {"now": 1_000_000.0}
    return {
        "controller": CompositionController(
            config,
            state_dir=state,
            sources=SourceSet(composition.sources, state / "sources"),
            ports=ports,
            channel=channel,
            clock=lambda: clock["now"],
        ),
        "ports": ports,
        "channel": channel,
        "clock": clock,
        "state": state,
        "shop": shop,
    }


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    return _world(tmp_path, monkeypatch)


@pytest.fixture
def hosts_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    return _world(
        tmp_path,
        monkeypatch,
        'controller=Controller(on=REGISTRY_NODE, builds=("hosts", "cluster"), '
        'host_wait="10m")',
    )


def _gitops(*argv: str) -> Any:
    from piceli.k8s.cli import app

    return CliRunner().invoke(app, ["gitops", *argv])


def _retry(world: dict[str, Any]) -> dict[str, Any]:
    """The next poll after the environment's retry time."""
    controller = world["controller"]
    record = controller.state["envs"]["main"]
    world["clock"]["now"] = (
        max(world["clock"]["now"], float(record.get("next_attempt_at") or 0)) + 1
    )
    return controller.poll_once()


# ------------------------------------------------------------------ provenance


def test_own_builds_keep_their_provenance_in_status_cache_and_history(
    world: dict[str, Any],
) -> None:
    controller, state = world["controller"], world["state"]
    status = controller.poll_once()
    main = status["envs"]["main"]
    assert main["state"] == "deployed"
    web = main["components"]["web"]
    assert web["provenance"] == {
        "builder": "cluster-job",
        "host": "amedes",
        "node": "amedes",
        "builder_image": BUILDER,
        "commits": {"shop": main["revision"]["shop"]},
        "started_at": "2026-10-18T10:00:00Z",
        "finished_at": "2026-10-18T10:04:00Z",
    }
    assert "provenance" not in main["components"]["cache"]  # a mirrored image
    cached = json.loads(
        (state / "components" / "web" / f"{web['source_digest'][7:]}.json").read_text()
    )
    assert cached["provenance"]["host"] == "amedes"
    (run,) = controller._history_document()["envs"]["main"]["runs"]
    by_name = {item["name"]: item for item in run["components"]}
    assert by_name["web"]["provenance"]["host"] == "amedes"
    assert by_name["web"]["provenance"]["builder_image"] == BUILDER
    # A sync reuses the cached image: its provenance comes back with it.
    world["channel"].add_request(*rebuild_request("nobody"))
    from piceli.gitops.state import sync_request

    world["channel"].add_request(*sync_request("main"))
    status = controller.poll_once()
    assert status["envs"]["main"]["components"]["web"]["provenance"]["host"] == "amedes"
    rejected = status["rejected_requests"][-1]
    assert rejected["kind"] == "rebuild" and "no deployed image" in rejected["message"]


def test_host_built_images_keep_the_hosts_receipt(hosts_world: dict[str, Any]) -> None:
    controller, channel = hosts_world["controller"], hosts_world["channel"]
    (wanted,) = controller.poll_once()["controller"]["builds_wanted"]
    channel.add_request(*build_claim_request(wanted["id"], "mini"))
    controller.poll_once()
    images = {
        item["component"]: {
            "key": item["digest"],
            "pull_ref": f"registry.example:5000/{item['repository']}@sha256:{'d' * 64}",
            "manifest_digest": "sha256:" + "d" * 64,
            "platforms": {},
        }
        for item in wanted["components"]
    }
    receipt = {
        "host": "mini",
        "image": BUILDER,
        "started_at": "2026-10-18T09:00:00Z",
        "finished_at": "2026-10-18T09:30:00Z",
        "seconds": 1800,
        "platforms": ["linux/amd64"],
        "commits": {"shop": "1" * 40},
        "toolchains": {"rustc": "1.90.0"},
    }
    channel.add_request(*built_request(wanted["id"], "mini", images, receipt))
    status = controller.poll_once()
    web = status["envs"]["main"]["components"]["web"]
    assert web["provenance"]["host"] == "mini"
    assert web["provenance"]["builder"] == "host"
    assert web["provenance"]["toolchains"] == {"rustc": "1.90.0"}
    assert web["provenance"]["finished_at"] == "2026-10-18T09:30:00Z"


# ------------------------------------------------------------------ suspects


def test_suspects_list_what_a_host_built_filtered_by_time(
    world: dict[str, Any],
) -> None:
    controller, state = world["controller"], world["state"]
    status = controller.poll_once()
    found = provenance.suspects(status, "amedes")
    assert sorted(item["component"] for item in found) == ["api", "catalog", "web"]
    assert all(item["env"] == "main" for item in found)
    assert provenance.suspects(status, "mini") == []
    since = provenance.parse_since("2026-10-18T10:03:00Z")
    assert len(provenance.suspects(status, "amedes", since)) == 3  # ended 10:04
    later = provenance.parse_since("2026-10-18T10:05:00Z")
    assert provenance.suspects(status, "amedes", later) == []
    shown = _gitops(
        "suspects", "--host", "amedes", "--since", "2026-10-18", "--state-dir",
        str(state), "--json",
    )  # fmt: skip
    assert shown.exit_code == 0, shown.stderr
    body = json.loads(shown.stdout)
    assert body["schema"] == "piceli.gitops-suspects.v1"
    assert sorted(item["component"] for item in body["images"]) == [
        "api",
        "catalog",
        "web",
    ]
    assert "rebuild them: piceli gitops rebuild --host amedes" in shown.stderr


# ------------------------------------------------------------------ rebuild


def test_a_rebuild_request_drops_the_cached_builds_and_rebuilds_them(
    world: dict[str, Any],
) -> None:
    controller, ports, state = world["controller"], world["ports"], world["state"]
    status = controller.poll_once()
    web = status["envs"]["main"]["components"]["web"]
    cache = state / "components" / "web" / f"{web['source_digest'][7:]}.json"
    assert ports.builder.built == [["api", "catalog", "web"]]
    ports.builder.cluster = _Node("pi01")  # a sound node builds this time
    requested = _gitops("rebuild", "--host", "amedes", "--state-dir", str(state))
    assert requested.exit_code == 0, requested.stderr
    assert json.loads(requested.stdout)["state"] == "requested"
    status = controller.poll_once()
    assert ports.builder.built == [["api", "catalog", "web"]] * 2
    main = status["envs"]["main"]
    assert main["state"] == "deployed"
    assert {
        c["provenance"]["host"] for n, c in main["components"].items() if n != "cache"
    } == {"pi01"}
    assert json.loads(cache.read_text())["provenance"]["host"] == "pi01"
    last = status["controller"]["last_rebuild"]
    assert last["host"] == "amedes" and last["envs"] == {
        "main": ["api", "catalog", "web"]
    }
    assert last["dropped"] == 3
    run = controller._history_document()["envs"]["main"]["runs"][0]
    assert run["trigger"] == "sync rebuild amedes"
    assert sorted(run["built"]) == ["api", "catalog", "web"]
    # Nothing left that the old node built.
    assert provenance.suspects(status, "amedes") == []


def test_rebuild_requests_are_checked() -> None:
    from piceli.gitops import GitOpsError

    with pytest.raises(GitOpsError):
        rebuild_request("Not_A_Host")
    with pytest.raises(GitOpsError):
        rebuild_request("mini", since="last tuesday")
    key, body = rebuild_request("mini", since="2026-10-01")
    assert key.startswith("rebuild.") and body["since"] == "2026-10-01"


# ------------------------------------------------------------------ quarantine


def test_repeated_segfaults_quarantine_the_builder_node_until_released(
    world: dict[str, Any],
) -> None:
    controller, ports, state = world["controller"], world["ports"], world["state"]
    ports.builder.tail = f"Compiling ring v0.17\n{SEGFAULT}\nerror: could not compile"
    status = controller.poll_once()
    assert status["envs"]["main"]["reason"] == "component-build-failed"
    health = status["controller"]["build_health"]["amedes"]
    assert health["kind"] == "node" and health["recent"] == 1
    assert health["quarantined"] is False
    assert health["events"][0]["kind"] == "segfault"
    status = _retry(world)
    health = status["controller"]["build_health"]["amedes"]
    assert health["quarantined"] is True and "2 health events" in health["reason"]
    calls = len(ports.builder.built)
    status = _retry(world)
    main = status["envs"]["main"]
    assert main["reason"] == "build-host-quarantined"
    assert "amedes is quarantined" in str(main["failure"])
    assert len(ports.builder.built) == calls  # nothing ran on the node
    assert status["controller"]["build_health"]["amedes"]["recent"] == 2
    shown = _gitops("hosts", "--state-dir", str(state), "--json")
    assert shown.exit_code == 0, shown.stderr
    assert json.loads(shown.stdout)["hosts"]["amedes"]["quarantined"] is True
    assert "QUARANTINED" in shown.stderr
    ports.builder.tail = None  # the node was fixed
    released = _gitops("release-host", "amedes", "--state-dir", str(state))
    assert released.exit_code == 0, released.stderr
    status = _retry(world)
    assert status["envs"]["main"]["state"] == "deployed"
    assert "build_health" not in status["controller"]


def test_an_ordinary_build_failure_records_no_health_event(
    world: dict[str, Any],
) -> None:
    controller, ports = world["controller"], world["ports"]
    ports.builder.tail = "error[E0308]: mismatched types\nerror: could not compile"
    status = controller.poll_once()
    assert status["envs"]["main"]["reason"] == "component-build-failed"
    assert "build_health" not in status["controller"]


def test_a_quarantined_host_cannot_claim_and_a_quarantined_node_is_no_fallback(
    hosts_world: dict[str, Any],
) -> None:
    controller, channel = hosts_world["controller"], hosts_world["channel"]
    ports, clock = hosts_world["ports"], hosts_world["clock"]
    (wanted,) = controller.poll_once()["controller"]["builds_wanted"]
    channel.add_request(
        *build_failed_request(wanted["id"], "mini", "the build exited 1", SEGFAULT)
    )
    status = controller.poll_once()
    assert status["controller"]["build_health"]["mini"]["recent"] == 1
    controller._record_health("mini", "host", {"kind": "segfault"}, "build-failed")
    # A failed build is offered again only on an explicit sync (0.22.0).
    channel.add_request(*sync_request("main"))
    status = _retry(hosts_world)
    (wanted,) = status["controller"]["builds_wanted"]
    channel.add_request(*build_claim_request(wanted["id"], "mini"))
    status = controller.poll_once()
    assert status["controller"]["builds_wanted"][0]["claimed"] is None
    refused = status["rejected_requests"][-1]
    assert (
        refused["kind"] == "build-claim" and "mini is quarantined" in refused["message"]
    )
    # The builder node is quarantined too: past host_wait nothing falls back.
    for _ in range(2):
        controller._record_health(
            "amedes", "node", {"kind": "machine-check"}, "build-job"
        )
    clock["now"] += 11 * 60
    status = controller.poll_once()
    assert ports.builder.built == []
    assert status["envs"]["main"]["reason"] == "build-waiting"
    channel.add_request(*host_release_request("amedes"))
    clock["now"] += 11 * 60
    status = controller.poll_once()
    assert ports.builder.built == [["api", "catalog", "web"]]
    assert status["envs"]["main"]["state"] == "deployed"


def test_the_controller_policy_is_declared() -> None:
    from piceli.infra import Controller
    from piceli.infra.cluster import ClusterError

    Controller(on="pi00", quarantine_after=3, quarantine_window="12h")
    Controller(on="pi00", quarantine_after=None)
    with pytest.raises(ClusterError):
        Controller(on="pi00", quarantine_after=0)
    with pytest.raises(ClusterError):
        Controller(on="pi00", quarantine_window="a while")


# ------------------------------------------------------------------ builder serve


class _Channel:
    def __init__(self, entry: dict[str, Any]) -> None:
        self.entry = entry
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.reads = 0

    def read_status(self) -> dict[str, Any]:
        self.reads += 1
        return {"controller": {"builds_wanted": [self.entry]}}

    def add_request(self, key: str, body: dict[str, Any]) -> None:
        self.requests.append((key, body))


def _serve(tmp_path: Path, container: Any, **options: Any) -> Any:
    from piceli.dev.health import Health
    from piceli.dev.model import BuildHost
    from piceli.infra.builder_serve import BuilderServe

    host = BuildHost.native("mini", state_dir=str(tmp_path / "mini"))
    entry = {
        "id": "a" * 20,
        "kind": "components",
        "components": [],
        "images": [],
        "platforms": ["linux/amd64"],
        "claimed": {"host": "mini"},
    }
    lines: list[str] = []
    worker = BuilderServe(
        host="mini",
        image=BUILDER,
        channel=_Channel(entry),
        container=container,
        push=lambda *_a: "",
        index=lambda *_a: "",
        root=tmp_path / "builder",
        say=lines.append,
        health=Health.of(host),
        **options,
    )
    return worker, lines


def test_builder_serve_records_health_and_a_quarantined_host_claims_nothing(
    tmp_path: Path,
) -> None:
    def crashing(argv: list[str], sources: Any, out: Path, line: Any) -> int:
        line("Compiling tokio v1.40.0")
        line("error: internal compiler error: compiler/rustc_codegen_ssa")
        return 101

    worker, lines = _serve(tmp_path, crashing)
    for _ in range(2):
        worker.answered.clear()  # offered again (an explicit gitops sync)
        assert worker.once() == "a" * 20
    failed = [body for _key, body in worker.channel.requests]
    assert [body["kind"] for body in failed] == ["build-failed", "build-failed"]
    assert any("health event on mini: internal-compiler-error" in x for x in lines)
    assert worker.quarantine is not None
    reads = worker.channel.reads
    assert worker.once() is None
    assert worker.channel.reads == reads  # did not even look at the builds
    assert sum("is quarantined" in x for x in lines) == 1  # said once
    worker.health.release()
    assert worker.quarantined() is None
    assert any("released; claiming builds again" in x for x in lines)


def test_builder_serve_claims_nothing_while_the_controller_quarantines_it(
    tmp_path: Path,
) -> None:
    worker, lines = _serve(tmp_path, lambda *_a: 0)
    worker.channel.entry["claimed"] = None
    held = {
        "quarantined": True,
        "since": "2026-10-18T09:00:00Z",
        "reason": "2 health events",
    }

    def status() -> dict[str, Any]:
        return {"controller": {"builds_wanted": [worker.channel.entry],
                               "build_health": {"mini": held}}}  # fmt: skip

    worker.channel.read_status = status
    assert worker.once() is None and worker.once() is None
    assert worker.channel.requests == []  # no build-claim sent
    assert sum("the controller quarantined mini" in x for x in lines) == 1


def test_builder_serve_attaches_each_images_provenance(tmp_path: Path) -> None:
    attached: list[tuple[str, str, bytes]] = []

    def attest(repository: str, digest: str, body: bytes) -> dict[str, Any]:
        attached.append((repository, digest, body))
        if repository.endswith("api"):
            raise OSError("registry unreachable")
        return {"state": "attached", "digest": "sha256:" + "7" * 64}

    worker, lines = _serve(tmp_path, lambda *_a: 0, attest=attest)
    digest = "sha256:" + "9" * 64
    images = {
        name: {
            "pull_ref": f"reg.svc:5000/shop/{name}@{digest}",
            "manifest_digest": digest,
        }
        for name in ("web", "api")
    }
    record = provenance.record(
        builder="host", host="mini", builder_image=BUILDER,
        commits={"shop": "1" * 40}, started_at="2026-10-18T09:00:00Z",
        finished_at="2026-10-18T09:30:00Z",
    )  # fmt: skip
    found = worker.attach({}, images, record)
    assert found == {"web": "sha256:" + "7" * 64}
    assert any("provenance of api not attached" in x for x in lines)
    repository, subject, body = next(item for item in attached if item[0] == "shop/web")
    statement = json.loads(body)
    assert statement["_type"] == "https://in-toto.io/Statement/v1"
    assert statement["subject"] == [
        {"name": "shop/web", "digest": {"sha256": "9" * 64}}
    ]
    predicate = statement["predicate"]
    assert predicate["runDetails"]["builder"]["id"].endswith("/host/mini")
    assert {"uri": "oci://ghcr.io/example/builder", "digest": {"sha256": "e" * 64}} in (
        predicate["buildDefinition"]["resolvedDependencies"]
    )


# ------------------------------------------------------------------ registry


def test_provenance_is_pushed_as_a_referrer_and_skipped_when_refused() -> None:
    import hashlib

    from piceli.artifacts.registry import RegistryTarget
    from piceli.artifacts.registry_delivery import RegistryDelivery
    from tests.unit.test_registry_delivery import FakeRegistry

    registry = FakeRegistry()
    registry.referrers_api = True
    try:
        config = b'{"architecture":"amd64","os":"linux"}'
        config_digest = "sha256:" + hashlib.sha256(config).hexdigest()
        registry.blobs[("shop/web", config_digest)] = config
        manifest = json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": {
                    "mediaType": "application/vnd.oci.image.config.v1+json",
                    "digest": config_digest,
                    "size": len(config),
                },
                "layers": [],
            }
        ).encode()
        subject = "sha256:" + hashlib.sha256(manifest).hexdigest()
        registry.manifests[("shop/web", subject)] = (
            manifest,
            "application/vnd.oci.image.manifest.v1+json",
        )
        target = RegistryTarget.parse(f"oci://127.0.0.1:{registry.port}/shop/web")
        body = provenance.statement(
            "shop/web", subject, {"host": "mini", "builder": "host"}
        )
        result = RegistryDelivery().attach_referrer(target, subject, body)
        assert result["state"] == "attached", result
        assert result["artifact_type"] == "application/vnd.in-toto+json"
        stored = json.loads(registry.manifests[("shop/web", result["digest"])][0])
        assert stored["subject"]["digest"] == subject
        assert stored["artifactType"] == "application/vnd.in-toto+json"
        assert registry.blobs[("shop/web", stored["layers"][0]["digest"])] == body

        original = registry._manifest

        def refuse(handler, method, repo, reference, data):  # type: ignore[no-untyped-def]
            if method == "PUT" and b'"subject"' in data:
                return registry._reply(
                    handler, 400, b'{"errors":[{"code":"MANIFEST_INVALID"}]}'
                )
            return original(handler, method, repo, reference, data)

        registry._manifest = refuse  # type: ignore[method-assign]
        other = provenance.statement("shop/web", subject, {"host": "pi01"})
        skipped = RegistryDelivery().attach_referrer(target, subject, other)
        assert skipped == {"state": "skipped", "reason": "manifest-rejected"}
    finally:
        registry.close()


# ------------------------------------------------------------------ caches


def test_a_corrupt_cached_blob_is_discarded_and_written_again(tmp_path: Path) -> None:
    from piceli.artifacts.oci_layers import BlobStore

    discarded: list[tuple[str, str]] = []
    store = BlobStore(tmp_path, on_discard=lambda d, why: discarded.append((d, why)))
    body = b"layer bytes" * 100
    value = store.put(body)
    path = store.path(value)
    path.write_bytes(b"x" + body[1:])  # same size, other content (a bad disk)
    fresh = BlobStore(tmp_path, on_discard=lambda d, why: discarded.append((d, why)))
    assert fresh.put(body) == value
    assert path.read_bytes() == body
    assert discarded and discarded[0][0] == value and "hashes to" in discarded[0][1]
    assert fresh.discarded == [{"digest": value, "reason": discarded[0][1]}]


def test_a_corrupt_base_layer_is_fetched_again(tmp_path: Path) -> None:
    import hashlib

    from piceli.artifacts.oci_layers import BlobStore, Descriptor, _fetch

    body = b"base layer" * 1000
    value = "sha256:" + hashlib.sha256(body).hexdigest()
    fetched: list[str] = []

    class Client:
        @contextmanager
        def open_blob(self, repository: str, digest: str) -> Any:
            fetched.append(digest)
            yield io.BytesIO(body)

    store = BlobStore(tmp_path)
    item = Descriptor("application/vnd.oci.image.layer.v1.tar+gzip", value, len(body))
    _fetch(Client(), "library/base", item, store)
    assert fetched == [value]
    _fetch(Client(), "library/base", item, BlobStore(tmp_path))
    assert fetched == [value]  # a sound blob is reused
    store.path(value).write_bytes(body[:-1] + b"!")
    again = BlobStore(tmp_path)
    _fetch(Client(), "library/base", item, again)
    assert fetched == [value, value]  # the corrupt one was not trusted
    assert again.discarded[0]["digest"] == value
    assert store.path(value).read_bytes() == body


def test_host_build_logs_a_discarded_blob_as_a_health_sign() -> None:
    from piceli.artifacts.host_execution import HostExecution
    from piceli.dev.pod import health_signal

    said: list[str] = []

    class Log:
        def line(self, text: str) -> None:
            said.append(text)

        def say(self, text: str) -> None:
            said.append(text)

    execution = HostExecution.__new__(HostExecution)
    execution.log = Log()  # type: ignore[assignment]
    execution._discarded("sha256:" + "a" * 64, "content hashes to sha256:" + "b" * 64)
    signal = health_signal(said)
    assert signal is not None and signal["kind"] == "cache-corrupt"
