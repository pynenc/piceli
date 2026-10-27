"""0.33.0: a build host claims only on the controller's Piceli version.

On a mismatch ``builder serve`` claims nothing (the controller falls back to
its in-cluster build), tells the controller its version, writes the
controller's version into ``--version-file`` and exits 75; ``builder
status``, ``gitops hosts`` and the ``build-host-version`` alarm show it.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli import __version__
from piceli.gitops.alarms import changes, observe, version_mismatches
from piceli.gitops.state import DirectoryChannel
from piceli.infra import build_host_status
from piceli.infra.builder_serve import (
    EXIT_VERSION_MISMATCH,
    BuilderServe,
    VersionMismatch,
    write_version_file,
)
from tests.unit.infra.test_remote_builds import _world

OLD = "0.0.1"


def _worker(world: dict[str, Any], tmp_path: Path, **extra: Any) -> BuilderServe:
    controller = world["controller"]
    return BuilderServe(
        host="builder-1",
        image="ghcr.io/x/builder@sha256:" + "e" * 64,
        channel=world["channel"],
        container=lambda *_a: 0,
        push=lambda *_a: "",
        index=lambda *_a: "",
        root=tmp_path / "builder",
        sleep=lambda _s: controller.poll_once(),
        **extra,
    )


def test_a_host_on_another_version_claims_nothing_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _world(tmp_path, monkeypatch)
    controller, channel = world["controller"], world["channel"]
    status = controller.poll_once()
    assert status["controller"]["version"] == __version__
    (wanted,) = status["controller"]["builds_wanted"]
    lines: list[str] = []
    version_file = tmp_path / "state-dir" / "version"
    worker = _worker(
        world, tmp_path, version=OLD, version_file=version_file, say=lines.append
    )
    with pytest.raises(VersionMismatch) as caught:
        worker.once()
    assert (caught.value.version, caught.value.controller) == (OLD, __version__)
    assert str(caught.value) == (
        f"build host builder-1 runs piceli {OLD}, the controller {__version__}"
    )
    assert version_file.read_text() == f"{__version__}\n"
    assert lines == [f"builder: wrote {__version__} into {version_file}"]
    # Nothing was claimed: only the host's version went to the controller.
    assert [key.split(".")[0] for key in channel.requests()] == ["build-host-version"]

    status = controller.poll_once()
    seen = status["controller"]["build_hosts"]["builder-1"]
    assert (seen["version"], seen["seen"]) == (OLD, "build-host-version")
    assert seen.get("last_build") is None
    assert not (status["controller"]["builds_wanted"][0].get("claimed"))
    assert status["controller"]["builds_wanted"][0]["id"] == wanted["id"]

    # Seen: a restarted host on the same version says it once, not again.
    again = _worker(world, tmp_path, version=OLD, version_file=version_file)
    with pytest.raises(VersionMismatch):
        again.once()
    assert channel.requests() == {}
    assert not any(path.name.endswith(".tmp") for path in version_file.parent.iterdir())

    # Visible: builder status, gitops hosts and the alarm.
    view = build_host_status.view(status, ["builder-1"])
    (row,) = view["hosts"]
    assert view["controller_version"] == __version__
    assert row["state"] == "version-mismatch" and row["version_mismatch"] is True
    assert version_mismatches(status["controller"]) == {"builder-1": OLD}
    alarm = observe(status, 0.0, 600.0)["build-host-version:builder-1"]
    assert alarm.reason == "builder-version-mismatch"
    assert f"runs piceli {OLD}, the controller {__version__}" in alarm.text
    assert f"install piceli {__version__} on builder-1" in alarm.next

    # Upgraded: it tells the controller its version, claims and the alarm resolves.
    memory: dict[str, Any] = {}
    assert [a.event for a in changes(memory, status, 0.0, 600.0)] == [
        "build-host-version"
    ]
    upgraded = _worker(world, tmp_path, version_file=version_file)
    entry = upgraded.pick()
    assert entry is not None and entry["id"] == wanted["id"]
    assert [key.split(".")[0] for key in channel.requests()] == ["build-host-version"]
    status = controller.poll_once()
    assert status["controller"]["build_hosts"]["builder-1"]["version"] == __version__
    assert version_mismatches(status["controller"]) == {}
    (resolved,) = changes(memory, status, 0.0, 600.0)
    assert resolved.state == "resolved" and resolved.event == "build-host-version"
    assert "runs the controller's Piceli version" in resolved.text
    assert upgraded.claim(entry) is True


def test_a_controller_without_a_version_is_not_checked(tmp_path: Path) -> None:
    channel = DirectoryChannel(tmp_path / "state")
    channel.publish({"controller": {"builds_wanted": []}})
    worker = BuilderServe(
        host="builder-1",
        image="",
        channel=channel,
        container=lambda *_a: 0,
        push=None,
        index=None,
        root=tmp_path / "builder",
        version=OLD,
    )
    assert worker.pick() is None
    channel.publish({"controller": {"version": "unknown", "builds_wanted": []}})
    assert worker.pick() is None


def test_only_declared_hosts_count_as_mismatched() -> None:
    controller = {
        "version": "2.0.0",
        "build_hosts_declared": ["builder-1"],
        "build_hosts": {
            "builder-1": {"version": "1.0.0"},
            "old-host": {"version": "1.0.0"},
        },
    }
    assert version_mismatches(controller) == {"builder-1": "1.0.0"}
    assert version_mismatches({**controller, "build_hosts_declared": []}) == {
        "builder-1": "1.0.0",
        "old-host": "1.0.0",
    }


def test_the_version_file_is_written_only_when_it_changes(tmp_path: Path) -> None:
    path = tmp_path / "version"
    assert write_version_file(path, "1.2.3") is True
    assert write_version_file(path, "1.2.3") is False
    path.write_text("1.2.2\n")
    assert write_version_file(path, "1.2.3") is True
    assert path.read_text() == "1.2.3\n"


MODULE = """
from piceli import Registry
from piceli.infra import BuildHost, Cluster, DevBuilds, Node

