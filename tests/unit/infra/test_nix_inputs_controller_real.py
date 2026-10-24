"""A controller release seals its Git token for one build (real ``nix``, 0.26.0).

Opt-in: ``PICELI_TEST_NIX=1`` on a machine with a multi-user Nix and Git.
A local smart-HTTP Git server serves the private ``app.git`` only with
basic auth. A real controller holds the token in its Git Secret directory
and follows the flake's local repository ``hosts``; a
``NixRelease(credentials="controller")`` is planned on a new tag, and a real
``BuilderServe`` on a native build host with no Git credential of its own
claims it with a one-time key, opens the sealed token, fetches ``app`` into
the store before the build, builds and publishes. The token is in no state
file, request, status, cache object or closure file.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
from pathlib import Path
from typing import Any

import pytest

from piceli.dev import nix
from piceli.dev.model import BuildHost, DevBuilds, DevProfile
from piceli.dev.nix_fetch import eval_argv
from piceli.dev.nix_inputs import fetch_inputs
from piceli.dev.nix_publish import NIX_PATHS, path_info
from piceli.gitops.state import DirectoryChannel
from piceli.infra.builder_serve import BuilderServe
from piceli.infra.composition import load_composition
from piceli.infra.controller import CompositionConfig, CompositionController
from piceli.infra.nix_releases import Publisher, dev_build, dev_push
from piceli.infra.sources import SourceSet
from tests.unit.dev.test_nix_inputs_real import (  # noqa: F401 - the fixture
    USER,
    _askpass_env,
    _files,
    git_server,
)
from tests.unit.dev.test_nix_real import IMAGE, NIX, _nix
from tests.unit.dev.test_pack import repo
from tests.unit.infra.git_support import Repo
from tests.unit.infra.test_controller import FakePorts

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("PICELI_TEST_NIX") != "1", reason="set PICELI_TEST_NIX=1"
    ),
    pytest.mark.skipif(NIX is None, reason="no nix on this machine"),
    pytest.mark.timeout(600),
]

FLAKE = """{
  inputs.app.url = "git+http://127.0.0.1:PORT/app.git";
  outputs = { self, app }: {
    packages.SYSTEM.hello = derivation {
      name = "piceli-sealed-input-test";
      system = "SYSTEM";
      builder = "/bin/sh";
      args = [ "-c" "echo ${app.message} > $out" ];
    };
  };
}
"""
INFRA = """
import os

from piceli.infra import NixCache, NixHost, NixRelease, Source, Tag

hosts = Source(os.environ["NIX_RELEASE_URL"], name="hosts")
cache = NixCache(os.environ["NIX_RELEASE_PUBLIC"],
                 url="file://" + os.environ["NIX_RELEASE_CACHE"],
                 signing_key="cache-test")
web = NixHost("web-1", server="ssh://root@web-1.example.org",
              flake=os.environ["NIX_RELEASE_ATTR"], follow=Tag("web-v*"),
              cache=cache)
web_release = NixRelease(web, source=hosts, builder="mini", auto_approve=True,
                         inputs={"app": "unused"}, credentials="controller")
