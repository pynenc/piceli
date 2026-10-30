"""A Nix cache declared before its key exists (0.25.0).

``NixCache(key_name="cache-1", ...)`` names the key without its public key:
``piceli secrets status`` and ``setup`` load the module (the key is
``missing``, then ``copy`` once generated), ``infra plan`` of the bucket
works, and whatever needs the key (publishing, gc, a host's module, a
``NixRelease``) refuses ``dev-nix-cache-key-unset``. A module whose
declaration raises is reported under the declaration's own code.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.dev import nix
from piceli.dev.model import DevError
from piceli.infra import NixCache, NixHost, Source, Tag
from piceli.infra.composition import composition_from
from piceli.infra.machines import credentials
from piceli.infra.nix_releases import NixRelease, cache_keys
from piceli.k8s.cli import app

from .machines_support import last_json

UNSET = "dev-nix-cache-key-unset"

MODULE = """
from pathlib import Path
from piceli.infra import Bucket, Infrastructure, NixCache, NixHost, S3Key, Tag
from piceli.testing.infra import FakeProvider

bucket = Bucket("example-nix-cache", provider=FakeProvider(), keys=[
    S3Key("nix-cache-write"), S3Key("nix-cache-read", access="read")])
cache = NixCache(key_name="cache-1", bucket=bucket, endpoint="s3.example.org")
infra = Infrastructure("web-1", buckets=[bucket], state_dir=Path(STATE))
host = NixHost("web-1", server="ssh://root@web-1.example.org", flake=".#web-1",
               follow=Tag("v*"), cache=cache)