cluster = Cluster(
    "home",
    api="https://k8s.example.org:6443",
    credentials="home-admin",
    nodes=[Node("node-a", arch="arm64", roles=["registry"])],
    registry=Registry.in_cluster(on="node-a"),
    dev=DevBuilds(
        node=None,
        image="ghcr.io/example/builder@sha256:" + "0" * 64,
        hosts=[BuildHost.docker("builder-1")],
    ),
)
"""


@pytest.fixture
def serve_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """``builder serve`` from the command line against a directory channel:
    no cluster, no container engine."""
    import piceli.dev.health as health
    import piceli.dev.hosts as hosts
    import piceli.gitops.state as state
    import piceli.infra.builder_serve as builder_serve
    import piceli.profiles as profiles
    from piceli.k8s.cli import builder as builder_cli

    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy" / "infra.py").write_text(MODULE)
    monkeypatch.chdir(tmp_path / "deploy")
    channel = DirectoryChannel(tmp_path / "state")

    @contextmanager
    def connect(*_args: Any) -> Any:
        yield SimpleNamespace(client=None)

    class Cli:
        def __init__(self, *_args: Any) -> None:
            pass

        def platform(self) -> str:
            return "linux/amd64"

    monkeypatch.setattr(builder_cli, "connect", connect)
    monkeypatch.setattr(state, "ConfigMapChannel", lambda _client, _ns: channel)
    monkeypatch.setattr(hosts, "ContainerCli", Cli)
    monkeypatch.setattr(builder_serve, "ensure_image", lambda *_a: None)
    monkeypatch.setattr(builder_serve, "prepare_cache", lambda *_a: None)
    monkeypatch.setattr(health.Health, "of", classmethod(lambda cls, *_a: None))
    monkeypatch.setattr(
        profiles,
        "resolve",
        lambda _name: SimpleNamespace(kubeconfig=None, context=None),
    )
    return {"channel": channel, "tmp": tmp_path}


def test_builder_serve_exits_75_and_writes_the_version_file(
    serve_world: dict[str, Any],
) -> None:
    from piceli.k8s.cli import app

    channel, tmp = serve_world["channel"], serve_world["tmp"]
    channel.publish({"controller": {"version": "99.0.0", "builds_wanted": []}})
    version_file = tmp / "version"
    result = CliRunner().invoke(
        app,
        [
            "builder", "serve", "--cluster", "infra:cluster", "--host", "builder-1",
            "--version-file", str(version_file),
        ],
    )  # fmt: skip
    assert result.exit_code == EXIT_VERSION_MISMATCH == 75, result.output
    found = json.loads(result.stdout.strip().splitlines()[-1])
    assert found["state"] == "rejected"
    assert found["reason"] == "builder-version-mismatch"
    assert found["controller_version"] == "99.0.0"
    assert found["version"] == __version__
    assert found["version_file"] == str(version_file)
    assert version_file.read_text() == "99.0.0\n"
    assert [key.split(".")[0] for key in channel.requests()] == ["build-host-version"]


def test_builder_serve_check_reports_both_versions(
    serve_world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from piceli.k8s.cli import app
    from piceli.k8s.cli import builder as builder_cli

    monkeypatch.setattr(builder_cli, "_check_sources", lambda _status: [])
    monkeypatch.setattr(
        builder_cli, "_check_scratch", lambda *_a: {"state": "writable"}
    )
    channel = serve_world["channel"]
    argv = ["builder", "serve", "--cluster", "infra:cluster", "--check"]
    channel.publish({"controller": {"version": "99.0.0", "builds_wanted": []}})
    result = CliRunner().invoke(app, argv)
    assert result.exit_code == 1, result.output
    found = json.loads(result.stdout.strip().splitlines()[-1])
    assert found["version"] == {
        "host": __version__,
        "controller": "99.0.0",
        "matches": False,
        "image": "ghcr.io/example/builder@sha256:" + "0" * 64,
        "controller_image": None,
    }
    assert "VERSION MISMATCH" in result.stderr
    channel.publish({"controller": {"version": __version__}})
    result = CliRunner().invoke(app, argv)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout.strip().splitlines()[-1])["version"]["matches"]


def _image_worker(tmp_path: Path, channel: Any, **extra: Any) -> BuilderServe:
    return BuilderServe(
        host="builder-1",
        image="ghcr.io/x/builder@sha256:" + "1" * 64,
        channel=channel,
        container=lambda *_a: 0,
        push=None,
        index=None,
        root=tmp_path / "builder",
        **extra,
    )


NEW_IMAGE = "ghcr.io/x/builder@sha256:" + "2" * 64


def test_the_host_builds_in_the_controllers_builder_image(tmp_path: Path) -> None:
    """0.33.0: a newer controller image is pulled and used before a claim."""
    channel = DirectoryChannel(tmp_path / "state")
    channel.publish(
        {"controller": {"version": __version__, "builder_image": NEW_IMAGE}}
    )
    pulled: list[str] = []
    runner = lambda *_a: 0  # noqa: E731

    def switch(image: str) -> Any:
        pulled.append(image)
        return runner

    lines: list[str] = []
    worker = _image_worker(tmp_path, channel, switch_image=switch, say=lines.append)
    assert worker.pick() is None
    assert (worker.image, worker.container, pulled) == (NEW_IMAGE, runner, [NEW_IMAGE])
    assert lines[-1].endswith(f"-> {NEW_IMAGE} (the controller's)")
    worker.pick()
    assert pulled == [NEW_IMAGE]  # pulled once


def test_an_unusable_controller_image_claims_nothing(tmp_path: Path) -> None:
    channel = DirectoryChannel(tmp_path / "state")
    channel.publish(
        {"controller": {"version": __version__, "builder_image": NEW_IMAGE}}
    )

    def switch(_image: str) -> Any:
        raise RuntimeError("pull failed")

    lines: list[str] = []
    worker = _image_worker(tmp_path, channel, switch_image=switch, say=lines.append)
    worker.wanted()
    assert worker.follow_image() is False
    assert worker.image.endswith("1" * 64)
    assert "cannot use the controller's builder image" in lines[-1]


def test_a_pinned_image_is_kept_and_said_once(tmp_path: Path) -> None:
    channel = DirectoryChannel(tmp_path / "state")
    channel.publish(
        {"controller": {"version": __version__, "builder_image": NEW_IMAGE}}
    )
    lines: list[str] = []
    worker = _image_worker(
        tmp_path,
        channel,
        image_pinned=True,
        switch_image=lambda _i: pytest.fail("a pinned image is never switched"),
        say=lines.append,
    )
    worker.pick()
    worker.pick()
    assert worker.image.endswith("1" * 64)
    assert len([line for line in lines if "differs from the controller's" in line]) == 1


def test_the_controller_publishes_its_builder_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dataclasses

    world = _world(tmp_path, monkeypatch)
    controller = world["controller"]
    assert "builder_image" not in controller.poll_once()["controller"]
    controller.config = dataclasses.replace(controller.config, builder_image=NEW_IMAGE)
    assert controller.poll_once()["controller"]["builder_image"] == NEW_IMAGE


def test_builder_serve_uses_the_controllers_image_unless_overridden(
    serve_world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import piceli.infra.builder_serve as builder_serve
    from piceli.k8s.cli import app

    pulled: list[str] = []
    monkeypatch.setattr(
        builder_serve, "ensure_image", lambda _c, i, _s: pulled.append(i)
    )
    channel = serve_world["channel"]
    channel.publish(
        {
            "controller": {
                "version": __version__,
                "builder_image": NEW_IMAGE,
                "builds_wanted": [],
            }
        }
    )
    base = ["builder", "serve", "--cluster", "infra:cluster", "--once"]
    result = CliRunner().invoke(app, base)
    assert result.exit_code == 0, result.output
    assert pulled == [NEW_IMAGE]
    pinned = "ghcr.io/x/builder@sha256:" + "3" * 64
    result = CliRunner().invoke(app, [*base, "--image", pinned])
    assert result.exit_code == 0, result.output
    assert pulled[-1] == pinned
    assert "differs from the controller's builder image" in result.stderr
