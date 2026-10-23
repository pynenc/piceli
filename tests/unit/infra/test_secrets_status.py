"""``piceli secrets status`` and ``setup`` (0.25.0).

Status lists every secret a module's declarations need, with its state, who
creates it, the next command, what it is for and where else it must live,
then the backup and warnings; it never prints a value. Setup walks the
missing ones in order, generates what Piceli can (printing only public
halves), asks for the rest part by part and never replaces a stored key
without a confirmation.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.dev.nix import generate_key, public_key
from piceli.hosts.age import generate_identity
from piceli.infra import (
    Bucket,
    BuildHost,
    DevBuilds,
    NixCache,
    NixHost,
    OpenStack,
    S3Key,
    Tag,
)
from piceli.infra.machines import credentials
from piceli.infra.machines.keys import needed_secrets
from piceli.infra.machines.secrets_status import report
from piceli.infra.nix_releases import NixRelease
from piceli.k8s.cli import app

API_KEY = "pk1_" + "0a" * 32
SECRET_KEY = "sk1_" + "1b" * 32
CRED_ID = "0123456789abcdef0123456789abcdef"
CRED_SECRET = "app-credential-secret-" + "s" * 40
PUBLIC = "example-cache-1:jXbGkEMPpvXtlVqGBrqfsnU8Jyn1cZB9u0QhGhvsKP0="

DECLARATION = """
from pathlib import Path
from piceli.infra import (
    Bucket, BuildHost, DevBuilds, DevProfile, Image, Infrastructure, NixCache,
    NixHost, OpenStack, Porkbun, DnsRecord, S3Key, Server, Ssh, Tag, UserData,
)

# A generic OpenStack cloud: the Infomaniak preset creates no S3 keys (0.25.3).
cloud = OpenStack(
    credentials="infomaniak", auth_url="https://identity.example.org/v3",
    region="region-1", public_network="public", flavors=["a1-ram2-disk20-perf1"],
    prices={"a1-ram2-disk20-perf1": 4.70},
)
dns = Porkbun(credentials="porkbun")
web = Server(
    "web-1", provider=cloud, type="a1-ram2-disk20-perf1",
    image=Image("base", url="https://images.example.org/base.qcow2", sha256="0" * 64),
    user_data=UserData("web-1-boot"),
    ssh=Ssh(user="root", address="web-1.example.org"),
)
nix_bucket = Bucket("example-nix-cache", provider=cloud, keys=[
    S3Key("nix-cache-write"), S3Key("nix-cache-read", access="read")])
cache = NixCache(PUBLIC, bucket=nix_bucket, endpoint="s3.example.org")
infra = Infrastructure(
    "web-1", servers=[web], buckets=[nix_bucket],
    records=[DnsRecord("example.org", "@", "A", server=web, provider=dns)],
    state_dir=STATE,
)
host = NixHost("web-1", server=web, flake=".#web-1", follow=Tag("v*"),
               cache=cache, age_identity="web-1-age")
builds = DevBuilds(
    node=None, image="ghcr.io/example/builder@sha256:" + "0" * 64,
    hosts=[BuildHost.docker("nix-1", context="ssh://builder@nix-1.example.org")],
    profiles=[DevProfile.for_nix(timeout="2h")],
)
"""
VALUES = (API_KEY, SECRET_KEY, CRED_ID, CRED_SECRET)


def _module(homes: Path, public: str = PUBLIC) -> str:
    path = homes / "web_1.py"
    path.write_text(
        f"PUBLIC = {public!r}\nSTATE = {str(homes / 'infra-state')!r}\n" + DECLARATION
    )
    return str(path)


def _run(*args: str, input: str | None = None) -> Any:
    return CliRunner().invoke(app, ["secrets", *args], input=input)


def _rows(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["name"]: row for row in body["secrets"]}


def _lines(stdout: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stdout.splitlines()]


def _store_all(homes: Path) -> str:
    """Every secret stored and complete; the module's key matches."""
    key = generate_key("example-cache-1")
    credentials.save_credential("example-cache-1", "nix-signing-key", key)
    ref = _module(homes, public_key(key))
    credentials.save_parts(
        "infomaniak", "openstack",
        {"application_credential_id": CRED_ID,
         "application_credential_secret": CRED_SECRET},
    )  # fmt: skip
    credentials.save_parts(
        "porkbun", "porkbun", {"api_key": API_KEY, "secret_api_key": SECRET_KEY}
    )
    credentials.save_credential("web-1-state", "state-key", "p" * 32)
    credentials.save_credential("web-1-age", "age-identity", generate_identity())
    credentials.save_credential("web-1-boot", "user-data", "tskey-auth-example")
    for name in ("nix-cache-write", "nix-cache-read"):
        credentials.save_parts(
            name, "s3", {"access_key_id": f"AK{name}", "secret_access_key": "s" * 20}
        )
    return ref


