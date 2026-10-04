"""The OS hand-off: the install hook, then k3s registered as the server's cluster.

No OpenTofu here: the state directory's inventory is written as an apply
leaves it. The cluster is the in-process fake Kubernetes API; SSH is a pair
of recording scripts on ``PATH``.
"""

from __future__ import annotations

import base64
import json
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner, Result

from piceli import profiles
from piceli.infra import Cluster
from piceli.infra.machines.plan import INVENTORY
from piceli.infra.machines.register import fingerprint, register_cluster
from piceli.infra.machines.state import read_record, write_record
from piceli.k8s.cli import app as cli
from piceli.testing import fake_cluster

from .machines_support import TOKEN, last_json, write_module

runner = CliRunner()
HOST_KEY = base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519" + b"k" * 36).decode()
OTHER_KEY = base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519" + b"o" * 36).decode()
CLIENT_SECRET = "client-token-never-printed"


def piceli(*args: str, input: str | None = None) -> tuple[Result, dict[str, Any]]:
    result = runner.invoke(cli, list(args), input=input)
    return result, last_json(result.stdout)


def _applied(homes: Path, **servers: dict[str, str]) -> Path:
    directory = homes / "state"
    directory.mkdir(mode=0o700, exist_ok=True)
    write_record(
        directory,
        INVENTORY,
        {
            "servers": servers
            or {"edge-1": {"id": "srv-1", "ipv4": "127.0.0.1", "ipv6": "2001:db8::10"}}
        },
    )
    return directory


@pytest.fixture(autouse=True)
def _profiles() -> Iterator[None]:
    profiles.set_override(None)
    yield
    profiles.set_override(None)


# ------------------------------------------------------------ install


def test_install_previews_then_runs_the_approved_command(
    homes: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = homes / "hook-env"
    ref = f"{write_module(homes, hook=['sh', '-c', f'echo installing {{name}} at {{ipv4}}; env > {seen}'])}:infra"
    _applied(homes)
    monkeypatch.setenv(
        "HCLOUD_TOKEN", TOKEN
    )  # the owner's shell may hold one; the hook never gets it
    preview, body = piceli("infra", "install", ref, "edge-1")
    assert preview.exit_code == 3, preview.output
    assert body["argv"][-1].startswith("echo installing edge-1 at 127.0.0.1;")
    assert not seen.exists()
    wrong, refused = piceli(
        "infra", "install", ref, "edge-1", "--approve", "sha256:" + "1" * 64
    )
    assert wrong.exit_code == 2 and refused["reason"] == "infra-install-changed"
    assert not seen.exists()
    done, outcome = piceli(
        "infra", "install", ref, "edge-1", "--approve", body["digest"]
    )
    assert done.exit_code == 0, done.output
    assert outcome["state"] == "installed" and outcome["outcome"]["exit_code"] == 0
    # The hook's output goes to stderr; stdout stays one JSON object.
    assert len(done.stdout.strip().splitlines()) == 1
    assert "installing edge-1 at 127.0.0.1\n" in done.stderr
    assert TOKEN not in seen.read_text()
    assert (
        read_record(homes / "state", "installs.json")["edge-1"]["state"] == "installed"
    )
    _, status = piceli("infra", "status", ref, "--json")
    assert status["servers"][0]["install"]["state"] == "installed"


def test_a_failing_hook_exits_1(homes: Path) -> None:
    ref = f"{write_module(homes, hook=['sh', '-c', 'exit 7'])}:infra"
    _applied(homes)
    _, body = piceli("infra", "install", ref, "edge-1")
    result, failed = piceli(
        "infra", "install", ref, "edge-1", "--approve", body["digest"]
    )
    assert result.exit_code == 1
    assert (
        failed["reason"] == "infra-install-failed"
        and failed["outcome"]["exit_code"] == 7
    )


def test_install_needs_an_applied_server_and_a_hook(homes: Path) -> None:
    ref = f"{write_module(homes)}:infra"
    result, body = piceli("infra", "install", ref, "edge-1")
    assert result.exit_code == 2 and body["reason"] == "infra-not-applied"
    _applied(homes)
    result, body = piceli("infra", "install", ref, "edge-9")
    assert body["reason"] == "infra-server-unknown"


# ------------------------------------------------------------ register


def _ready(cluster: Any) -> None:
    cluster.api.add_node("edge-1")
    cluster.api.nodes["edge-1"]["status"]["conditions"] = [
        {"type": "Ready", "status": "True"}
    ]


def _owner_kubeconfig(path: Path, url: str) -> Path:
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "v1",
                "kind": "Config",
                "clusters": [
                    {"name": "default", "cluster": {"server": "https://127.0.0.1:6443"}}
                ],
                "users": [{"name": "default", "user": {"token": CLIENT_SECRET}}],
                "contexts": [
                    {
                        "name": "default",
                        "context": {"cluster": "default", "user": "default"},
                    }
                ],
                "current-context": "default",
            }
        )
    )
    return path


