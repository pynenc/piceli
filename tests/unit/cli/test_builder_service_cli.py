"""0.28.0: ``piceli builder install|uninstall|status`` from the command line.

The machine (system, user, home, tools, ``systemctl``/``launchctl``) is a
fake; the controller's status is a fixed document. Nothing reaches a
cluster or a service manager.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.infra import builder_service as bs
from piceli.k8s.cli import app
from piceli.k8s.cli import builder as builder_cli

PICELI = Path("/opt/piceli/bin/piceli")
IMAGE = "ghcr.io/example/builder@sha256:" + "0" * 64
PUBLIC = "example-cache-1:jXbGkEMPpvXtlVqGBrqfsnU8Jyn1cZB9u0QhGhvsKP0="

MODULE = f"""
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
        image="{IMAGE}",
        hosts=[BuildHost.docker("builder-1"), BuildHost.docker("builder-2")],
    ),
)
"""


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 0, "", "")


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy" / "infra.py").write_text(MODULE)
    monkeypatch.chdir(tmp_path / "deploy")
    monkeypatch.setenv("PICELI_CREDENTIALS_DIR", str(tmp_path / "credentials"))
    runner = FakeRunner()
    tools: dict[str, str | None] = {
        name: f"/usr/bin/{name}" for name in ("docker", "git", "kubectl")
    }
    machine = bs.Machine(
        system="linux",
        euid=1000,
        home=tmp_path / "home",
        root=tmp_path / "root",
        environ={"PATH": "/usr/bin", "PICELI_CREDENTIALS_DIR": "/srv/credentials"},
        which=lambda name, path=None: tools.get(name),
        run=runner,
        home_of=lambda user: Path(f"/home/{user}"),
        piceli=PICELI,
    )
    monkeypatch.setattr(bs.Machine, "current", classmethod(lambda cls: machine))
    status: dict[str, Any] = {"controller": {"builds_wanted": []}}
    reads: list[str] = []

    def read_status(declared: Any, namespace: str, *args: Any) -> dict[str, Any]:
        reads.append(namespace)
        return status

    monkeypatch.setattr(builder_cli, "_read_status", read_status)
    return {
        "runner": runner,
        "tools": tools,
        "status": status,
        "reads": reads,
        "unit": tmp_path / "home/.config/systemd/user/piceli-builder-builder-1.service",
    }


def _invoke(*argv: str) -> Any:
    return CliRunner().invoke(app, ["builder", *argv])


INSTALL = ("install", "--cluster", "infra:cluster", "--host", "builder-1")


def test_print_writes_the_unit_to_stdout_and_changes_nothing(
    world: dict[str, Any],
) -> None:
    result = _invoke(*INSTALL, "--print")
    assert result.exit_code == 0, result.output
    assert "[Service]" in result.stdout
    assert (
        f"ExecStart={PICELI} builder serve --cluster infra:cluster --host builder-1"
        in result.stdout
    )
    assert f"WorkingDirectory={Path.cwd()}" in result.stdout
    assert world["runner"].calls == []
    assert not world["unit"].exists()
    assert world["reads"] == ["piceli-system"]  # the checks still ran


def test_install_checks_writes_and_enables_the_unit(world: dict[str, Any]) -> None:
    result = _invoke(*INSTALL, "--image", IMAGE, "--namespace", "builds")
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["schema"] == "piceli.builder-install.v1"
    assert body["state"] == "installed" and body["host"] == "builder-1"
    assert (body["manager"], body["scope"]) == ("systemd", "user")
    assert body["path"] == str(world["unit"])
    text = world["unit"].read_text()
    assert f"--image {IMAGE} --namespace builds" in text
    assert [call[:3] for call in world["runner"].calls] == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "piceli-builder-builder-1.service"][:3],
        ["systemctl", "--user", "restart"],
    ]
    assert "loginctl enable-linger" in result.stderr
    assert world["reads"] == ["builds"]


def test_a_missing_tool_refuses_before_anything_is_written(
    world: dict[str, Any],
) -> None:
    world["tools"]["kubectl"] = None
    result = _invoke(*INSTALL)
    assert result.exit_code == 2
    body = json.loads(result.stdout)
    assert body["reason"] == "builder-service-invalid"
    assert "kubectl" in body["message"]
    assert world["runner"].calls == [] and not world["unit"].exists()


def test_a_missing_credential_the_host_needs_refuses_by_name(
    world: dict[str, Any],
) -> None:
    world["status"]["nix_releases"] = {
        "web-1": {
            "builder": "builder-1",
            "plan": {
                "credentials": "host",
                "inputs": [
                    {
                        "input": "lib",
                        "url": "https://git.example.org/lib.git",
                        "credential": "lib-token",
                    }
                ],
            },
        }
    }
    result = _invoke(*INSTALL)
    assert result.exit_code == 2, result.output
    body = json.loads(result.stdout)
    assert body["reason"] == "infra-credential-missing"
    assert "lib-token" in body["message"]
    assert "piceli secrets git-token lib-token --host builder-1" in body["message"]
    assert world["runner"].calls == [] and not world["unit"].exists()


def test_a_missing_cache_key_refuses_by_name(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from piceli.dev.nix import NixCache
    from piceli.k8s.cli import dev_nix

    monkeypatch.setattr(
        dev_nix, "load_cache", lambda _ref: NixCache(PUBLIC, url="file:///srv/cache")
    )
    result = _invoke(*INSTALL, "--publish", "infra:cache")
    assert result.exit_code == 2, result.output
    body = json.loads(result.stdout)
    assert body["reason"] == "infra-credential-missing"
    assert "example-cache-1" in body["message"]
    assert world["reads"] == [] and not world["unit"].exists()


def test_an_undeclared_host_is_refused(world: dict[str, Any]) -> None:
    result = _invoke("install", "--cluster", "infra:cluster", "--host", "builder-9")
    assert result.exit_code == 2
    assert json.loads(result.stdout)["reason"] == "dev-host-unknown"


def test_uninstall_stops_and_removes_the_service(world: dict[str, Any]) -> None:
    assert _invoke(*INSTALL).exit_code == 0
    world["runner"].calls.clear()
    result = _invoke("uninstall", "--host", "builder-1")
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["state"] == "removed" and body["path"] == str(world["unit"])
    assert not world["unit"].exists()
    assert world["runner"].calls[0][:4] == ["systemctl", "--user", "disable", "--now"]
    again = json.loads(_invoke("uninstall", "--host", "builder-1").stdout)
    assert again["state"] == "absent"


def test_status_lists_the_hosts_from_the_controllers_status(
    world: dict[str, Any],
) -> None:
    world["status"]["controller"] = {
        "build_hosts": {
            "builder-1": {
                "version": "0.28.0",
                "seen_at": "2027-01-15T07:58:00Z",
                "seen": "build-claim",
                "last_build": {"id": "a" * 20, "outcome": "claimed"},
            }
        },
        "builds_wanted": [
            {
                "id": "a" * 20,
                "kind": "components",
                "claimed": {"host": "builder-1", "at": "2027-01-15T07:58:00Z"},
            }
        ],
    }
    result = _invoke("status", "--cluster", "infra:cluster", "--json")
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    rows = {row["host"]: row for row in body["hosts"]}
    assert rows["builder-1"]["claim"]["state"] == "live"
    assert rows["builder-2"]["state"] == "never-seen"
    assert body["never_seen"] == ["builder-2"]
    assert "builder-1" in result.stderr and "0.28.0" in result.stderr
    assert "never seen" in result.stderr


def test_as_root_the_checks_read_the_service_users_secrets(
    world: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dataclasses

    seen: list[str | None] = []
    machine = dataclasses.replace(
        bs.Machine.current(), euid=0, home_of=lambda user: tmp_path / user
    )
    monkeypatch.setattr(bs.Machine, "current", classmethod(lambda cls: machine))

    def read_status(declared: Any, namespace: str, *args: Any) -> dict[str, Any]:
        import os

        seen.append(os.environ.get("HOME"))
        return {}

    monkeypatch.setattr(builder_cli, "_read_status", read_status)
    result = _invoke(*INSTALL, "--user", "builder", "--print")
    assert result.exit_code == 0, result.output
    assert seen == [str(tmp_path / "builder")]
    assert "User=builder" in result.stdout
