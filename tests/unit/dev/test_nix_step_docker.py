"""A private input fetched on a docker build host with no image of its own (0.26.1).

The live test of the two 0.26.0 failures of ``builder serve --publish``:
``BuildHost.docker(..., runs=False)`` without an image,
``DevProfile.for_nix(timeout="2h")`` without one either, so the steps run
the ``DevBuilds`` builder image; a private input read with a deploy key (a
``git-ssh-key``). On 0.26.0 the fetch ran an image named ``None`` (docker:
``invalid reference format``) and, given an image, Nix could not find the
home of user 65532 (``cannot determine user's home directory``), nor could
``ssh`` (``No user exists for uid 65532``). Here the publisher of
``builder serve`` fetches the input with the deploy key and then publishes
the fetched tree to a ``file://`` cache inside the container.

Opt-in: ``PICELI_TEST_NIX_STEP_DOCKER=1`` and a Docker engine that can run
the builder image (pulled if missing; ``PICELI_TEST_NIX_STEP_IMAGE``
overrides it, pinned by digest). It needs network access to pull
``nixos/nix`` and ``alpine`` (``openssh``, ``git``).

- **The engine**: the local one, or ``PICELI_TEST_NIX_DOCKER_CONTEXT``
  (a docker context or ``ssh://``). Every container of the test runs there.
- **The Nix store**: with ``PICELI_TEST_NIX_STEP_HOST_NIX=1`` the engine's
  own ``/nix`` and daemon (a Linux build host with a multi-user Nix, the
  ``DevProfile.for_nix()`` pattern as declared); otherwise (Docker Desktop:
  its ``/nix`` is not Linux) a Linux store in a volume, copied from
  ``nixos/nix``, with a ``nix-daemon`` container, mounted where the steps
  mount ``/nix``.
- **The private repository**: an ``sshd`` container serving a Git
  repository to one deploy key, generated here.

Every container and the volume are removed at the end, on failure too.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from piceli.dev.model import BuildHost, DevBuilds, DevProfile

ENV = os.environ
#: The 0.26.0 builder image, the one the report ran.
BUILDER = (
    "ghcr.io/pynenc/piceli-builder@sha256:"
    "c899c074bbf7cd7b49e325f603da04fcbd6d7d8c40187a3ade58cfc8a54c595a"
)
NIXOS = "docker.io/nixos/nix:latest"
ALPINE = "docker.io/library/alpine:3.20"
pytestmark = [
    pytest.mark.skipif(
        ENV.get("PICELI_TEST_NIX_STEP_DOCKER") != "1",
        reason="set PICELI_TEST_NIX_STEP_DOCKER=1 (and a Docker engine)",
    ),
    pytest.mark.skipif(shutil.which("docker") is None, reason="no docker CLI"),
    pytest.mark.timeout(1800),
]

SSHD = r"""set -e
apk add --no-cache openssh git >/dev/null
adduser -D -s /usr/bin/git-shell git
sed -i 's/^git:!/git:*/' /etc/shadow
mkdir -p /home/git/.ssh
printf '%s\n' "$KEY" > /home/git/.ssh/authorized_keys
chmod 700 /home/git/.ssh && chmod 600 /home/git/.ssh/authorized_keys
git init -q --bare /home/git/app.git
mkdir /w && cd /w && git init -q && echo private > README
git add . && git -c user.email=a@b -c user.name=a commit -qm init
git push -q /home/git/app.git HEAD:refs/heads/main
git -C /home/git/app.git symbolic-ref HEAD refs/heads/main
chown -R git:git /home/git
ssh-keygen -A >/dev/null
echo ready
exec /usr/sbin/sshd -D -e
"""
LOCK = r"""set -e
printf '%s' "$KEY" > /tmp/k && chmod 600 /tmp/k
export GIT_SSH_COMMAND="ssh -i /tmp/k -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
nix --extra-experimental-features 'nix-command flakes' eval --impure --json --expr \
  "let t = builtins.fetchTree { type = \"git\"; url = \"$URL\"; };
   in { inherit (t) narHash rev lastModified; }"
"""


class Engine:
    """The docker engine of the build host; what it started goes at the end."""

    def __init__(self, host: BuildHost) -> None:
        from piceli.dev.hosts import ContainerCli

        self.cli = ContainerCli(host)
        self.containers: list[str] = []
        self.volumes: list[str] = []

    def run(self, *args: str, timeout: float = 900) -> str:
        done = subprocess.run(
            self.cli.argv(*args), capture_output=True, timeout=timeout, check=False
        )
        assert done.returncode == 0, done.stderr.decode("utf-8", "replace")[-2000:]
        return done.stdout.decode("utf-8", "replace")

    def start(self, name: str, *args: str) -> str:
        self.containers.append(name)
        self.run("run", "-d", "--name", name, *args)
        return name

    def wait_log(self, name: str, text: str, seconds: float = 300) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            done = subprocess.run(
                self.cli.argv("logs", name), capture_output=True, check=False
            )
            if text in (done.stdout + done.stderr).decode("utf-8", "replace"):
                return
            time.sleep(1)
        raise AssertionError(f"{name} never said {text!r}")

    def wait_file(self, name: str, path: str, seconds: float = 120) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            argv = self.cli.argv("exec", name, "test", "-e", path)
            if subprocess.run(argv, capture_output=True, check=False).returncode == 0:
                return
            time.sleep(1)
        raise AssertionError(f"{name} has no {path}")

    def close(self) -> None:
        for name in self.containers:
            subprocess.run(self.cli.argv("rm", "-f", name), capture_output=True)
        for volume in self.volumes:
            subprocess.run(self.cli.argv("volume", "rm", "-f", volume),
                           capture_output=True)  # fmt: skip


def _deploy_key() -> tuple[str, str]:
    """A new ed25519 deploy key: (OpenSSH private key, public key line)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    key = ed25519.Ed25519PrivateKey.generate()
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(
            serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
        )
        .decode()
    )
    return private, public