def test_register_with_an_owner_kubeconfig(homes: Path) -> None:
    with fake_cluster() as cluster:
        _ready(cluster)
        ref = f"{write_module(homes, api=cluster.url)}:infra"
        _applied(homes)
        given = _owner_kubeconfig(homes / "k3s.yaml", cluster.url)
        args = [
            "infra",
            "register",
            ref,
            "edge-1",
            "--kubeconfig",
            str(given),
            "--transport",
            "loopback-http",
            "--wait",
            "20",
        ]
        preview, body = piceli(*args)
        assert preview.exit_code == 3, preview.output
        assert (
            body["source"] == "file"
            and body["profile"] == "edge-1"
            and not body["profile_exists"]
        )
        with pytest.raises(profiles.ProfileError):
            profiles.load_profile("edge-1")
        done, registered = piceli(*args, "--approve", body["digest"])
        assert done.exit_code == 0, done.output
    assert registered["state"] == "registered"
    assert registered["outcome"]["nodes"] == {"nodes": 1, "ready": 1}
    stored = profiles.load_profile("edge-1")
    assert stored.context == "edge-1"
    assert stored.kubeconfig == homes / "credentials" / "kubeconfigs" / "edge-1.yaml"
    assert stat.S_IMODE(stored.kubeconfig.stat().st_mode) == 0o600
    document = yaml.safe_load(stored.kubeconfig.read_text())
    assert document["clusters"][0]["cluster"]["server"] == cluster.url  # Cluster(api=)
    assert "current-context" not in document
    for result in (preview, done):
        assert CLIENT_SECRET not in result.output
    _, status = piceli("infra", "status", ref, "--json")
    assert status["servers"][0]["cluster"]["registered"] is True


def test_register_waits_and_gives_up_without_a_ready_node(homes: Path) -> None:
    with fake_cluster() as cluster:
        cluster.api.add_node("edge-1")  # no Ready condition
        ref = f"{write_module(homes, api=cluster.url)}:infra"
        _applied(homes)
        given = _owner_kubeconfig(homes / "k3s.yaml", cluster.url)
        args = [
            "infra",
            "register",
            ref,
            "edge-1",
            "--kubeconfig",
            str(given),
            "--transport",
            "loopback-http",
            "--wait",
            "0",
        ]
        _, body = piceli(*args)
        result, failed = piceli(*args, "--approve", body["digest"])
    assert result.exit_code == 1 and failed["reason"] == "infra-k3s-not-ready"
    with pytest.raises(profiles.ProfileError):
        profiles.load_profile("edge-1")


def _fake_ssh(homes: Path, kubeconfig: Path, key: str) -> Path:
    bin_dir = homes / "bin"
    bin_dir.mkdir(exist_ok=True)
    scan = bin_dir / "ssh-keyscan"
    scan.write_text(
        f'#!/bin/sh\necho "# comment"\necho "127.0.0.1 ssh-ed25519 {key}"\n'
    )
    ssh = bin_dir / "ssh"
    ssh.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$@" > {homes}/ssh.argv\ncat {kubeconfig}\n'
    )
    for path in (scan, ssh):
        path.chmod(0o755)
    return bin_dir