# ------------------------------------------------------------- status


def test_status_of_an_empty_store_lists_everything_in_setup_order(
    homes: Path,
) -> None:
    ref = _module(homes)
    result = _run("status", ref, "--json")
    assert result.exit_code == 1, result.output
    body = json.loads(result.stdout)
    assert body["schema"] == "piceli.secrets-status.v1"
    assert [(r["name"], r["kind"], r["state"], r["created_by"]) for r in body["secrets"]] == [
        ("infomaniak", "provider-token", "missing", "owner"),
        ("porkbun", "provider-token", "missing", "owner"),
        ("example-cache-1", "nix-signing-key", "missing", "piceli"),
        ("web-1-state", "state-key", "missing", "piceli"),
        ("web-1-age", "age-identity", "missing", "piceli"),
        ("web-1-boot", "user-data", "missing", "owner"),
        ("nix-cache-write", "s3-keys", "pending", "infra-apply"),
        ("nix-cache-read", "s3-keys", "pending", "infra-apply"),
    ]  # fmt: skip
    rows = _rows(body)
    assert rows["infomaniak"]["next"] == ("piceli secrets provider infomaniak --prompt")
    assert rows["infomaniak"]["provider"] == "openstack"
    assert rows["example-cache-1"]["next"] == (
        "piceli secrets nix-cache example-cache-1 --generate"
    )
    assert rows["web-1-boot"]["next"] == "piceli secrets user-data web-1-boot --prompt"
    assert "right before" in rows["web-1-boot"]["where"]
    assert rows["nix-cache-read"]["next"] == f"piceli infra plan {ref}:infra"
    assert "/var/lib/piceli-host/cache.env" in rows["nix-cache-read"]["where"]
    assert ".sops.yaml" in rows["web-1-age"]["where"]
    assert body["counts"]["missing"] == 6 and body["counts"]["pending"] == 2
    assert body["complete"] is False
    assert body["backup"]["state"] == "empty" and body["backup"]["next"] is None
    assert [w["kind"] for w in body["warnings"]] == ["s3-read-not-enforced"]
    assert [d["kind"] for d in body["declarations"]] == [
        "Bucket", "NixCache", "Infrastructure", "NixHost", "DevBuilds"]  # fmt: skip
    head = result.stderr.splitlines()[1].split()
    assert head == ["NAME", "KIND", "STATE", "BY", "NEXT"]
    assert "warning: S3 key nix-cache-read (bucket example-nix-cache) is not" in (
        result.stderr
    )
    assert f"piceli secrets setup {ref} walks through them" in result.stderr


