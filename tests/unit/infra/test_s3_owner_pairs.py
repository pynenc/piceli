"""S3 pairs the owner makes by hand (0.25.3).

Some clouds let only the account's own user create S3 (EC2) credentials,
never an application credential: the ``Infomaniak`` preset refuses
``Bucket(keys=[S3Key(...)])`` at declaration (``infra-s3-keys-unsupported``)
with the commands that make and store each pair by hand. A ``NixCache``
names such pairs (``write_credentials=``, ``read_credentials=``);
``piceli host secrets`` installs the read pair as it installs a bucket's
read key, and ``secrets status`` lists both ``pending`` with the command
that stores them.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.dev.model import DevError
from piceli.errors import ERRORS
from piceli.hosts.model import PICELI_CACHE_ENV
from piceli.infra import (
    Bucket,
    Infomaniak,
    NixCache,
    NixHost,
    NixRelease,
    OpenStack,
    Source,
    Tag,
)
from piceli.infra.machines import credentials
from piceli.infra.machines.keys import needed_secrets
from piceli.infra.machines.model import InfraError, S3Key
from piceli.k8s.cli import app

PUBLIC = "cache-1:" + "A" * 43 + "="
OS = OpenStack(
    credentials="cloud", auth_url="https://identity.example.net/v3",
    region="region-1", public_network="public", flavors=["small"],
)  # fmt: skip
KEYS = [S3Key("cache-write"), S3Key("cache-read", access="read")]


def _owner_cache(**kwargs: Any) -> NixCache:
    bucket = Bucket("nix-cache", provider=Infomaniak(credentials="cloud"))
    return NixCache(
        PUBLIC, bucket=bucket, write_credentials="cache-write",
        read_credentials="cache-read", **kwargs,
    )  # fmt: skip


def _host(cache: NixCache, **kwargs: Any) -> NixHost:
    return NixHost("web-1", server="ssh://root@web-1.example.org", flake=".#web-1",
                   follow=Tag("v*"), cache=cache, **kwargs)  # fmt: skip


# ------------------------------------------------------------- the preset


def test_the_infomaniak_preset_cannot_create_s3_keys() -> None:
    assert OS.supports_s3_keys is True
    assert Infomaniak(credentials="cloud").supports_s3_keys is False
    assert "infra-s3-keys-unsupported" in ERRORS


def test_a_keyed_bucket_at_infomaniak_is_refused_with_the_commands() -> None:
    with pytest.raises(InfraError) as caught:
        Bucket("nix-cache", provider=Infomaniak(credentials="cloud"), keys=KEYS)
    error = caught.value
    assert error.code == "infra-s3-keys-unsupported"
    text = str(error)
    assert "lets only the account's own user create S3 credentials" in text
    assert "signed in as the project's user: openstack ec2 credentials create" in text
    assert "piceli secrets s3 cache-write --prompt" in text
    assert "piceli secrets s3 cache-read --prompt" in text
    assert (
        'NixCache(bucket=..., write_credentials="cache-write", '
        'read_credentials="cache-read")'
    ) in text
    only_write = pytest.raises(InfraError, match='write_credentials="w-1"\\)')
    with only_write:
        Bucket(
            "nix-cache", provider=Infomaniak(credentials="cloud"), keys=[S3Key("w-1")]
        )


def test_the_generic_openstack_path_still_creates_keys() -> None:
    bucket = Bucket("nix-cache", provider=OS, keys=KEYS)
    cache = NixCache(PUBLIC, bucket=bucket, endpoint="s3.example.org")
    assert (cache.write_credentials, cache.read_credentials) == (
        "cache-write", "cache-read",
    )  # fmt: skip
    assert cache.creates("cache-read")


def test_infra_plan_refuses_the_keyed_bucket_before_anything_runs(
    homes: Path,
) -> None:
    module = homes / "cache_infra.py"
    module.write_text(
        "from piceli.infra import Bucket, Infomaniak, Infrastructure, S3Key\n"
        "bucket = Bucket('nix-cache', provider=Infomaniak(credentials='cloud'),\n"
        "                keys=[S3Key('cache-write'), S3Key('cache-read', access='read')])\n"
        "infra = Infrastructure('web', buckets=[bucket])\n"
    )
    result = CliRunner().invoke(app, ["infra", "plan", f"{module}:infra"])
    assert result.exit_code == 2, result.output
    body = json.loads(result.stdout)
    assert body["reason"] == "infra-s3-keys-unsupported"
    assert "openstack ec2 credentials create" in body["message"]
    assert "[infra-s3-keys-unsupported]" in result.stderr


# ------------------------------------------------------------- the cache


def test_a_cache_names_owner_made_pairs_without_keys() -> None:
    cache = _owner_cache()
    assert cache.endpoint == "s3.pub1.infomaniak.cloud"
    assert (cache.write_credentials, cache.read_credentials) == (
        "cache-write", "cache-read",
    )  # fmt: skip
    assert not cache.creates("cache-write") and not cache.creates("cache-read")
    assert cache.describe()["read_credentials"] == "cache-read"
    write_only = NixCache(
        PUBLIC, bucket=Bucket("nix-cache", provider=Infomaniak(credentials="cloud")),
        write_credentials="cache-write",
    )  # fmt: skip
    assert write_only.read_credentials is None
    on_url = NixCache(PUBLIC, url="s3://nix-cache?endpoint=s3.example.org",
                      write_credentials="w-1", read_credentials="r-1")  # fmt: skip
    assert on_url.read_credentials == "r-1"


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"read_credentials": "cache-write"}, "the write pair"),
        ({"read_credentials": "Not A Name"}, "credential names"),
        ({"read_credentials": "cache-read", "keys": KEYS}, "one of"),
        ({"read_credentials": "cache-write", "keys": [KEYS[0]]}, "the write pair"),
        (
            {"read_credentials": "other-key", "keys": [*KEYS, S3Key("other-key")]},
            "one of",
        ),
        ({"read_credentials": "r-1", "url": "file:///srv/cache"}, "s3 cache"),
    ],
)
def test_a_wrong_read_pair_is_refused(kwargs: dict[str, Any], match: str) -> None:
    keys = kwargs.pop("keys", None)
    url = kwargs.pop("url", None)
    if url is not None:
        place: dict[str, Any] = {"url": url}
    elif keys is not None:
        place = {"bucket": Bucket("nix-cache", provider=OS, keys=keys),
                 "endpoint": "s3.example.org"}  # fmt: skip
    else:
        place = {"bucket": Bucket("nix-cache", provider=Infomaniak(credentials="cloud")),
                 "write_credentials": "cache-write"}  # fmt: skip
    with pytest.raises(DevError, match=match) as caught:
        NixCache(PUBLIC, **place, **kwargs)
    assert caught.value.code == "dev-nix-cache-invalid"


def test_the_host_installs_an_owner_made_read_pair_like_a_bucket_key() -> None:
    host = _host(_owner_cache())
    assert host.read_credentials_file == PICELI_CACHE_ENV
    assert host.cache_credentials == "cache-read"
    own = _host(_owner_cache(), read_credentials_file="/run/secrets/piceli/cache.env")
    assert own.cache_credentials is None


# ------------------------------------------------------------- secrets


def test_needed_secrets_reports_both_pairs_as_the_owners() -> None:
    host = _host(_owner_cache())
    release = NixRelease(host, source=Source("https://git.example.org/hosts.git"),
                         builder="mini")  # fmt: skip
    found = {s.name: s.to_dict() for s in needed_secrets(release)}
    write, read = found["cache-write"], found["cache-read"]
    for row, name in ((write, "cache-write"), (read, "cache-read")):
        assert row["kind"] == "s3-keys"
        assert row["created_by"] == "owner"
        assert row["setup"] == f"piceli secrets s3 {name} --prompt"
        assert "openstack ec2 credentials create" in str(row["hint"])
    assert write["build_host"] == "mini" and "build host mini" in str(write["where"])
    assert read["build_host"] is None
    assert "on host web-1" in str(read["where"])
    assert PICELI_CACHE_ENV in str(read["where"])
    alone = {s.name: s for s in needed_secrets(_owner_cache())}
    assert alone["cache-write"].where is None  # a cache alone names no host
    assert alone["cache-read"].where is None
    infra_key = {s.name: s for s in needed_secrets(
        _host(NixCache(PUBLIC, bucket=Bucket("nix-cache", provider=OS, keys=KEYS),
                       endpoint="s3.example.org")))}  # fmt: skip
    assert infra_key["cache-read"].created_by == "infra-apply"
    assert infra_key["cache-read"].hint is None


MODULE = """
from piceli.infra import Bucket, Infomaniak, Infrastructure, NixCache, NixHost, Tag
cloud = Infomaniak(credentials="cloud")
bucket = Bucket("nix-cache", provider=cloud)
cache = NixCache(PUBLIC, bucket=bucket, write_credentials="cache-write",
                 read_credentials="cache-read")
