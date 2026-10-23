"""A private repository as a flake input, built on a docker build host (0.26.0).

The live test: the docker build host holds no Git credential of its own;
this machine's ``git-token`` is used only by the short fetch step on that
host, before the build, and the build run never sees it. Opt-in, it needs:

- ``PICELI_TEST_NIX_DOCKER=1``;
- a Linux docker build host with a multi-user Nix (its ``/nix`` and daemon,
  the ``DevProfile.for_nix()`` pattern): the local engine, or
  ``PICELI_TEST_NIX_DOCKER_CONTEXT`` naming a docker context (``ssh://``);
- ``PICELI_TEST_NIX_DOCKER_IMAGE``: the run image, pinned by digest, with
  ``python3`` and ``git``;
- ``PICELI_TEST_PRIVATE_INPUT``: the URL of a private Git repository
  (``https://github.com/org/app`` or ``ssh://git@github.com/org/app``);
- ``PICELI_TEST_PRIVATE_CREDENTIAL``: the ``git-token`` in this machine's
  Piceli secrets that reads it (default ``git``; ``piceli secrets
  git-token git --host github.com --prompt``);
- a local ``nix`` to lock the flake.

The flake reads the input without referring to it (the number of files at
its root), so the published closure holds no private source either.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from piceli.dev.model import BuildHost, DevBuilds, DevProfile
from piceli.dev.nix_publish import FEATURES
from tests.unit.dev.test_nix_real import NIX
from tests.unit.dev.test_pack import commit, repo

ENV = os.environ
pytestmark = [
    pytest.mark.skipif(
        ENV.get("PICELI_TEST_NIX_DOCKER") != "1"
        or not ENV.get("PICELI_TEST_NIX_DOCKER_IMAGE")
        or not ENV.get("PICELI_TEST_PRIVATE_INPUT"),
        reason="set PICELI_TEST_NIX_DOCKER=1, PICELI_TEST_NIX_DOCKER_IMAGE and "
        "PICELI_TEST_PRIVATE_INPUT",
    ),
    pytest.mark.skipif(NIX is None, reason="no local nix to lock the flake"),
    pytest.mark.timeout(1800),
]

FLAKE = """{
  inputs.app = { url = "URL"; flake = false; };
  outputs = { self, app }: {
    packages.x86_64-linux.hello = derivation {
      name = "piceli-private-input-docker";
      system = "x86_64-linux";
      builder = "/bin/sh";
      args = [ "-c" "echo ${toString (builtins.length (builtins.attrNames (builtins.readDir app)))} > $out" ];
    };
    packages.aarch64-linux.hello = derivation {
      name = "piceli-private-input-docker";
      system = "aarch64-linux";
      builder = "/bin/sh";
      args = [ "-c" "echo ${toString (builtins.length (builtins.attrNames (builtins.readDir app)))} > $out" ];
    };
  };
}
"""


def test_a_private_input_builds_on_a_docker_host_without_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import piceli.k8s.cli.dev_nix as dev_nix
    from piceli.dev import nix_fetch, nix_inputs
    from piceli.k8s.cli import app

    url = ENV["PICELI_TEST_PRIVATE_INPUT"]
    name = ENV.get("PICELI_TEST_PRIVATE_CREDENTIAL") or "git"
    credential = nix_inputs.load_git_token(name)  # this machine's store only
    flake_url = url if url.startswith("git+") else f"git+{url}"
    source = repo(tmp_path / "hosts", {"flake.nix": FLAKE.replace("URL", flake_url)})
    with tempfile.TemporaryDirectory() as scratch:  # lock it here, with the token
        mode = "askpass" if url.startswith(("https://", "git+https://")) else "none"
        env = nix_fetch.input_env(
            {"mode": mode, "index": 0, **credential}, scratch, os.environ
        )
        locked = subprocess.run(
            [str(NIX), *FEATURES, "flake", "lock"], cwd=source, env=env,
            capture_output=True, text=True,
        )  # fmt: skip
    assert locked.returncode == 0, locked.stderr
    commit(source, {"flake.lock": (source / "flake.lock").read_text()})
    monkeypatch.chdir(source)
    host = BuildHost.docker(
        "nix-docker",
        context=ENV.get("PICELI_TEST_NIX_DOCKER_CONTEXT") or None,
        image=ENV["PICELI_TEST_NIX_DOCKER_IMAGE"],
        state_dir=str(tmp_path / "state"),
    )
    dev = DevBuilds(node=None, image=ENV["PICELI_TEST_NIX_DOCKER_IMAGE"],
                    profiles=[DevProfile.for_nix()], hosts=[host])  # fmt: skip
    monkeypatch.setattr(dev_nix, "load_dev", lambda _ref: dev)
    from piceli.dev.hosts import ContainerCli

    arch = ContainerCli(host).platform().split("/")[-1]
    system = {"amd64": "x86_64-linux", "arm64": "aarch64-linux"}[arch]
    runner = CliRunner()
    build = ["dev", "nix", "build", f"packages.{system}.hello", "--cluster",
             "infra.py:c", "--host", "nix-docker", "--json"]  # fmt: skip

    # A credential this host does not hold: fetched with nothing, so refused
    # unless the host's store already has the tree from an earlier run.
    missing = runner.invoke(app, [*build, "--input", "app=piceli-test-missing"])
    if missing.exit_code == 2:
        refusal = json.loads(missing.stdout)
        assert refusal["reason"] == "infra-nix-input-unreachable"
        assert "app git+" in missing.stdout + missing.stderr

    done = runner.invoke(app, [*build, "--input", f"app={name}"])
    assert done.exit_code == 0, done.stdout + done.stderr
    body = json.loads(done.stdout)
    assert body["state"] == "built" and body["store_path"]
    for text in (done.stdout, done.stderr, missing.stdout, missing.stderr):
        assert credential["token"] not in text