def test_status_names_incomplete_damaged_and_mismatched_secrets(homes: Path) -> None:
    ref = _module(homes)
    # An older single value for a two-part provider.
    credentials.save_credential("porkbun", "provider-token", SECRET_KEY)
    credentials.save_credential("web-1-state", "state-key", "p" * 32)
    # A signing key that is not the declared one.
    credentials.save_credential(
        "example-cache-1", "nix-signing-key", generate_key("example-cache-1")
    )
    # An age identity file someone else can read, and an unreadable one.
    path = credentials.save_credential("web-1-age", "age-identity", generate_identity())
    os.chmod(path, 0o644)
    bad = credentials.credentials_dir() / "infomaniak.json"
    bad.write_text("{not json")
    os.chmod(bad, 0o600)
    result = _run("status", ref, "--json")
    assert result.exit_code == 1, result.output
    for value in VALUES:
        assert value not in result.stdout + result.stderr
    rows = _rows(json.loads(result.stdout))
    assert rows["porkbun"]["state"] == "incomplete"
    assert "one part of two stored" in rows["porkbun"]["detail"]
    assert rows["porkbun"]["next"] == "piceli secrets provider porkbun --prompt"
    assert rows["web-1-state"]["state"] == "present"
    assert rows["web-1-state"]["next"] is None
    assert rows["example-cache-1"]["state"] == "damaged"
    assert "NixCache(public_key=)" in rows["example-cache-1"]["detail"]
    assert rows["example-cache-1"]["next"] == (
        "piceli secrets nix-cache example-cache-1"
    )
    assert rows["web-1-age"]["state"] == "damaged"
    assert "chmod 600" in rows["web-1-age"]["detail"]
    assert rows["infomaniak"]["state"] == "damaged"
    assert rows["infomaniak"]["detail"] == "unreadable"


def test_status_exits_0_when_every_secret_is_stored(homes: Path) -> None:
    ref = _store_all(homes)
    result = _run("status", ref, "--json")
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["complete"] is True and body["counts"]["present"] == 8
    assert body["backup"]["state"] == "none"
    assert body["backup"]["next"] == "piceli secrets backup-key --out PATH --add"
    assert "user-data-spent" in [w["kind"] for w in body["warnings"]]
    assert "every secret is stored" in result.stderr
    assert "tskey-auth-example" not in result.stdout + result.stderr


def test_the_user_data_of_a_created_server_is_not_needed(homes: Path) -> None:
    ref = _module(homes)
    state = homes / "infra-state"
    state.mkdir(mode=0o700)
    (state / "inventory.json").write_text(
        json.dumps({"servers": {"web-1": {"id": "srv-1", "status": "ACTIVE"}}})
    )
    rows = _rows(json.loads(_run("status", ref, "--json").stdout))
    assert rows["web-1-boot"]["state"] == "not-needed"
    assert rows["web-1-boot"]["next"] is None


def test_an_attribute_selects_one_declaration(homes: Path) -> None:
    ref = _module(homes)
    body = json.loads(_run("status", f"{ref}:host", "--json").stdout)
    assert body["attr"] == "host"
    assert sorted(_rows(body)) == [
        "example-cache-1", "nix-cache-read", "nix-cache-write", "web-1-age"]  # fmt: skip
    # The module's Infrastructure still names the pairs' command.
    assert _rows(body)["nix-cache-read"]["next"] == f"piceli infra plan {ref}:infra"


def test_status_refuses_a_module_without_declarations(homes: Path) -> None:
    empty = homes / "empty.py"
    empty.write_text("value = 1\n")
    result = _run("status", str(empty))
    assert result.exit_code == 2
    assert json.loads(result.stdout)["reason"] == "secrets-nothing-declared"
    missing = _run("status", f"{_module(homes)}:nothing")
    assert missing.exit_code == 2
    assert json.loads(missing.stdout)["reason"] == "infra-not-found"


def test_a_composition_cluster_needs_its_login_profile(homes: Path) -> None:
    module = homes / "infra_comp.py"
    module.write_text(
        "from piceli.infra import Cluster\n"
        "home = Cluster('home', api='https://k8s.example.org:6443', "
        "credentials='home-admin')\n"
    )
    body = json.loads(_run("status", str(module), "--json").stdout)
    row = _rows(body)["home-admin"]
    assert row["kind"] == "profile" and row["state"] == "missing"
    assert row["next"] == "piceli login home-admin --kubeconfig FILE --context NAME"
    assert row["created_by"] == "owner"