@pytest.fixture
def engine() -> Iterator[tuple[BuildHost, Engine]]:
    host = BuildHost.docker(
        "nix-host", context=ENV.get("PICELI_TEST_NIX_DOCKER_CONTEXT") or None,
        slots=1, runs=False,
    )  # fmt: skip
    found = Engine(host)
    try:
        yield host, found
    finally:
        found.close()


def test_builder_serve_fetches_a_private_input_with_the_builder_image(
    engine: tuple[BuildHost, Engine],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import piceli.dev.hosts as hosts
    from piceli.dev import nix, nix_inputs
    from piceli.k8s.cli import builder

    host, docker = engine
    image = ENV.get("PICELI_TEST_NIX_STEP_IMAGE") or BUILDER
    tag = secrets.token_hex(4)
    private, public = _deploy_key()

    # The Nix store of the build host.
    if ENV.get("PICELI_TEST_NIX_STEP_HOST_NIX") != "1":
        volume = f"piceli-test-nix-{tag}"
        docker.volumes.append(volume)
        docker.run("volume", "create", volume)
        docker.run("run", "--rm", "-v", f"{volume}:/target", "--entrypoint", "sh",
                   NIXOS, "-c", "cp -a /nix/. /target/")  # fmt: skip
        docker.start(f"piceli-test-nixd-{tag}", "-v", f"{volume}:/nix",
                     "--entrypoint", "nix-daemon", NIXOS)  # fmt: skip
        docker.wait_file(f"piceli-test-nixd-{tag}", "/nix/var/nix/daemon-socket/socket")
        monkeypatch.setattr(
            hosts, "NIX_MOUNT", ("-v", f"{volume}:/nix:ro", "-e", "NIX_REMOTE=daemon")
        )

    # The private repository, readable with the deploy key only.
    sshd = docker.start(f"piceli-test-sshd-{tag}", "-e", f"KEY={public}",
                        "--entrypoint", "sh", ALPINE, "-c", SSHD)  # fmt: skip
    docker.wait_log(sshd, "Server listening")
    address = docker.run(
        "inspect", "-f", "{{.NetworkSettings.IPAddress}}", sshd
    ).strip()
    url = f"ssh://git@{address}/home/git/app.git"
    locking = ["run", "--rm", "-e", f"KEY={private}", "-e", f"URL={url}",
               "--entrypoint", "sh", NIXOS, "-c", LOCK]  # fmt: skip
    pinned = json.loads(docker.run(*locking))
    lock = json.dumps(
        {
            "version": 7,
            "root": "root",
            "nodes": {
                "root": {"inputs": {"app": "app"}},
                "app": {
                    "locked": {"type": "git", "url": url, **pinned},
                    "original": {"type": "git", "url": url},
                },
            },
        }
    )

    # This machine holds the deploy key; the build host's steps get it on stdin.
    monkeypatch.setenv("PICELI_CREDENTIALS_DIR", str(tmp_path / "credentials"))
    nix_inputs.save_git_ssh_key("app-read", address, private)
    dev = DevBuilds(node=None, image=image, hosts=[host],
                    profiles=[DevProfile.for_nix(timeout="2h")])  # fmt: skip
    publisher = builder._publisher([], dev, host)  # as builder serve --publish

    said: list[str] = []
    plan: dict[str, Any] = {
        "inputs": nix_inputs.covered(lock, declared={"app": "app-read"})
    }
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / nix_inputs.LOCK_FILE).write_text(lock)
    publisher.fetch_inputs(
        plan, checkout, said.append, lambda _stage: contextlib.nullcontext()
    )
    outcome = publisher.fetch(nix_inputs.resolve(plan["inputs"], lock), said.append)
    assert outcome["state"] == "fetched", said
    store_path = outcome["inputs"][0]["store_path"]
    assert store_path.startswith("/nix/store/")

    # The publisher step runs the same way (0.26.0: the same two failures).
    request = {"store_path": store_path, "store_url": "file:///tmp/cache",
               "signing_key": nix.generate_key("cache-1")}  # fmt: skip
    published = publisher.push(request, said.append)
    assert published["state"] == "published", said
    assert published["nar_hash"] == pinned["narHash"]
    joined = "\n".join(said)
    assert "BEGIN OPENSSH PRIVATE KEY" not in joined
    assert private.splitlines()[1] not in joined
