"""``piceli dev nix build --publish`` with the real ``nix`` (opt-in, 0.24.0).

Set ``PICELI_TEST_NIX=1`` on a machine with a multi-user Nix: a native
build host builds a builtins-only derivation of a local flake (no network,
no nixpkgs), the publisher signs it with a key Piceli generated and pushes
it to a ``file://`` binary cache in ``tmp_path``, and ``nix store verify``
checks the signature with the public key. Everything lives in ``tmp_path``
(the store path is deleted from the store at the end, when Nix allows it).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.dev import nix
from piceli.dev.model import BuildHost, DevBuilds, DevProfile
from piceli.dev.nix import NixCache
from piceli.dev.nix_publish import FEATURES, NIX_PATHS
from tests.unit.dev.test_pack import repo

IMAGE = "ghcr.io/example/builder@sha256:" + "a" * 64
NIX = shutil.which(
    "nix", path=os.pathsep.join([os.environ.get("PATH", ""), *NIX_PATHS])
)

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("PICELI_TEST_NIX") != "1", reason="set PICELI_TEST_NIX=1"
    ),
    pytest.mark.skipif(NIX is None, reason="no nix on this machine"),
    pytest.mark.timeout(600),
]


def _nix(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    assert NIX is not None
    return subprocess.run(
        [NIX, *FEATURES, *args], capture_output=True, text=True, check=check
    )


FLAKE = """{
  outputs = { self }: {
    packages.SYSTEM.hello = derivation {
      name = "piceli-nix-test";
      system = "SYSTEM";
      builder = "/bin/sh";
      args = [ "-c" "echo built-by-piceli > $out" ];
    };
  };
}
"""


def test_a_native_nix_host_builds_signs_and_pushes_to_a_file_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import piceli.k8s.cli.dev_nix as dev_nix
    from piceli.infra import Cluster, Node
    from piceli.k8s.cli import app

    system = _nix("eval", "--impure", "--raw", "--expr", "builtins.currentSystem")
    flake = FLAKE.replace("SYSTEM", system.stdout.strip())
    source = repo(tmp_path / "app", {"flake.nix": flake})
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
    host = BuildHost.native("laptop", state_dir=str(tmp_path / "state"))
    cluster = Cluster(
        "c", api="https://10.255.0.1:6443", credentials="c",
        nodes=[Node("builder-1", arch="amd64", roles=["builder"])],
        dev=DevBuilds(node=None, image=IMAGE, profiles=[DevProfile.for_nix()],
                      hosts=[host]),
    )  # fmt: skip
    monkeypatch.setattr(dev_nix, "load_dev", lambda _ref: cluster.dev)
    monkeypatch.setattr(dev_nix, "load_cache", lambda _ref: cache)
    args = [
        "dev", "nix", "build", f"packages.{system.stdout.strip()}.hello",
        "--cluster", "infra.py:c", "--ref", "HEAD", "--name", "hello",
        "--publish", "infra.py:cache", "--json",
    ]  # fmt: skip
    planned = runner.invoke(app, args)
    assert planned.exit_code == 3, planned.stdout + planned.stderr
    digest = json.loads(planned.stdout)["digest"]
    done = runner.invoke(app, [*args, "--approve", digest])
    body: dict[str, Any] = json.loads(done.stdout or "{}")
    store_path = body.get("store_path")
    try:
        assert done.exit_code == 0, done.stdout + done.stderr
        assert body["state"] == "published"
        assert Path(store_path).read_text() == "built-by-piceli\n"
        # Nix itself accepts the signature with the public key, and only it.
        verified = _nix(
            "store", "verify", "--store", f"file://{cache_dir}",
            "--trusted-public-keys", public, store_path, check=False,
        )  # fmt: skip
        assert verified.returncode == 0, verified.stderr
        stranger = nix.public_key(nix.generate_key("cache-test"))
        refused = _nix(
            "store", "verify", "--store", f"file://{cache_dir}",
            "--trusted-public-keys", stranger, store_path, check=False,
        )  # fmt: skip
        assert refused.returncode != 0
        # The narinfo verifies with Piceli's standard-library check, as a host will.
        narinfo = (
            cache_dir / f"{Path(store_path).name.split('-', 1)[0]}.narinfo"
        ).read_text()
        assert nix.verify_narinfo(narinfo, public)
        rev = body["commit"]
        manifest = json.loads(
            (cache_dir / "piceli" / "hello" / f"{rev}.json").read_text()
        )
        assert nix.verify_manifest(manifest, public)
        assert manifest["store_path"] == store_path
        sized = _nix("path-info", "--json", "-S", store_path).stdout
        from piceli.dev.nix_publish import path_info

        assert manifest["closure_size"] == path_info(sized, store_path)["closureSize"]
        assert manifest["commit"] == rev and manifest["tag"] is None
        assert manifest["attribute"].startswith(".#packages.")
        # Retention keeps a kept manifest's whole closure; with no kept
        # commit it would remove the manifest, the narinfo and the NAR.
        objects = nix.CacheObjects(cache)
        assert nix.gc_plan(objects, {rev: ["main"]}, keep_tags=5, main="main",
                           grace_seconds=0, now=time.time())["delete"] == []  # fmt: skip
        gone = nix.gc_plan(objects, {}, keep_tags=5, main="main",
                           grace_seconds=0, now=time.time())["delete"]  # fmt: skip
        assert len(gone) == 3 and f"piceli/hello/{rev}.json" in gone
        assert nix.narinfo_fields(narinfo)["NarHash"] == manifest["nar_hash"]
        assert body["publish"]["signatures"]
        secret = json.loads((tmp_path / "credentials" / "cache-test.json").read_text())[
            "value"
        ]
        for text in (done.stdout, done.stderr, json.dumps(manifest)):
            assert secret.partition(":")[2] not in text
    finally:
        # The run's out-link is a GC root of the native host's store.
        for link in (tmp_path / "state").rglob(nix.OUT_LINK):
            link.unlink()
        if store_path:
            from piceli.dev.nix_publish import path_info

            shown = _nix("path-info", "--json", store_path, check=False).stdout
            deriver = path_info(shown, store_path).get("deriver")
            _nix("store", "delete", store_path, check=False)
            if deriver:
                _nix("store", "delete", deriver, check=False)