"""


def _module(homes: Path, body: str = MODULE, name: str = "keyless") -> str:
    path = homes / f"{name}.py"
    path.write_text(f"STATE = {str(homes / 'infra-state')!r}\n" + body)
    return str(path)


def _run(*args: str) -> Any:
    return CliRunner().invoke(app, list(args))


def _pending(**kwargs: Any) -> NixCache:
    return NixCache(key_name="cache-1", url="file:///srv/nix-cache", **kwargs)


# ------------------------------------------------------------- declaration


def test_a_cache_names_its_key_before_it_exists() -> None:
    cache = _pending()
    assert (cache.key_name, cache.public_key, cache.key_set) == ("cache-1", None, False)
    assert cache.signing_credential == "cache-1"
    with pytest.raises(DevError) as raised:
        cache.trusted_key()
    assert raised.value.code == UNSET
    assert "piceli secrets setup MODULE" in str(raised.value)
    assert "piceli secrets nix-cache cache-1 --generate" in str(raised.value)
    assert "NixCache(public_key=)" in str(raised.value)
    # The full key keeps working, positionally, and names the key.
    public = nix.public_key(nix.generate_key("cache-1"))
    full = NixCache(public, url="file:///srv/nix-cache")
    assert (full.key_name, full.trusted_key(), full.key_set) == (
        "cache-1",
        public,
        True,
    )
    assert NixCache(public, url="file:///x", key_name="cache-1").key_name == "cache-1"


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"key_name": "bad name"},
        {"key_name": "cache-2", "public_key": "PLACEHOLDER"},
        {"public_key": "cache-1:REPLACE-WITH-THE-CACHE-PUBLIC-KEY"},
    ],
    ids=["neither", "bad-name", "name-differs", "placeholder"],
)
def test_a_cache_refuses_a_missing_or_wrong_key(kwargs: dict[str, Any]) -> None:
    if kwargs.get("public_key") == "PLACEHOLDER":
        kwargs["public_key"] = nix.public_key(nix.generate_key("cache-1"))
    with pytest.raises(DevError) as raised:
        NixCache(url="file:///srv/nix-cache", **kwargs)
    assert raised.value.code == "dev-nix-cache-invalid"


def test_everything_that_needs_the_key_refuses() -> None:
    from piceli.hosts.model import agent_config

    cache = _pending()
    host = NixHost("web-1", server="ssh://root@web-1.example.org", flake=".#web-1",
                   follow=Tag("v*"), cache=cache)  # fmt: skip
    hosts = Source("https://git.example.org/hosts.git", name="hosts")
    release = NixRelease(host, source=hosts, builder="mini")
    for refused in (
        lambda: nix.publish_plan(
            target=".#web-1",
            name="web-1",
            commit="a" * 40,
            tag=None,
            host="nix-1",
            profile="nix",
            image=False,
            cache=cache,
        ),
        lambda: agent_config(host),
        lambda: cache_keys(cache),
    ):
        with pytest.raises(DevError) as raised:
            refused()
        assert raised.value.code == UNSET
    # The composition loads: the release is refused when the controller polls.
    (target,) = composition_from({"web_release": release}).nix_releases
    assert target.public_key is None
    assert "piceli secrets setup MODULE" in str(target.key_unset)


# ------------------------------------------------------------- CLI


def test_status_and_setup_work_before_and_after_the_key(homes: Path) -> None:
    ref = _module(homes)
    before = _run("secrets", "status", ref, "--json")
    assert before.exit_code == 1, before.output
    row = {r["name"]: r for r in json.loads(before.stdout)["secrets"]}["cache-1"]
    assert (row["kind"], row["state"], row["next"]) == (
        "nix-signing-key", "missing", "piceli secrets nix-cache cache-1 --generate"
    )  # fmt: skip
    setup = _run("secrets", "setup", ref, "--yes", "--json")
    assert setup.exit_code == 0, setup.output
    stored = nix.public_key(credentials.load_credential("cache-1", "nix-signing-key"))
    assert f"public key: {stored}" in setup.stderr
    assert f"copy: {stored} into NixCache(public_key=...) (cache-1)" in setup.stderr
    assert last_json(setup.stdout)["copy"] == [
        {"name": "cache-1", "kind": "nix-signing-key", "public_key": stored,
         "into": "NixCache(public_key=...)"},
    ]  # fmt: skip
    after = _run("secrets", "status", ref, "--json")
    assert after.exit_code == 1, after.output
    row = {r["name"]: r for r in json.loads(after.stdout)["secrets"]}["cache-1"]
    assert (row["state"], row["next"], row["detail"]) == (
        "copy", "piceli secrets nix-cache cache-1",
        f"put {stored} into NixCache(public_key=)",
    )  # fmt: skip
    again = _run("secrets", "setup", ref, "--yes")
    assert f"cache-1: stored; public key: {stored}" in again.stderr
    assert credentials.load_credential("cache-1", "nix-signing-key")  # not replaced
    # Once the declaration holds it, the key is present.
    filled = MODULE.replace('key_name="cache-1"', f"public_key={stored!r}")
    done = _run("secrets", "status", _module(homes, filled, "filled"), "--json")
    row = {r["name"]: r for r in json.loads(done.stdout)["secrets"]}["cache-1"]
    assert row["state"] == "present", done.output


def test_host_module_and_gc_refuse_until_the_key_is_declared(homes: Path) -> None:
    ref = _module(homes)
    for args in (
        ("host", "module", f"{ref}:host"),
        ("dev", "nix", "gc", "--cache", f"{ref}:cache", "--repo", str(homes)),
    ):
        result = _run(*args)
        assert result.exit_code == 2, result.output
        assert json.loads(result.stdout)["reason"] == UNSET, args


@pytest.mark.tofu
def test_infra_plan_of_the_bucket_needs_no_signing_key(homes: Path) -> None:
    credentials.save_credential("web-1-state", "state-key", "p" * 32)
    ref = _module(homes)
    planned = _run("infra", "plan", f"{ref}:infra")
    assert planned.exit_code == 3, planned.output
    keys = {k["name"] for k in last_json(planned.stdout)["s3_keys"]}
    assert keys == {"nix-cache-write", "nix-cache-read"}


def test_a_refused_declaration_is_reported_under_its_own_code(homes: Path) -> None:
    ref = _module(
        homes, MODULE.replace('key_name="cache-1"', '"cache-1:REPLACE-WITH-THE-KEY"')
    )
    for command in ("status", "setup"):
        result = _run("secrets", command, ref)
        assert result.exit_code == 2, result.output
        body = json.loads(result.stdout)
        assert body["reason"] == "dev-nix-cache-invalid", body
        assert "NixCache(public_key=)" in body["message"]
    raising = homes / "raising.py"
    raising.write_text("raise RuntimeError('boom')\n")
    result = _run("secrets", "status", str(raising))
    assert json.loads(result.stdout)["reason"] == "infra-load-failed"
    missing = _run("secrets", "status", "no_such_module_here")
    assert json.loads(missing.stdout)["reason"] == "infra-not-found"