def test_needed_secrets_of_a_release_name_its_build_host() -> None:
    cloud = OpenStack(
        credentials="cloud", auth_url="https://identity.example.net/v3",
        region="region-1", public_network="public", flavors=["small"],
    )  # fmt: skip
    bucket = Bucket("nix-cache", provider=cloud, keys=[S3Key("cache-write")])
    cache = NixCache(PUBLIC, bucket=bucket, endpoint="s3.example.org")
    host = NixHost("web-1", server="ssh://root@web-1.example.org", flake=".#web-1",
                   follow=Tag("v*"), cache=cache)  # fmt: skip
    from piceli.infra import Source

    release = NixRelease(
        host=host, source=Source("https://git.example.org/hosts.git"),
        builder="mini",
    )  # fmt: skip
    builds = DevBuilds(
        node=None, image="ghcr.io/example/builder@sha256:" + "0" * 64,
        hosts=[BuildHost.docker("mini")],
    )  # fmt: skip
    found = {s.name: s for s in needed_secrets(release, builds, host)}
    assert sorted(found) == ["cache-write", "example-cache-1"]
    assert "build host mini" in str(found["example-cache-1"].where)
    assert "build host mini" in str(found["cache-write"].where)
    assert found["cache-write"].created_by == "infra-apply"


def test_report_never_holds_a_value(homes: Path) -> None:
    ref = _store_all(homes)
    namespace: dict[str, Any] = {}
    exec(compile(Path(ref).read_text(), ref, "exec"), namespace)
    body = json.dumps(report("web_1.py", namespace))
    for value in (*VALUES, "tskey-auth-example", "p" * 32, "AGE-SECRET-KEY"):
        assert value not in body


# ------------------------------------------------------------- setup


def test_setup_generates_with_yes_and_reads_piped_parts(homes: Path) -> None:
    ref = _module(homes)
    piped = f"{CRED_ID}\n{CRED_SECRET}\n{API_KEY}\n{SECRET_KEY}\n"
    result = _run("setup", ref, "--yes", "--json", input=piped)
    assert result.exit_code == 0, result.output
    for value in VALUES:
        assert value not in result.stdout + result.stderr
    steps = _lines(result.stdout)
    by_name = {s["name"]: s for s in steps if "name" in s}
    assert by_name["infomaniak"]["state"] == "stored"
    assert by_name["porkbun"]["parts"] == ["api_key", "secret_api_key"]
    assert by_name["web-1-state"]["state"] == "generated"
    assert by_name["example-cache-1"]["public_key"].startswith("example-cache-1:")
    assert by_name["web-1-age"]["public_key"].startswith("age1")
    assert by_name["web-1-boot"]["state"] == "later"
    assert by_name["nix-cache-read"]["state"] == "later"
    assert steps[-2]["group"] == "backup" and steps[-2]["state"] == "later"
    assert steps[-1]["state"] == "done" and steps[-1]["remaining"] == []
    assert steps[-1]["copy"] == [
        {"name": "example-cache-1", "kind": "nix-signing-key",
         "public_key": by_name["example-cache-1"]["public_key"],
         "into": "NixCache(public_key=...)"},
        {"name": "web-1-age", "kind": "age-identity",
         "public_key": by_name["web-1-age"]["public_key"], "into": ".sops.yaml"},
    ]  # fmt: skip
    assert {r["name"] for r in steps[-1]["later"]} == {
        "web-1-boot", "nix-cache-write", "nix-cache-read"}  # fmt: skip
    assert [s["step"] for s in steps[:-1]] == list(range(1, len(steps)))
    stored = json.loads((credentials.credentials_dir() / "porkbun.json").read_text())
    assert stored["parts"] == {"api_key": API_KEY, "secret_api_key": SECRET_KEY}
    assert "put it in NixCache(public_key=...)" in result.stderr
    assert "add it to .sops.yaml" in result.stderr
    # The generated signing key is not the declared one until it is copied.
    status = json.loads(_run("status", ref, "--json").stdout)
    row = _rows(status)["example-cache-1"]
    assert row["state"] == "damaged"
    assert row["next"] == "piceli secrets nix-cache example-cache-1"
    # Setup again: it prints the stored key's public key, never replaces it.
    again = _run("setup", ref, "--yes", "--json", input="")
    assert again.exit_code == 0, again.output
    step = {s["name"]: s for s in _lines(again.stdout) if "name" in s}[
        "example-cache-1"
    ]
    assert step["state"] == "copy"
    assert step["public_key"] == by_name["example-cache-1"]["public_key"]


