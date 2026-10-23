"""The host agent's resolve, verify and realise with the real ``nix`` (opt-in, 0.24.0).

Set ``PICELI_TEST_NIX=1`` on a machine with a multi-user Nix: ``piceli dev
nix build --publish`` builds a builtins-only derivation of a local flake at
the tag ``v1.0.0`` on a native host and publishes it, with its manifest and
tag pointer, to a ``file://`` cache in ``tmp_path``; then the agent follows
``Tag("v*")`` from that cache: it verifies the pointer, the manifest and the
narinfo with the standard-library verifier, runs the real ``nix copy
--from`` (no builder, the cache's key only) and checks the realised path's
NarHash. Only the switch and the unit check are faked. Everything lives in
``tmp_path``; the store path is deleted at the end when Nix allows it.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.dev import nix
from piceli.dev.model import BuildHost, DevBuilds, DevProfile
from piceli.dev.nix import NixCache
from piceli.dev.nix_publish import NIX_PATHS
from piceli.hosts.agent import Agent
from piceli.hosts.model import NixHost, Tag, agent_config
from tests.unit.dev.test_nix_real import FLAKE, IMAGE, NIX, _nix
from tests.unit.dev.test_pack import repo

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("PICELI_TEST_NIX") != "1", reason="set PICELI_TEST_NIX=1"
    ),
    pytest.mark.skipif(NIX is None, reason="no nix on this machine"),
    pytest.mark.timeout(600),
]


def test_the_agent_follows_a_published_tag_with_the_real_nix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import piceli.k8s.cli.dev_nix as dev_nix
    from piceli.infra import Cluster, Node
    from piceli.k8s.cli import app

    assert NIX is not None
    system = _nix("eval", "--impure", "--raw", "--expr", "builtins.currentSystem")
    system_name = system.stdout.strip()
    source = repo(tmp_path / "app", {"flake.nix": FLAKE.replace("SYSTEM", system_name)})
    subprocess.run(["git", "-C", str(source), "tag", "v1.0.0"], check=True)
    monkeypatch.chdir(source)
    monkeypatch.setenv("PICELI_CREDENTIALS_DIR", str(tmp_path / "credentials"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    monkeypatch.setenv("PATH", os.pathsep.join([os.environ["PATH"], *NIX_PATHS]))
    runner = CliRunner()
    made = runner.invoke(app, ["secrets", "nix-cache", "cache-test", "--generate"])
    assert made.exit_code == 0, made.stdout + made.stderr
    public = json.loads(made.stdout)["public_key"]
    cache_dir = tmp_path / "binary-cache"
    cache = NixCache(public, url=f"file://{cache_dir}")
    cluster = Cluster(
        "c", api="https://10.255.0.1:6443", credentials="c",
        nodes=[Node("builder-1", arch="amd64", roles=["builder"])],
        dev=DevBuilds(node=None, image=IMAGE, profiles=[DevProfile.for_nix()],
                      hosts=[BuildHost.native("laptop",
                                              state_dir=str(tmp_path / "state"))]),
    )  # fmt: skip
    monkeypatch.setattr(dev_nix, "load_dev", lambda _ref: cluster.dev)
    monkeypatch.setattr(dev_nix, "load_cache", lambda _ref: cache)
    attribute = f"packages.{system_name}.hello"
    args = [
        "dev", "nix", "build", attribute, "--cluster", "infra.py:c",
        "--ref", "v1.0.0", "--name", "hello", "--publish", "infra.py:cache", "--json",
    ]  # fmt: skip
    planned = runner.invoke(app, args)
    assert planned.exit_code == 3, planned.stdout + planned.stderr
    plan = json.loads(planned.stdout)
    assert plan["plan"]["pointers"] == ["piceli/hello/refs/tags/v1.0.0.json"]
    done = runner.invoke(app, [*args, "--approve", plan["digest"]])
    body: dict[str, Any] = json.loads(done.stdout or "{}")
    store_path = body.get("store_path")
    current = tmp_path / "current-system"
    current.symlink_to("/nix/store/" + "0" * 32 + "-nixos-system-old")
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: Any) -> Any:
        calls.append(list(argv))
        if argv[0] == NIX:  # the real nix: copy and path-info
            env = {**os.environ, **(kwargs.pop("env", None) or {})}
            env["PATH"] = os.environ["PATH"]
            return subprocess.run(argv, env=env, **kwargs)
        if argv[0].endswith("/bin/switch-to-configuration"):
            current.unlink()
            current.symlink_to(argv[0][: -len("/bin/switch-to-configuration")])
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    try:
        assert done.exit_code == 0, done.stdout + done.stderr
        assert body["publish"]["pointers"][0]["ref"] == "refs/tags/v1.0.0"
        declared = NixHost(
            "hello", server="ssh://root@hello.example.net", flake=f".#{attribute}",
            follow=Tag("v*"), cache=cache, build="hello",
        )  # fmt: skip
        config = {
            **agent_config(declared),
            "state_dir": str(tmp_path / "agent"),
            "profile": str(tmp_path / "profile"),
            "current_system": str(current),
            "system": system_name,
            "tools": {"nix": NIX, "nix_env": "fake-nix-env",
                      "systemctl": "fake-systemctl"},
        }  # fmt: skip
        poll = Agent(config, runner=run).run_once()
        status = json.loads((tmp_path / "agent" / "status.json").read_text())
        assert poll["state"] == "deployed", status
        assert status["current"]["tag"] == "v1.0.0"
        assert status["current"]["store_path"] == store_path
        copy = next(call for call in calls if "copy" in call)
        assert copy[copy.index("--from") + 1] == f"file://{cache_dir}"
        # A tampered narinfo is refused before nix runs again.
        narinfo = cache_dir / f"{Path(store_path).name.split('-', 1)[0]}.narinfo"
        narinfo.write_text(narinfo.read_text().replace("NarSize: ", "NarSize: 1"))
        assert not nix.verify_narinfo(narinfo.read_text(), public)
    finally:
        for link in (tmp_path / "state").rglob(nix.OUT_LINK):
            link.unlink()
        if store_path:
            from piceli.dev.nix_publish import path_info

            shown = _nix("path-info", "--json", store_path, check=False).stdout
            deriver = path_info(shown, store_path).get("deriver")
            _nix("store", "delete", store_path, check=False)
            if deriver:
                _nix("store", "delete", deriver, check=False)
