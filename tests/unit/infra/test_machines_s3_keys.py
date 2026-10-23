"""S3 key pairs ``piceli infra apply`` creates (0.25.0).

``Bucket(keys=[S3Key(...)])`` renders one resource per pair; the plan lists
them (never a value), the apply stores each as its local credential
(``s3-keys``, ``0600``) and never prints it, a re-apply changes nothing, and
removing a key deletes the pair and its stored credential. The OpenStack
rendering, its known refusal and its honest "read-only not enforced" are
checked without a cloud.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest

from piceli.infra import Bucket, Infrastructure, NixCache, NixHost, OpenStack, Tag
from piceli.infra.machines import credentials
from piceli.infra.machines.keys import needed_secrets, require_encrypted
from piceli.infra.machines.model import InfraError, S3Key
from piceli.infra.machines.plan import apply, plan, progress, workspace
from piceli.infra.machines.render import render
from piceli.infra.machines.state import encryption_env
from piceli.infra.machines.tofu import TofuResult
from piceli.testing.infra import FakeProvider

from .machines_support import PASSPHRASE, last_json, needs_tofu

PUBLIC = "cache-1:" + "A" * 43 + "="
OS = OpenStack(
    credentials="cloud", auth_url="https://identity.example.net/v3",
    region="region-1", public_network="public", flavors=["small"],
)  # fmt: skip
KEYS = [S3Key("cache-write"), S3Key("cache-read", access="read")]


def _infra(tmp_path: Path, keys: list[S3Key], generation: int = 0) -> Infrastructure:
    bucket = Bucket(
        "nix-cache", provider=FakeProvider(generation=generation), keys=keys
    )
    return Infrastructure("web", buckets=[bucket], state_dir=tmp_path / "state")


def _stored(name: str) -> dict[str, Any]:
    path = credentials.credentials_dir() / f"{name}.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    return dict(json.loads(path.read_text()))


def test_the_declaration_is_checked() -> None:
    with pytest.raises(InfraError, match="credential name"):
        S3Key("Not A Name")
    with pytest.raises(InfraError, match="write or read"):
        S3Key("cache-read", access="admin")  # type: ignore[arg-type]
    with pytest.raises(InfraError, match="same name"):
        Bucket("nix-cache", provider=OS, keys=[S3Key("a-key"), S3Key("a-key", "read")])
    with pytest.raises(InfraError, match="state key or a provider credential"):
        Infrastructure(
            "web", buckets=[Bucket("b-1", provider=OS, keys=[S3Key("cloud")])]
        )
    two = [Bucket("b-1", provider=OS, keys=[S3Key("k-1")]),
           Bucket("b-2", provider=OS, keys=[S3Key("k-1")])]  # fmt: skip
    with pytest.raises(InfraError, match="same name"):
        Infrastructure("web", buckets=two)
    bucket = Bucket("nix-cache", provider=OS, keys=KEYS)
    assert bucket.describe()["keys"] == [
        {"name": "cache-write", "access": "write"},
        {"name": "cache-read", "access": "read"},
    ]
    assert "keys" not in Bucket("nix-cache", provider=OS).describe()


def test_openstack_renders_one_ec2_credential_per_key_and_never_its_secret() -> None:
    infra = Infrastructure("web", buckets=[Bucket("nix-cache", provider=OS, keys=KEYS)])
    rendered = render(infra)
    ec2 = rendered.config["resource"]["openstack_identity_ec2_credential_v3"]
    assert ec2 == {
        "s3key-cache-write": {
            "depends_on": ["openstack_objectstorage_container_v1.bucket-nix-cache"]
        },
        "s3key-cache-read": {
            "depends_on": ["openstack_objectstorage_container_v1.bucket-nix-cache"]
        },
    }
    assert rendered.s3_keys == {
        "openstack_identity_ec2_credential_v3.s3key-cache-write": (
            "cache-write", "nix-cache", "write"),
        "openstack_identity_ec2_credential_v3.s3key-cache-read": (
            "cache-read", "nix-cache", "read"),
    }  # fmt: skip
    assert OS.ledger_owned("openstack_identity_ec2_credential_v3")
    assert OS.hides_id("openstack_identity_ec2_credential_v3")
    read = OS.s3_key_enforcement(KEYS[1])
    assert read["read_only_enforced"] is False and "every container" in read["how"]
    assert OS.s3_key_values({"access": "AK", "secret": "SK"}) == ("AK", "SK")
    assert OS.s3_key_values({"access": "AK"}) is None


def test_openstack_names_keystones_refusal() -> None:
    line = (
        "Error creating openstack_identity_ec2_credential_v3: Expected HTTP response "
        "code [201] when accessing [POST https://identity.example.net/v3/users/u/"
        "credentials/OS-EC2], but got 403 instead"
    )
    error = OS.known_error([line])
    assert error is not None and error.code == "infra-s3-key-refused"
    assert OS.known_error(["Error creating openstack_compute_instance_v2: 403"]) is None


def test_keys_need_an_encrypted_enforced_state() -> None:
    require_encrypted(encryption_env(PASSPHRASE))
    for env in ({}, {"TF_ENCRYPTION": encryption_env(PASSPHRASE)["TF_ENCRYPTION"]
                     .replace("enforced = true", "enforced = false")}):  # fmt: skip
        with pytest.raises(InfraError) as caught:
            require_encrypted(env)
        assert caught.value.code == "infra-state-unencrypted"


def test_a_partial_apply_never_reports_an_access_key_id() -> None:
    address = "openstack_identity_ec2_credential_v3.s3key-cache-read"
    line = json.dumps(
        {
            "type": "apply_complete",
            "hook": {
                "resource": {"addr": address},
                "action": "create",
                "id_value": "AKIDSECRET",
            },
        }
    )
    found = progress(TofuResult(1, line, ""), (), hidden=lambda a: a == address)
    assert found["applied"] == [{"address": address, "action": "create"}]


def test_needed_secrets_lists_who_creates_each() -> None:
    bucket = Bucket("nix-cache", provider=OS, keys=KEYS)
    infra = Infrastructure("web", buckets=[bucket])
    cache = NixCache(PUBLIC, bucket=bucket, endpoint="s3.example.org")
    assert cache.write_credentials == "cache-write"
    assert cache.read_credentials == "cache-read"
    host = NixHost("web-1", server="ssh://root@web-1.example.org", flake=".#web-1",
                   follow=Tag("v*"), cache=cache, age_identity="web-1-age")  # fmt: skip
    found = {s.name: s.to_dict() for s in needed_secrets(infra, host)}
    assert found["cache-write"]["created_by"] == "infra-apply"
    assert found["cache-read"]["kind"] == "s3-keys"
    assert found["cache-read"]["created_by"] == "infra-apply"
    assert (
        found["web-state"]["setup"] == "piceli secrets state-key web-state --generate"
    )
    assert found["cloud"]["kind"] == "provider-token"
    assert found["cache-1"]["kind"] == "nix-signing-key"
    assert found["web-1-age"]["created_by"] == "operator"
    own = NixCache(PUBLIC, bucket=Bucket("nix-cache", provider=OS), endpoint="s3.example.org",
                   write_credentials="my-keys")  # fmt: skip
    mine = {s.name: s for s in needed_secrets(own)}
    assert mine["my-keys"].created_by == "operator"
    assert mine["my-keys"].setup == "piceli secrets s3 my-keys --prompt"
    with pytest.raises(TypeError):
        needed_secrets("not a declaration")


@needs_tofu
def test_apply_stores_the_pairs_reapply_is_a_noop_removal_deletes(
    homes: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from typer.testing import CliRunner

    from piceli.k8s.cli import app

    credentials.save_credential("web-state", "state-key", PASSPHRASE)
    infra = _infra(homes, KEYS)
    (homes / "keys_infra.py").write_text(
        "from pathlib import Path\n"
        "from piceli.infra import Bucket, Infrastructure, S3Key\n"
        "from piceli.testing.infra import FakeProvider\n"
        "bucket = Bucket('nix-cache', provider=FakeProvider(), keys=[\n"
        "    S3Key('cache-write'), S3Key('cache-read', access='read')])\n"
        f"infra = Infrastructure('web', buckets=[bucket], state_dir=Path({str(homes / 'state')!r}))\n"
    )
    ref = f"{homes / 'keys_infra.py'}:infra"
    planned = CliRunner().invoke(app, ["infra", "plan", ref])
    assert planned.exit_code == 3, planned.output
    first = last_json(planned.stdout)
    keys = {k["name"]: k for k in first["s3_keys"]}
    assert keys["cache-read"] == {
        "name": "cache-read", "bucket": "nix-cache", "access": "read",
        "action": "create", "credential": "cache-read", "stored": False,
        "read_only_enforced": True, "how": "fake",
    }  # fmt: skip
    assert "s3 key cache-read: read on bucket nix-cache, create" in planned.stderr
    done = CliRunner().invoke(
        app, ["infra", "apply", ref, "--approve", first["plan_hash"]]
    )
    assert done.exit_code == 0, done.output
    write, read = _stored("cache-write"), _stored("cache-read")
    assert write["kind"] == read["kind"] == "s3-keys"
    access, _, secret = read["value"].partition(":")
    assert access.startswith("FAKE") and secret.startswith("fake-secret-")
    assert write["value"] != read["value"]
    printed = capsys.readouterr()
    for output in (planned.output, done.output, printed.out + printed.err):
        assert secret not in output
    record = json.loads((homes / "state" / "s3-keys.json").read_text())
    assert sorted(item["name"] for item in record.values()) == [
        "cache-read", "cache-write"]  # fmt: skip
    assert secret not in json.dumps(record)
    # The same declaration: nothing to change, the stored pairs kept.
    with workspace(infra) as ws:
        again = plan(ws)
        assert again.changes == ()
        assert {k["action"] for k in again.s3_keys} == {"keep"}
        assert secret not in json.dumps(again.to_dict())
    assert _stored("cache-read") == read
    # A pair the store lost is stored again from the state by the next plan.
    credentials.delete_credential("cache-read", "s3-keys")
    with workspace(infra) as ws:
        assert plan(ws).changes == ()
    assert _stored("cache-read") == read
    # A new pair (the provider's rotation): stored over the old one.
    with workspace(_infra(homes, KEYS, generation=1)) as ws:
        rotated = plan(ws)
        assert {k["action"] for k in rotated.s3_keys} == {"update"}  # fake: new input
        apply(ws, rotated.plan_hash)
    assert _stored("cache-read")["value"] != read["value"]
    # Removing the read key deletes the pair and its credential.
    smaller = _infra(homes, KEYS[:1], generation=1)
    with workspace(smaller) as ws:
        removing = plan(ws)
        gone = [k for k in removing.s3_keys if k["name"] == "cache-read"]
        assert gone and gone[0]["action"] == "delete"
        apply(ws, removing.plan_hash)
    assert not credentials.credential_present("cache-read")
    assert credentials.credential_present("cache-write")
    # Destroy: the remaining pair goes with the bucket.
    with workspace(smaller) as ws:
        doomed = plan(ws, destroy=True)
        apply(ws, doomed.plan_hash, destroy=True)
    assert not credentials.credential_present("cache-write")


@needs_tofu
def test_a_credential_piceli_did_not_store_is_never_replaced(homes: Path) -> None:
    credentials.save_credential("web-state", "state-key", PASSPHRASE)
    credentials.save_credential("cache-read", "s3-keys", "MINE:typed-by-hand")
    with workspace(_infra(homes, KEYS)) as ws, pytest.raises(InfraError) as caught:
        plan(ws)
    assert caught.value.code == "infra-credential-exists"
    assert credentials.load_credential("cache-read", "s3-keys") == "MINE:typed-by-hand"
