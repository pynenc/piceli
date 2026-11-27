"""0.89.0: derived images in a composition: their change keys and the append.

A derived image's key is its base image's key plus the files of its own
contexts. A release decision (a file only the derived image reads) changes
the derived key alone: the controller builds nothing and appends the file to
the base image it recorded for the base's key, pinned by digest.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from piceli.artifacts.host_build import HostBuildSpec
from piceli.gitops import history
from piceli.infra.builders import BuiltImage
from piceli.infra.pipelines import (
    DeriveItem,
    SpecBuild,
    derived_files,
    image_keys,
)
from tests.unit.infra.test_pipeline_composition import (
    EXAMPLE,
    FakeBuilder,
    _states,
    world,
)

__all__ = ["world"]

COMMIT = "a" * 40

SPEC: dict[str, Any] = {
    "revision": "piceli.host-build.v1",
    "name": "shop",
    "build": {"tools": ["true"], "commands": [["true"]]},
    "context": {
        "app": {"source": "shop", "include": ["site/**"]},
        "release": {"source": "decisions", "include": ["release/**"]},
    },
    "output": {
        "image": [
            {
                "name": "web-base",
                "repository": "shop/web-base",
                "contexts": ["app"],
                "files": {"app/site": "/srv/site"},
            },
            {
                "name": "web",
                "repository": "shop/web",
                "from": "web-base",
                "contexts": ["release"],
                "files": {"release/release/release": "/srv/release"},
            },
        ]
    },
}


class FakeSources:
    """Files per source; every commit sees the current files."""

    def __init__(self, files: dict[str, dict[str, str]]) -> None:
        self.files = files

    def fetch(self, key: str) -> None:
        pass

    def tree(self, key: str, commit: str, prefix: str) -> list[tuple[str, str, str]]:
        found = []
        for path, text in sorted(self.files[key].items()):
            if prefix and not path.startswith(prefix + "/"):
                continue
            inner = path[len(prefix) + 1 :] if prefix else path
            mode = "100755" if text.startswith("#!") else "100644"
            found.append((inner, mode, hashlib.sha1(text.encode()).hexdigest()))
        return found

    def read(self, key: str, commit: str, path: str) -> bytes | None:
        text = self.files[key].get(path)
        return None if text is None else text.encode()


def _build() -> SpecBuild:
    spec = HostBuildSpec.from_dict(SPEC, Path("/"))
    return SpecBuild(
        "shop",
        "host-build.toml",
        spec,
        {"app": ("shop", ""), "release": ("decisions", "")},
    )


def _keys(sources: FakeSources) -> dict[str, str]:
    revision = {"shop": COMMIT, "decisions": COMMIT}
    found = image_keys(_build(), revision, sources, platform="linux/arm64")
    return {name: item.key for name, item in found.items()}


def _sources() -> FakeSources:
    return FakeSources(
        {
            "shop": {"site/index.html": "<h1>shop</h1>\n"},
            "decisions": {"release/release": "1.4.0\n"},
        }
    )


def test_a_release_decision_changes_only_the_derived_key() -> None:
    sources = _sources()
    first = _keys(sources)
    sources.files["decisions"]["release/release"] = "1.4.1\n"
    after = _keys(sources)
    assert after["web-base"] == first["web-base"]
    assert after["web"] != first["web"]


def test_a_change_of_the_base_changes_the_derived_key_too() -> None:
    sources = _sources()
    first = _keys(sources)
    sources.files["shop"]["site/index.html"] = "<h1>new</h1>\n"
    after = _keys(sources)
    assert after["web-base"] != first["web-base"]
    assert after["web"] != first["web"]


def test_a_file_outside_the_derived_images_contexts_changes_no_key() -> None:
    sources = _sources()
    first = _keys(sources)
    sources.files["decisions"]["README.md"] = "notes\n"
    assert _keys(sources) == first


def test_the_derived_key_names_the_sources_of_base_and_files() -> None:
    revision = {"shop": COMMIT, "decisions": "b" * 40}
    found = image_keys(_build(), revision, _sources(), platform="linux/arm64")
    assert dict(found["web"].sources) == revision
    assert dict(found["web-base"].sources) == {"shop": COMMIT}


def test_the_derived_files_are_read_from_git() -> None:
    build = _build()
    (derived,) = build.spec.derived
    revision = {"shop": COMMIT, "decisions": COMMIT}
    (found,) = derived_files(build, derived, revision, _sources())
    assert found.path == "/srv/release" and found.data == b"1.4.0\n"
    assert not found.executable


# ------------------------------------------------------------- the controller

#: The example's spec plus a release decision only ``api-release`` reads.
DERIVED = """
[context.release]
source = "infra"
include = ["release/**"]