def test_setup_without_yes_or_input_generates_nothing(homes: Path) -> None:
    ref = _module(homes)
    result = _run("setup", ref, "--json", input="")
    assert result.exit_code == 1, result.output
    steps = {s["name"]: s for s in _lines(result.stdout) if "name" in s}
    assert steps["infomaniak"]["state"] == "skipped"
    assert steps["infomaniak"]["why"] == "no-input"
    assert steps["porkbun"]["why"] == "earlier-refusal"
    assert steps["web-1-state"]["state"] == "needs-yes"
    assert (
        steps["web-1-state"]["next"]
        == "piceli secrets state-key web-1-state --generate"
    )
    assert not credentials.credential_present("web-1-state")
    summary = _lines(result.stdout)[-1]
    assert summary["state"] == "incomplete"
    assert {r["name"] for r in summary["remaining"]} == {
        "infomaniak", "porkbun", "example-cache-1", "web-1-state", "web-1-age"}  # fmt: skip


def test_setup_stops_reading_after_a_refused_part(homes: Path) -> None:
    ref = _module(homes)
    result = _run("setup", ref, "--json", input=f"{CRED_ID}:{CRED_SECRET}\n")
    assert result.exit_code == 1
    steps = {s["name"]: s for s in _lines(result.stdout) if "name" in s}
    assert steps["infomaniak"]["state"] == "failed"
    assert steps["infomaniak"]["reason"] == "infra-credential-part-invalid"
    assert steps["infomaniak"]["part"] == "application_credential_id"
    assert steps["porkbun"]["state"] == "skipped"
    assert CRED_SECRET not in result.stdout + result.stderr
    assert not credentials.credential_present("infomaniak")