"""


def test_the_controller_seals_its_token_for_the_build_that_needs_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    git_server: dict[str, Any],  # noqa: F811 - the imported fixture
) -> None:
    from piceli.infra.machines.credentials import save_credential

    token, port, served = git_server["token"], git_server["port"], git_server["root"]
    system = _nix("eval", "--impure", "--raw", "--expr", "builtins.currentSystem")
    system_name = system.stdout.strip()
    message = "sealed-" + secrets.token_hex(8)
    private = repo(tmp_path / "app-src", {"flake.nix":
                   f'{{ outputs = _: {{ message = "{message}"; }}; }}\n'})  # fmt: skip
    subprocess.run(["git", "clone", "-q", "--bare", str(private),
                    str(served / "app.git")], check=True)  # fmt: skip
    hosts = Repo(tmp_path / "remotes", "hosts", {"flake.nix": FLAKE.replace("PORT", str(port))
                                   .replace("SYSTEM", system_name)})  # fmt: skip
    locked = subprocess.run(
        [str(NIX), "--extra-experimental-features", "nix-command flakes",
         "flake", "lock"], cwd=hosts.work, env=_askpass_env(tmp_path, token),
        capture_output=True, text=True,
    )  # fmt: skip
    assert locked.returncode == 0, locked.stderr
    hosts.commit({"flake.lock": (hosts.work / "flake.lock").read_text()})
    lock = json.loads((hosts.work / "flake.lock").read_text())
    shown = subprocess.run(
        eval_argv(str(NIX), lock["nodes"]["app"]["locked"]),
        env=_askpass_env(tmp_path, token), capture_output=True, text=True,
    )  # fmt: skip
    assert shown.returncode == 0, shown.stderr
    tree = shown.stdout.strip()
    _nix("store", "delete", tree, check=False)
    assert not Path(tree).exists()

    monkeypatch.setenv("PICELI_CREDENTIALS_DIR", str(tmp_path / "credentials"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.setenv("PATH", os.pathsep.join([os.environ["PATH"], *NIX_PATHS]))
    secret = nix.generate_key("cache-test")
    save_credential("cache-test", "nix-signing-key", secret)
    public = nix.public_key(secret)
    cache_dir = tmp_path / "binary-cache"
    monkeypatch.setenv("NIX_RELEASE_URL", hosts.url)
    # A Source URL is https://, ssh:// or a path, and this server speaks
    # http: the hosts the token may go to are this server's (their
    # derivation from the composition's sources has its own unit test).
    import piceli.dev.nix_inputs as nix_inputs

    monkeypatch.setattr(nix_inputs, "git_hosts", lambda _sources: ["127.0.0.1"])
    monkeypatch.setenv("NIX_RELEASE_PUBLIC", public)
    monkeypatch.setenv("NIX_RELEASE_CACHE", str(cache_dir))
    monkeypatch.setenv("NIX_RELEASE_ATTR", f".#packages.{system_name}.hello")
    (tmp_path / "infra.py").write_text(INFRA)
    composition = load_composition(tmp_path / "infra.py")
    git_secret = tmp_path / "git-secret"  # the controller's mounted Secret
    git_secret.mkdir()
    (git_secret / "username").write_text(USER)
    (git_secret / "password").write_text(token)
    state = tmp_path / "state"
    channel = DirectoryChannel(state)
    controller = CompositionController(
        CompositionConfig(composition=composition.to_dict(), poll_seconds=10),
        state_dir=state,
        sources=SourceSet(
            composition.sources, state / "sources", credentials_dir=git_secret
        ),
        ports=FakePorts(),
        channel=channel,
    )
    assert controller.poll_once()["nix_releases"]["web-1"]["state"] == "idle"
    hosts.tag("web-v1.0.0")
    record = controller.poll_once()["nix_releases"]["web-1"]
    assert record["state"] == "waiting-build", record
    assert record["plan"]["credentials"] == "controller"
    assert record["plan"]["git_hosts"] == ["127.0.0.1"]

    host = BuildHost.native("mini", state_dir=str(tmp_path / "mini"))
    profile = DevProfile.for_nix()
    dev = DevBuilds(node=None, image=IMAGE, profiles=[profile], hosts=[host])
    publisher = Publisher(
        [nix.NixCache(public, url=f"file://{cache_dir}", signing_key="cache-test")],
        build=dev_build(dev, host, profile), push=dev_push(host, IMAGE),
        host="mini", fetch=fetch_inputs(host, IMAGE),
    )  # fmt: skip
    said: list[str] = []
    worker = BuilderServe(
        host="mini", image="", channel=channel, container=lambda *_a: 1,
        push=None, index=None, root=tmp_path / "builder", say=said.append,
        sleep=lambda _s: controller.poll_once(), publisher=publisher, images=False,
    )  # fmt: skip
    store_path = None
    try:
        assert worker.once() is not None
        record = controller.poll_once()["nix_releases"]["web-1"]
        assert record["state"] == "published", record.get("failure")
        store_path = record["published"]["store_path"]
        assert Path(store_path).read_text() == message + "\n"
        assert "inputs" in record["stages"]
        closure = _nix("path-info", "--json", "-r", store_path).stdout
        assert token not in closure and token not in "\n".join(said)
        for path in [*state.rglob("*"), *cache_dir.rglob("*")]:
            if path.is_file():
                assert token.encode() not in path.read_bytes(), path
        paths = json.loads(closure)
        names = list(paths) if isinstance(paths, dict) else [p["path"] for p in paths]
        for found in [*names, tree]:
            for data in _files(found):
                assert token.encode() not in data
    finally:
        for link in (tmp_path / "mini").rglob(nix.OUT_LINK):
            link.unlink()
        if store_path:
            shown_info = _nix("path-info", "--json", store_path, check=False).stdout
            deriver = path_info(shown_info, store_path).get("deriver")
            _nix("store", "delete", store_path, check=False)
            if deriver:
                _nix("store", "delete", deriver, check=False)
        _nix("store", "delete", tree, check=False)
