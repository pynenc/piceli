"""``piceli secrets backup|restore``: the secret store, encrypted with age (0.25.0).

The store (every file under ``$PICELI_CREDENTIALS_DIR``, whatever its kind)
and the profiles go into one tar streamed into ``age``; nothing unencrypted
touches the disk. A restore into a non-empty store plans first and writes
only with ``--approve DIGEST``. No value is ever printed.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import stat
import subprocess
import tarfile
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from piceli.infra.machines import credentials
from piceli.infra.machines import secrets_backup as sb
from piceli.infra.machines.model import InfraError
from piceli.k8s.cli import app

VALUES = {
    "hcloud": ("provider-token", "hcloud-token-0123456789abcdefghijklmnop"),
    "edge-state": ("state-key", "state-passphrase-0123456789abcdef"),
    "cache-s3": ("s3-keys", "AKIDEXAMPLE:s3-secret-0123456789"),
    "web-1-boot": ("user-data", "#cloud-config\nbootstrap: secret-boot-key-42\n"),
}
KUBECONFIG = (
    "apiVersion: v1\nusers: [{name: u, user: {token: kube-token-0123456789}}]\n"
)
SECRETS = [value for _, value in VALUES.values()] + ["kube-token-0123456789"]

AGE = shutil.which("age")
needs_age = pytest.mark.skipif(
    not (AGE and shutil.which("age-keygen")), reason="age is not on PATH"
)


def _run(*args: str) -> Any:
    return CliRunner().invoke(app, ["secrets", *args])


def _no_value(*texts: str) -> None:
    for text in texts:
        for value in SECRETS:
            assert value not in text


def _fill(homes: Path) -> None:
    for name, (kind, value) in VALUES.items():
        credentials.save_credential(name, kind, value)
    store = credentials.credentials_dir()
    (store / "kubeconfigs").mkdir(mode=0o700)
    kubeconfig = store / "kubeconfigs" / "edge.yaml"
    kubeconfig.write_text(KUBECONFIG)
    kubeconfig.chmod(0o600)
    # A kind this version does not know: still backed up.
    future = store / "future.json"
    future.write_text(json.dumps({"schema": "x", "kind": "new-kind", "value": "v"}))
    future.chmod(0o600)
    profiles = homes / "profiles"
    profiles.mkdir(mode=0o700)
    (profiles / "edge.json").write_text(
        json.dumps(
            {
                "schema": "piceli.profile.v1",
                "kubeconfig": str(kubeconfig),
                "context": "edge",
            }
        )
    )


@pytest.fixture
def identity(tmp_path: Path) -> tuple[Path, str]:
    key = tmp_path / "keys" / "backup.key"
    key.parent.mkdir()
    subprocess.run(["age-keygen", "-o", str(key)], check=True, capture_output=True)
    public = subprocess.run(
        ["age-keygen", "-y", str(key)], check=True, capture_output=True, text=True
    ).stdout.strip()
    return key, public


@pytest.fixture
def second(tmp_path: Path) -> str:
    """A second recipient (the offline key): backups need two."""
    key = tmp_path / "keys2" / "offline.key"
    key.parent.mkdir()
    subprocess.run(["age-keygen", "-o", str(key)], check=True, capture_output=True)
    return subprocess.run(
        ["age-keygen", "-y", str(key)], check=True, capture_output=True, text=True
    ).stdout.strip()


def _encrypt(data: bytes, public: str, out: Path) -> Path:
    assert AGE
    subprocess.run([AGE, "-r", public, "-o", str(out)], input=data, check=True)
    return out


def _tar(*members: tarfile.TarInfo, manifest: bool = True, body: bytes = b"x") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        if manifest:
            data = json.dumps({"schema": sb.SCHEMA, "entries": []}).encode()
            info = tarfile.TarInfo(sb.MANIFEST)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for info in members:
            if info.isfile():
                info.size = len(body)
                tar.addfile(info, io.BytesIO(body))
            else:
                tar.addfile(info)
    return buffer.getvalue()


def _member(
    name: str, kind: bytes = tarfile.REGTYPE, link: str = ""
) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = link
    return info


# ------------------------------------------------------------- recipients


def test_recipients_are_age_ssh_or_plugin_keys(tmp_path: Path) -> None:
    listed = tmp_path / "recipients.txt"
    listed.write_text(
        "# the password manager's key\n"
        "age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p\n\n"
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample owner@laptop\n"
    )
    found = sb.parse_recipients(["age1yubikey1qwexample0yubikey0recipient"], listed)
    assert found == [
        "age1yubikey1qwexample0yubikey0recipient",
        "age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p",
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample",
    ]
    for bad in ("AGE-SECRET-KEY-1QQQ", "age1 space", "-o /etc/passwd", ""):
        with pytest.raises(InfraError) as error:
            sb.parse_recipients([bad], None)
        assert error.value.code == "secrets-backup-recipient-invalid"
        assert "AGE-SECRET" not in str(error.value)


def test_backup_refuses_without_age(
    homes: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fill(homes)
    monkeypatch.setenv("PICELI_AGE", str(homes / "no-such-age"))
    done = _run("backup", "--to", str(homes / "b.age"), "--recipient", "age1abc")
    assert done.exit_code == 2, done.stdout
    assert json.loads(done.stdout)["reason"] == "secrets-age-missing"
    assert "install" in done.stderr
    assert not (homes / "b.age").exists()


def test_backup_needs_recipients_and_a_store(homes: Path) -> None:
    if not AGE:
        pytest.skip("age is not on PATH")
    empty = _run("backup", "--to", str(homes / "b.age"), "--recipient", "age1abc")
    assert empty.exit_code == 2
    assert json.loads(empty.stdout)["reason"] == "secrets-backup-empty"
    _fill(homes)
    none = _run("backup", "--to", str(homes / "b.age"))
    assert none.exit_code == 2
    assert json.loads(none.stdout)["reason"] == "secrets-backup-recipients-missing"
    inside = _run(
        "backup", "--to", str(credentials.credentials_dir() / "b.age"),
        "--recipient", "age1abc", "--recipient", "age1def",
    )  # fmt: skip
    assert json.loads(inside.stdout)["reason"] == "secrets-backup-invalid"


# ------------------------------------------------------------- round trip


@needs_age
def test_backup_and_restore_round_trip(
    homes: Path,
    identity: tuple[Path, str],
    second: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key, public = identity
    _fill(homes)
    target = homes / "out" / "store.age"
    target.parent.mkdir()
    done = _run("backup", "--to", str(target), "--recipient", public,
                "--recipient", second)  # fmt: skip
    assert done.exit_code == 0, done.stdout + done.stderr
    _no_value(done.stdout, done.stderr)
    body = json.loads(done.stdout)
    assert body["state"] == "backed-up"
    assert body["recipients"] == [public, second]
    assert body["single_recipient"] is False
    assert body["count"] == 7
    listed = {(e["area"], e["name"], e["kind"]) for e in body["entries"]}
    assert listed == {
        ("credentials", "hcloud", "provider-token"),
        ("credentials", "edge-state", "state-key"),
        ("credentials", "cache-s3", "s3-keys"),
        ("credentials", "web-1-boot", "user-data"),
        ("credentials", "future", "new-kind"),
        ("credentials", "kubeconfigs/edge.yaml", "kubeconfig"),
        ("profiles", "edge", "profile"),
    }
    assert body["staleness"]["stale"] is False
    assert target.read_bytes().startswith(b"age-encryption.org/v1")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert body["sha256"].startswith("sha256:")
    assert [p.name for p in target.parent.iterdir()] == ["store.age"]
    # The marker and the remembered recipients: no values.
    config = json.loads(sb.config_path().read_text())
    assert config["recipients"] == [public, second]
    assert config["last_backup"]["sha256"] == body["sha256"]
    assert config["last_backup"]["count"] == 7
    _no_value(sb.config_path().read_text())

    # Never overwritten without --force; later backups need no flags.
    again = _run("backup", "--to", str(target))
    assert again.exit_code == 2
    assert json.loads(again.stdout)["reason"] == "secrets-backup-exists"
    forced = _run("backup", "--to", str(target), "--force")
    assert forced.exit_code == 0, forced.stdout

    # A new machine: empty homes.
    fresh = homes / "new-machine"
    monkeypatch.setenv("PICELI_CREDENTIALS_DIR", str(fresh / "credentials"))
    monkeypatch.setenv("PICELI_PROFILES_DIR", str(fresh / "profiles"))
    restored = _run("restore", str(target), "--identity", str(key))
    assert restored.exit_code == 0, restored.stdout + restored.stderr
    _no_value(restored.stdout, restored.stderr)
    out = json.loads(restored.stdout)
    assert out["state"] == "restored"
    assert len(out["added"]) == 7 and out["replaced"] == []
    for name, (kind, value) in VALUES.items():
        assert credentials.load_credential(name, kind) == value
    store = fresh / "credentials"
    assert (store / "kubeconfigs" / "edge.yaml").read_text() == KUBECONFIG
    for path in [store, *store.rglob("*"), fresh / "profiles"]:
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == (0o700 if path.is_dir() else 0o600), path
    # The profile follows the store to its new place.
    profile = json.loads((fresh / "profiles" / "edge.json").read_text())
    assert profile["kubeconfig"] == str(store / "kubeconfigs" / "edge.yaml")
    # The backup config came along: the next backup needs no flags.
    assert json.loads(sb.config_path().read_text())["recipients"] == [public, second]
    assert sb.staleness()["stale"] is False


@needs_age
def test_restore_into_a_used_store_plans_then_needs_the_digest(
    homes: Path, identity: tuple[Path, str], second: str
) -> None:
    key, public = identity
    _fill(homes)
    target = homes / "store.age"
    done = _run("backup", "--to", str(target), "--recipient", public,
                "--recipient", second)  # fmt: skip
    assert done.exit_code == 0
    # Change one, delete one: the store is not empty.
    credentials.save_credential("hcloud", "provider-token", "a-newer-token-0123456789")
    (credentials.credentials_dir() / "edge-state.json").unlink()

    plan = _run("restore", str(target), "--identity", str(key))
    assert plan.exit_code == 3, plan.stdout + plan.stderr
    _no_value(plan.stdout, plan.stderr)
    body = json.loads(plan.stdout)
    assert body["state"] == "approval-required"
    assert [e["name"] for e in body["add"]] == ["edge-state"]
    assert [e["name"] for e in body["differ"]] == ["hcloud"]
    assert len(body["identical"]) == 5
    assert body["digest"].startswith("sha256:")
    assert f"--approve {body['digest']}" in body["approve_command"]
    assert credentials.load_credential("hcloud", "provider-token").startswith("a-newer")
    assert not (credentials.credentials_dir() / "edge-state.json").exists()

    wrong = _run("restore", str(target), "--identity", str(key), "--approve",
                 "sha256:" + "0" * 64)  # fmt: skip
    assert wrong.exit_code == 2
    assert json.loads(wrong.stdout)["reason"] == "secrets-restore-plan-changed"

    done = _run("restore", str(target), "--identity", str(key), "--approve",
                body["digest"])  # fmt: skip
    assert done.exit_code == 0, done.stdout + done.stderr
    out = json.loads(done.stdout)
    assert [e["name"] for e in out["added"]] == ["edge-state"]
    assert [e["name"] for e in out["replaced"]] == ["hcloud"]
    assert (
        credentials.load_credential("hcloud", "provider-token") == VALUES["hcloud"][1]
    )


@needs_age
def test_wrong_identity_fails_and_writes_nothing(
    homes: Path,
    identity: tuple[Path, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, public = identity
    _fill(homes)
    target = homes / "store.age"
    done = _run("backup", "--to", str(target), "--recipient", public,
                "--single-recipient")  # fmt: skip
    assert done.exit_code == 0
    other = tmp_path / "other.key"
    subprocess.run(["age-keygen", "-o", str(other)], check=True, capture_output=True)
    monkeypatch.setenv("PICELI_CREDENTIALS_DIR", str(tmp_path / "fresh"))
    done = _run("restore", str(target), "--identity", str(other))
    assert done.exit_code == 1
    assert json.loads(done.stdout)["reason"] == "secrets-restore-decrypt-failed"
    assert not (tmp_path / "fresh").exists()


# ------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "member",
    [
        _member("/etc/passwd"),
        _member("credentials/../../escape.json"),
        _member("credentials/link", tarfile.SYMTYPE, "/etc/passwd"),
        _member("credentials/inner", tarfile.SYMTYPE, "hcloud.json"),
        _member("credentials/hard", tarfile.LNKTYPE, "credentials/hcloud.json"),
        _member("credentials/fifo", tarfile.FIFOTYPE),
        _member("elsewhere/file.json"),
        _member("profiles/nested/edge.json"),
        _member("credentials\\..\\x"),
    ],
    ids=[
        "absolute",
        "dotdot",
        "symlink-out",
        "symlink-in",
        "hardlink",
        "fifo",
        "unknown-top",
        "nested-profile",
        "backslash",
    ],
)
def test_crafted_archives_are_refused(member: tarfile.TarInfo) -> None:
    with pytest.raises(InfraError) as error:
        sb.parse_archive(_tar(member))
    assert error.value.code == "secrets-restore-archive-invalid"


def test_an_archive_without_manifest_or_twice_a_name_is_refused() -> None:
    for data in (
        _tar(_member("credentials/a.json"), manifest=False),
        _tar(_member("credentials/a.json"), _member("credentials/a.json")),
        b"not a tar at all",
    ):
        with pytest.raises(InfraError) as error:
            sb.parse_archive(data)
        assert error.value.code == "secrets-restore-archive-invalid"


@needs_age
def test_a_crafted_archive_restores_nothing(
    homes: Path, identity: tuple[Path, str]
) -> None:
    key, public = identity
    bad = _encrypt(
        _tar(_member("credentials/ok.json"), _member("credentials/../../x.json")),
        public,
        homes / "bad.age",
    )
    done = _run("restore", str(bad), "--identity", str(key))
    assert done.exit_code == 2
    assert json.loads(done.stdout)["reason"] == "secrets-restore-archive-invalid"
    assert not credentials.credentials_dir().exists()
    assert not (homes.parent / "x.json").exists()


def test_restore_of_a_missing_file_is_refused(homes: Path) -> None:
    done = _run("restore", str(homes / "missing.age"))
    assert done.exit_code == 2
    assert json.loads(done.stdout)["reason"] == "secrets-restore-archive-invalid"


# ------------------------------------------------------------- staleness


def test_staleness_names_the_secrets_newer_than_the_backup(homes: Path) -> None:
    assert sb.staleness() == {
        "last_backup": None, "newest_secret": None, "stale": False, "newer": [],
        "single_recipient": False,
    }  # fmt: skip
    credentials.save_credential("hcloud", "provider-token", VALUES["hcloud"][1])
    never = sb.staleness()
    assert never["stale"] is True and never["newer"] == ["hcloud"]
    sb.record_backup(
        recipients=["age1abc"], sha256="sha256:" + "a" * 64,
        names=["hcloud"], at=time.time() + 5,
    )  # fmt: skip
    fresh = sb.staleness()
    assert fresh["stale"] is False and fresh["newer"] == []
    assert fresh["last_backup"] and fresh["newest_secret"]
    path = credentials.save_credential(
        "edge-state", "state-key", VALUES["edge-state"][1]
    )
    later = time.time() + 60
    os.utime(path, (later, later))
    stale = sb.staleness()
    assert stale["stale"] is True and stale["newer"] == ["edge-state"]
    _no_value(json.dumps(stale), sb.config_path().read_text())


# ------------------------------------------------------------- two recipients


def test_one_recipient_is_refused_without_the_flag(
    homes: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    _fill(homes)
    # Refused before age runs: any file stands in for it.
    monkeypatch.setenv("PICELI_AGE", sys.executable)
    done = _run("backup", "--to", str(homes / "b.age"), "--recipient", "age1abc")
    assert done.exit_code == 2, done.stdout
    body = json.loads(done.stdout)
    assert body["reason"] == "secrets-backup-single-recipient"
    assert "loses the store and its backup together" in body["message"]
    assert "--single-recipient" in body["message"]
    assert "backup-key" in body["message"]
    assert not (homes / "b.age").exists()
    assert not sb.config_path().exists()


@needs_age
def test_a_single_recipient_backup_is_recorded(
    homes: Path, identity: tuple[Path, str], second: str
) -> None:
    key, public = identity
    _fill(homes)
    target = homes / "store.age"
    done = _run("backup", "--to", str(target), "--recipient", public,
                "--single-recipient")  # fmt: skip
    assert done.exit_code == 0, done.stdout + done.stderr
    assert json.loads(done.stdout)["single_recipient"] is True
    assert "single recipient" in done.stderr
    config = json.loads(sb.config_path().read_text())
    assert config["last_backup"]["single_recipient"] is True
    assert sb.staleness()["single_recipient"] is True
    data, _ = sb.decrypt(target, key)
    manifest, _ = sb.parse_archive(data)
    assert manifest["single_recipient"] is True
    # The remembered single recipient still needs the flag each time.
    again = _run("backup", "--to", str(target), "--force")
    assert json.loads(again.stdout)["reason"] == "secrets-backup-single-recipient"
    two = _run("backup", "--to", str(target), "--force", "--recipient", public,
               "--recipient", second)  # fmt: skip
    assert two.exit_code == 0, two.stdout
    assert sb.staleness()["single_recipient"] is False


@needs_age
def test_backup_key_writes_the_identity_and_prints_only_the_recipient(
    homes: Path, tmp_path: Path, identity: tuple[Path, str]
) -> None:
    _, public = identity
    out = tmp_path / "usb" / "piceli-offline.key"
    out.parent.mkdir()
    done = _run("backup-key", "--out", str(out))
    assert done.exit_code == 0, done.stdout + done.stderr
    body = json.loads(done.stdout)
    derived = subprocess.run(
        ["age-keygen", "-y", str(out)], check=True, capture_output=True, text=True
    ).stdout.strip()
    assert body["state"] == "created" and body["recipient"] == derived
    assert body["path"] == str(out) and body["added"] is False
    assert derived.startswith("age1")
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    secret = next(line for line in out.read_text().splitlines()
                  if line.startswith("AGE-SECRET-KEY-"))  # fmt: skip
    assert secret not in done.stdout + done.stderr
    assert "AGE-SECRET-KEY" not in done.stdout + done.stderr
    assert derived in done.stderr
    assert "only copy" in done.stderr and "offline" in done.stderr
    # Never replaced without --force.
    again = _run("backup-key", "--out", str(out))
    assert json.loads(again.stdout)["reason"] == "secrets-backup-key-exists"
    assert secret in out.read_text()
    # --add appends it to the remembered recipients, once.
    sb.remember_recipients([public])
    added = _run("backup-key", "--out", str(out), "--force", "--add")
    assert added.exit_code == 0, added.stdout
    new = json.loads(added.stdout)["recipient"]
    assert new != derived
    assert sb.load_config()["recipients"] == [public, new]
    assert json.loads(added.stdout)["recipients"] == [public, new]


def test_backup_key_refuses_the_store_and_a_missing_directory(
    homes: Path, tmp_path: Path
) -> None:
    credentials.save_credential("hcloud", "provider-token", VALUES["hcloud"][1])
    inside = _run("backup-key", "--out", str(credentials.credentials_dir() / "k"))
    assert json.loads(inside.stdout)["reason"] == "secrets-backup-key-invalid"
    nowhere = _run("backup-key", "--out", str(tmp_path / "no" / "dir" / "k"))
    assert json.loads(nowhere.stdout)["reason"] == "secrets-backup-key-invalid"
    assert not (credentials.credentials_dir() / "k").exists()


def test_backup_key_needs_age_keygen(
    homes: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PICELI_AGE_KEYGEN", str(tmp_path / "no-age-keygen"))
    done = _run("backup-key", "--out", str(tmp_path / "k"))
    assert done.exit_code == 2
    assert json.loads(done.stdout)["reason"] == "secrets-age-missing"
    assert not (tmp_path / "k").exists()