[[output.image]]
name = "api-release"
repository = "example/api-release"
from = "api"
contexts = ["release"]
files = { "release/release/release.json" = "/srv/site/release.json" }
"""


class DerivingBuilder(FakeBuilder):
    def __init__(self) -> None:
        super().__init__()
        self.derived: list[DeriveItem] = []

    def derive(self, items: Sequence[DeriveItem]) -> Mapping[str, BuiltImage]:
        self.derived += list(items)
        done = {}
        for item in items:
            digest = (
                "sha256:"
                + hashlib.sha256(
                    (item.base_digest + item.files[0].sha256).encode()
                ).hexdigest()
            )
            base = {"image": item.base, "digest": item.base_digest}
            done[item.component] = BuiltImage(
                f"registry.example:5000/{item.repository}@{digest}",
                digest,
                {
                    "derived_from": base,
                    "provenance": {
                        "builder": "derive",
                        "host": "controller",
                        "derived_from": base,
                        "files": {f.path: f.sha256 for f in item.files},
                    },
                },
            )
        return done


def _with_release(world: dict[str, Any], release: str) -> None:
    spec = (EXAMPLE / "infra" / "host-build.toml").read_text() + DERIVED
    world["infra"].commit(
        {"host-build.toml": spec, "release/release.json": release}, message="release"
    )


def test_a_decision_only_change_appends_to_the_recorded_base_without_a_build(
    world: dict[str, Any],
) -> None:
    controller, ports = world["controller"], world["ports"]
    ports.builder = DerivingBuilder()
    _with_release(world, '{"release": "1.4.0"}\n')
    status = controller.poll_once()
    main = status["envs"]["main"]
    assert main["state"] == "deployed", main
    assert ports.builder.built == [["api", "worker"]]  # never the derived one
    api = main["components"]["api"]
    (first,) = ports.builder.derived
    assert first.base == "api" and first.base_digest == api["digest"]
    assert first.base_repository == "example/api"
    assert main["components"]["api-release"]["derived_from"] == {
        "image": "api",
        "digest": api["digest"],
    }
    # A release decision: only the derived key changed. No build item, an
    # append from the cached base digest.
    world["infra"].commit({"release/release.json": '{"release": "1.4.1"}\n'})
    status = controller.poll_once()
    main = status["envs"]["main"]
    assert main["state"] == "deployed", main
    assert len(ports.builder.built) == 1 and len(ports.builder.requests) == 1
    second = ports.builder.derived[-1]
    assert len(ports.builder.derived) == 2
    assert second.base_digest == api["digest"] and second.key != first.key
    assert second.files[0].data == b'{"release": "1.4.1"}\n'
    assert _states(status, "main") == {
        "api": "unchanged",
        "worker": "unchanged",
        "api-release": "synced",
    }
    assert main["components"]["api"]["digest"] == api["digest"]
    # The run history shows the base the derived image pins.
    entry = history.event(
        main,
        started_at="2026-11-27T10:00:00Z",
        finished_at="2026-11-27T10:00:01Z",
        trigger="push infra/main",
        approval=None,
        outcome=None,
        built=["api-release"],
    )
    shown = entry["components"]["api-release"]
    assert shown["derived_from"] == {"image": "api", "digest": api["digest"]}
    assert shown["provenance"]["derived_from"]["digest"] == api["digest"]
    # Nothing moved: nothing appended again.
    controller.poll_once()
    assert len(ports.builder.derived) == 2


def test_a_lost_base_record_fails_instead_of_building_the_base(
    world: dict[str, Any],
) -> None:
    import shutil

    controller, ports = world["controller"], world["ports"]
    ports.builder = DerivingBuilder()
    _with_release(world, '{"release": "1.4.0"}\n')
    controller.poll_once()
    shutil.rmtree(world["state"] / "components" / "api")
    world["infra"].commit({"release/release.json": '{"release": "1.4.1"}\n'})
    status = controller.poll_once()
    main = status["envs"]["main"]
    assert main.get("reason") == "derived-base-unrecorded", main
    assert ports.builder.built == [["api", "worker"]]  # the base was not rebuilt
    assert len(ports.builder.derived) == 1


def _plan(pipeline: Any, name: str, **_kw: Any) -> dict[str, Any]:
    return {"state": "planned", "plan_hash": "sha256:" + "e" * 64, "deploy": {}}


def test_the_canary_appends_derived_images_dry_instead_of_refusing_them(
    world: dict[str, Any],
) -> None:
    """0.93.0 (ops#114): the canary's builder had no ``derive``, so every
    composition with derived images failed the canary at resolve."""
    from piceli.gitops.canary import DryPorts, dry_run, summary_lines

    controller, ports = world["controller"], world["ports"]
    _with_release(world, '{"release": "1.4.0"}\n')
    body = dry_run(controller, DryPorts(ports, _plan))
    main = body["envs"]["main"]
    assert main["state"] == "ok", main
    assert sorted(main["builds"]) == ["api", "worker"]
    assert main["derives"] == ["api-release"]
    assert ports.builder.built == []  # nothing built by the real ports
    assert (
        "main: imports, plans 0 changes, builds 2 components, appends 1 derived image: ok"
        in summary_lines(body)
    )


def test_the_canary_reuses_the_live_controllers_derived_images(
    world: dict[str, Any],
) -> None:
    from piceli.gitops.canary import DryPorts, dry_run, known_images

    controller, ports = world["controller"], world["ports"]
    ports.builder = DerivingBuilder()
    _with_release(world, '{"release": "1.4.0"}\n')
    live = controller.poll_once()
    assert live["envs"]["main"]["state"] == "deployed"
    controller.state["envs"] = {}
    body = dry_run(controller, DryPorts(ports, _plan, known=known_images(live)))
    main = body["envs"]["main"]
    assert main["state"] == "ok", main
    assert main["builds"] == [] and main["derives"] == []
    assert len(ports.builder.derived) == 1  # only the live controller appended