infra = Infrastructure("web", buckets=[bucket], state_dir=STATE)
host = NixHost("web-1", server="ssh://root@web-1.example.org", flake=".#web-1",
               follow=Tag("v*"), cache=cache)
"""


def test_secrets_status_lists_owner_pairs_pending_with_the_command(
    homes: Path,
) -> None:
    path = homes / "owner_pairs.py"
    path.write_text(f"PUBLIC = {PUBLIC!r}\nSTATE = {str(homes / 'state')!r}\n" + MODULE)
    result = CliRunner().invoke(app, ["secrets", "status", str(path), "--json"])
    body = json.loads(result.stdout)
    rows = {row["name"]: row for row in body["secrets"]}
    for name in ("cache-write", "cache-read"):
        row = rows[name]
        assert (row["state"], row["created_by"]) == ("pending", "owner")
        assert row["next"] == f"piceli secrets s3 {name} --prompt"
        assert "openstack ec2 credentials create" in row["detail"]
        assert "signed in as the project's user" in row["detail"]
    assert "on host web-1" in rows["cache-read"]["where"]
    lines = [line for line in result.stderr.splitlines() if "cache-" in line]
    assert any(
        line.split()[:3] == ["cache-read", "s3-keys", "pending"]
        and line.rstrip().endswith("piceli secrets s3 cache-read --prompt")
        for line in lines
    ), lines
    warned = {
        w["name"] for w in body["warnings"] if w["kind"] == "s3-read-not-enforced"
    }
    assert "cache-read" in warned
    for name in ("cache-write", "cache-read"):
        credentials.save_parts(
            name, "s3", {"access_key_id": f"AK{name}", "secret_access_key": "s" * 20}
        )
    stored = json.loads(
        CliRunner().invoke(app, ["secrets", "status", str(path), "--json"]).stdout
    )
    states = {row["name"]: row["state"] for row in stored["secrets"]}
    assert states["cache-write"] == states["cache-read"] == "present"


def test_setup_asks_for_owner_pairs_and_leaves_them_for_later(homes: Path) -> None:
    path = homes / "owner_pairs.py"
    path.write_text(f"PUBLIC = {PUBLIC!r}\nSTATE = {str(homes / 'state')!r}\n" + MODULE)
    result = CliRunner().invoke(
        app, ["secrets", "setup", str(path), "--json"], input=""
    )
    steps = {
        s["name"]: s
        for s in (json.loads(line) for line in result.stdout.splitlines())
        if "name" in s
    }
    assert steps["cache-read"]["state"] == "skipped"
    assert steps["cache-read"]["next"] == "piceli secrets s3 cache-read --prompt"


def test_host_secrets_names_the_command_of_a_missing_owner_pair(
    homes: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import piceli.k8s.cli.host as cli_host

    monkeypatch.setattr(cli_host, "load_host", lambda _ref: _host(_owner_cache()))
    result = CliRunner().invoke(app, ["host", "secrets", "hosts.py:web"])
    assert result.exit_code == 2, result.output
    body = json.loads(result.stdout)
    assert body["reason"] == "infra-credential-missing"
    assert "piceli secrets s3 cache-read --prompt" in body["message"]
    assert "openstack ec2 credentials create" in body["message"]
