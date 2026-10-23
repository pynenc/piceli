"""The controller publishes a NixRelease with the real ``nix`` (opt-in, 0.24.0).

Set ``PICELI_TEST_NIX=1`` on a machine with a multi-user Nix. A real
controller follows a local flake repository; when ``web-v1.0.0`` appears,
``builder serve`` on a native build host (``--publish``: its key generated
into a credentials directory in ``tmp_path``) builds a builtins-only
derivation as a development run, signs and pushes it to a ``file://`` cache
in ``tmp_path``, and answers; ``nix store verify`` accepts the signature
with the cache's public key. The store path is deleted at the end when Nix
allows it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from piceli.dev import nix
from piceli.dev.model import BuildHost, DevBuilds, DevProfile
from piceli.dev.nix_publish import NIX_PATHS
from piceli.gitops.state import DirectoryChannel
from piceli.infra.builder_serve import BuilderServe
from piceli.infra.composition import load_composition
from piceli.infra.controller import CompositionConfig, CompositionController
from piceli.infra.nix_releases import Publisher, dev_build, dev_push
from piceli.infra.sources import SourceSet
from tests.unit.dev.test_nix_real import FLAKE, IMAGE, NIX, _nix
from tests.unit.infra.git_support import Repo
from tests.unit.infra.test_controller import FakePorts

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("PICELI_TEST_NIX") != "1", reason="set PICELI_TEST_NIX=1"
    ),
    pytest.mark.skipif(NIX is None, reason="no nix on this machine"),
    pytest.mark.timeout(600),
]

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
web_release = NixRelease(web, source=hosts, builder="mini", auto_approve=True)
"""


def test_a_new_tag_is_built_signed_and_published_by_a_native_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from piceli.infra.machines.credentials import save_credential

    system = _nix("eval", "--impure", "--raw", "--expr", "builtins.currentSystem")
    system_name = system.stdout.strip()
    monkeypatch.setenv("PICELI_CREDENTIALS_DIR", str(tmp_path / "credentials"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    monkeypatch.setenv("PATH", os.pathsep.join([os.environ["PATH"], *NIX_PATHS]))
    secret = nix.generate_key("cache-test")
    save_credential("cache-test", "nix-signing-key", secret)
    public = nix.public_key(secret)
    repo = Repo(
        tmp_path / "remotes", "hosts",
        {"flake.nix": FLAKE.replace("SYSTEM", system_name)},
    )  # fmt: skip
    cache_dir = tmp_path / "binary-cache"
    monkeypatch.setenv("NIX_RELEASE_URL", repo.url)
    monkeypatch.setenv("NIX_RELEASE_PUBLIC", public)
    monkeypatch.setenv("NIX_RELEASE_CACHE", str(cache_dir))
    monkeypatch.setenv("NIX_RELEASE_ATTR", f".#packages.{system_name}.hello")
    (tmp_path / "infra.py").write_text(INFRA)
    composition = load_composition(tmp_path / "infra.py")
    state = tmp_path / "state"
    channel = DirectoryChannel(state)
    controller = CompositionController(
        CompositionConfig(composition=composition.to_dict(), poll_seconds=10),
        state_dir=state,
        sources=SourceSet(composition.sources, state / "sources"),
        ports=FakePorts(),
        channel=channel,
    )
    assert controller.poll_once()["nix_releases"]["web-1"]["state"] == "idle"
    commit = repo.tag("web-v1.0.0")
    assert controller.poll_once()["nix_releases"]["web-1"]["state"] == "waiting-build"

    host = BuildHost.native("mini", state_dir=str(tmp_path / "mini"))
    profile = DevProfile.for_nix()
    dev = DevBuilds(node=None, image=IMAGE, profiles=[profile], hosts=[host])
    cache = nix.NixCache(public, url=f"file://{cache_dir}", signing_key="cache-test")
    publisher = Publisher(
        [cache], build=dev_build(dev, host, profile),
        push=dev_push(host, IMAGE), host="mini",
    )  # fmt: skip
    worker = BuilderServe(
        host="mini", image="", channel=channel, container=lambda *_a: 1,
        push=None, index=None, root=tmp_path / "builder",
        sleep=lambda _s: controller.poll_once(), publisher=publisher, images=False,
    )  # fmt: skip
    store_path = None
    try:
        assert worker.once() is not None
        record = controller.poll_once()["nix_releases"]["web-1"]
        assert record["state"] == "published", record
        store_path = record["published"]["store_path"]
        manifest = json.loads((cache_dir / f"piceli/web-1/{commit}.json").read_text())
        assert nix.verify_manifest(manifest, public)
        assert (manifest["commit"], manifest["tag"]) == (commit, "web-v1.0.0")
        assert manifest["store_path"] == store_path
        assert record["published"]["manifest_sha256"] == nix.manifest_digest(manifest)
        verified = _nix(
            "store", "verify", "--store", f"file://{cache_dir}",
            "--trusted-public-keys", public, store_path, check=False,
        )  # fmt: skip
        assert verified.returncode == 0, verified.stderr
        pointer = cache_dir / "piceli/web-1/refs/tags/web-v1.0.0.json"
        assert nix.verify_pointer(json.loads(pointer.read_text()), public)
    finally:
        for link in (tmp_path / "mini").rglob(nix.OUT_LINK):
            link.unlink()
        if store_path:
            from piceli.dev.nix_publish import path_info

            shown = _nix("path-info", "--json", store_path, check=False).stdout
            deriver = path_info(shown, store_path).get("deriver")
            _nix("store", "delete", store_path, check=False)
            if deriver:
                _nix("store", "delete", deriver, check=False)