def test_setup_never_replaces_a_damaged_key_without_a_confirmation(
    homes: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref = _store_all(homes)
    other = generate_identity()
    path = credentials.save_credential("web-1-age", "age-identity", other)
    path.write_text('{"kind": "age-identity", "value": "not-an-identity"}')
    os.chmod(path, 0o600)
    result = _run("setup", ref, "--yes", "--json", input="")
    assert result.exit_code == 1
    steps = {s["name"]: s for s in _lines(result.stdout) if "name" in s}
    assert steps["web-1-age"]["state"] == "kept"
    assert (
        steps["web-1-age"]["next"]
        == "piceli secrets age web-1-age --generate --replace"
    )
    assert "not-an-identity" in path.read_text()
    # On a terminal, a typed "y" replaces it.
    from piceli.k8s.cli import secrets_status as cli

    monkeypatch.setattr(cli, "_interactive", lambda: True)
    # replace? y; then the backup offers: offline key path (skip), own
    # recipient (skip).
    again = _run("setup", ref, "--json", input="y\n\n\n")
    assert again.exit_code == 0, again.output
    steps = {s["name"]: s for s in _lines(again.stdout) if "name" in s}
    assert steps["web-1-age"]["state"] == "generated"
    assert "not-an-identity" not in path.read_text()


def test_setup_on_a_terminal_asks_before_generating_and_offers_user_data(
    homes: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from piceli.k8s.cli import secrets_status as cli

    ref = _module(homes)
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    answers = [
        CRED_ID, CRED_SECRET,  # infomaniak (piped here; on a terminal: Step 1 of 2...)
        API_KEY, SECRET_KEY,  # porkbun
        "y",  # generate the signing key
        "n",  # not the state key
        "y",  # the age identity
        "y",  # store the user data now
        "tskey-auth-example",  # the user data
        "",  # no offline backup key
        "",  # no own recipient
    ]  # fmt: skip
    result = _run("setup", ref, "--json", input="\n".join(answers) + "\n")
    assert result.exit_code == 1, result.output
    steps = {s["name"]: s for s in _lines(result.stdout) if "name" in s}
    assert steps["infomaniak"]["state"] == steps["porkbun"]["state"] == "stored"
    assert steps["example-cache-1"]["state"] == "generated"
    assert steps["web-1-state"]["state"] == "skipped"
    assert steps["web-1-boot"]["state"] == "stored"
    assert steps["web-1-boot"]["digest"].startswith("sha256:")
    assert "tskey-auth-example" not in result.stdout + result.stderr
    summary = _lines(result.stdout)[-1]
    assert [r["name"] for r in summary["remaining"]] == ["web-1-state"]
    assert [c["name"] for c in summary["copy"]] == ["example-cache-1", "web-1-age"]


# ------------------------------------------------------------- pending pairs


def _applied(homes: Path, *names: str) -> None:
    """The infrastructure's record of the pairs an apply created."""
    state = homes / "infra-state"
    state.mkdir(mode=0o700, exist_ok=True)
    (state / "s3-keys.json").write_text(json.dumps({
        f"openstack_identity_ec2_credential_v3.s3key-{name}": {
            "name": name, "bucket": "example-nix-cache", "access": "read",
            "digest": "sha256:0"} for name in names}))  # fmt: skip


def test_pairs_before_the_first_apply_are_pending(homes: Path) -> None:
    ref = _store_all(homes)
    for name in ("nix-cache-write", "nix-cache-read"):
        credentials.delete_credential(name)
    result = _run("status", ref, "--json")
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    rows = _rows(body)
    assert rows["nix-cache-read"]["state"] == "pending"
    assert rows["nix-cache-read"]["next"] == f"piceli infra plan {ref}:infra"
    assert rows["nix-cache-read"]["detail"] == "created by the next infra apply"
    assert body["counts"]["pending"] == 2 and body["complete"] is True
    assert "created by the next infra apply" in result.stderr
    # Applied, but the credential is gone: missing, and a plan stores it again.
    _applied(homes, "nix-cache-read")
    again = _run("status", ref, "--json")
    assert again.exit_code == 1
    row = _rows(json.loads(again.stdout))["nix-cache-read"]
    assert row["state"] == "missing"
    assert row["next"] == f"piceli infra plan {ref}:infra"
    assert "stores it again from the state" in row["detail"]
    assert _rows(json.loads(again.stdout))["nix-cache-write"]["state"] == "pending"


def test_setup_leaves_pending_pairs_to_infra_apply(homes: Path) -> None:
    ref = _store_all(homes)
    for name in ("nix-cache-write", "nix-cache-read"):
        credentials.delete_credential(name)
    result = _run("setup", ref, "--json", input="")
    assert result.exit_code == 0, result.output
    steps = {s["name"]: s for s in _lines(result.stdout) if "name" in s}
    assert steps["nix-cache-read"]["state"] == "later"


# ------------------------------------------------------------- build host


RELEASE = """
from piceli.infra import NixRelease, Source
release = NixRelease(host, source=Source("https://git.example.org/hosts.git"),
                     builder="mini")
"""


def test_a_release_builder_gets_the_transfer_as_its_next_step(homes: Path) -> None:
    ref = _store_all(homes)
    Path(ref).write_text(Path(ref).read_text() + RELEASE)
    body = json.loads(_run("status", ref, "--json").stdout)
    rows = _rows(body)
    command = (
        "piceli secrets backup --only example-cache-1 --only nix-cache-write "
        "--to PATH --recipient AGE_RECIPIENT_OF_mini"
    )
    for name in ("example-cache-1", "nix-cache-write"):
        assert rows[name]["state"] == "present"
        assert rows[name]["build_host"] == "mini"
        assert rows[name]["next"] == command
        assert (
            "on mini: piceli secrets restore PATH --identity KEY"
            in rows[name]["detail"]
        )
    assert rows["nix-cache-read"]["build_host"] is None
    assert rows["nix-cache-read"]["next"] is None
    assert body["complete"] is True
    # Missing locally: first make it here.
    credentials.delete_credential("example-cache-1")
    row = _rows(json.loads(_run("status", ref, "--json").stdout))["example-cache-1"]
    assert row["next"] == "piceli secrets nix-cache example-cache-1 --generate"