def test_register_over_ssh_pins_the_scanned_host_key(
    homes: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with fake_cluster() as cluster:
        _ready(cluster)
        ref = f"{write_module(homes, api=cluster.url)}:infra"
        directory = _applied(homes)
        remote = _owner_kubeconfig(homes / "remote-k3s.yaml", cluster.url)
        bin_dir = _fake_ssh(homes, remote, HOST_KEY)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
        args = [
            "infra",
            "register",
            ref,
            "edge-1",
            "--transport",
            "loopback-http",
            "--wait",
            "20",
        ]
        preview, body = piceli(*args)
        assert preview.exit_code == 3, preview.output
        assert body["source"] == "ssh" and body["address"] == "127.0.0.1"
        assert body["host_keys"] == [
            {"type": "ssh-ed25519", "fingerprint": fingerprint(HOST_KEY)}
        ]
        assert fingerprint(HOST_KEY) in preview.stderr

        # Another key answers before the approval: refused, nothing fetched.
        _fake_ssh(homes, remote, OTHER_KEY)
        changed, refused = piceli(*args, "--approve", body["digest"])
        assert changed.exit_code == 2 and refused["reason"] == "infra-register-changed"
        assert not (homes / "ssh.argv").exists()

        _fake_ssh(homes, remote, HOST_KEY)
        done, registered = piceli(*args, "--approve", body["digest"])
        assert done.exit_code == 0, done.output
    argv = (homes / "ssh.argv").read_text().splitlines()
    assert "StrictHostKeyChecking=yes" in argv
    assert f"UserKnownHostsFile={directory / 'known_hosts'}" in argv
    assert argv[-3:] == ["cat", "--", "/etc/rancher/k3s/k3s.yaml"]
    assert (
        directory / "known_hosts"
    ).read_text() == f"127.0.0.1 ssh-ed25519 {HOST_KEY}\n"
    assert registered["outcome"]["source"] == "ssh"
    assert CLIENT_SECRET not in done.output
    record = json.loads((directory / "registrations.json").read_text())
    assert CLIENT_SECRET not in json.dumps(record)


def test_register_needs_a_declared_cluster(homes: Path) -> None:
    path = write_module(homes)
    path.write_text(path.read_text().replace("    cluster=cluster,\n", ""))
    _applied(homes)
    result, body = piceli("infra", "register", f"{path}:infra", "edge-1")
    assert result.exit_code == 2 and body["reason"] == "infra-no-cluster"


def test_registration_goes_through_the_multicluster_call_when_there(
    homes: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a home cluster: the multi-cluster registration (local profile and Secret)."""
    calls: list[dict[str, Any]] = []

    def multicluster_register(cluster: Cluster, **kwargs: Any) -> dict[str, Any]:
        calls.append({"cluster": cluster.name, **kwargs})
        return {
            "state": "created",
            "profile": cluster.credentials,
            "secret": {"name": "piceli-cluster-edge-1"},
        }

    from piceli.infra import multicluster

    monkeypatch.setattr(multicluster, "register_cluster", multicluster_register)
    edge = Cluster("edge-1", api="https://127.0.0.1:6443", credentials="edge-1")
    home = Cluster("home", api="https://10.0.0.1:6443", credentials="home")
    kubeconfig = _owner_kubeconfig(homes / "k.yaml", "https://127.0.0.1:6443")
    result = register_cluster(edge, kubeconfig, "default", home=home)
    assert result["secret"] == {"name": "piceli-cluster-edge-1"}
    assert calls == [
        {"cluster": "edge-1", "kubeconfig": kubeconfig, "context": "default", "home": home,
         "server": None, "transport": "https"}
    ]  # fmt: skip
    # No home declared: the profile only, even with the module.
    alone = register_cluster(edge, kubeconfig, "default")
    assert alone["state"] == "profile" and alone["secret"] is None
    assert profiles.load_profile("edge-1").context == "default"
    assert len(calls) == 1
