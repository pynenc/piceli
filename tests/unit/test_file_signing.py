"""Release signing keys in the secret store and ``piceli sign-file`` (0.32.0).

A signature is what ``ssh-keygen -Y sign -n file`` writes, byte for byte
(Ed25519 is deterministic), and ``ssh-keygen -Y verify`` accepts it. The key
is a stored one or one an SSH agent holds; neither command prints it.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from typer.testing import CliRunner

from piceli.artifacts.file_signing import (
    SigningError,
    SigningKey,
    agent_sign,
    allowed_signers_line,
    generate_private_key,
    load_signing_key,
    private_key_of,
    public_key_line,
    save_signing_key,
    sign,
    verify,
)
from piceli.infra.machines import credentials
from piceli.infra.machines.secrets_status import report
from piceli.k8s.cli import app

SSH_KEYGEN = shutil.which("ssh-keygen")
needs_ssh_keygen = pytest.mark.skipif(SSH_KEYGEN is None, reason="needs ssh-keygen")


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("PICELI_CREDENTIALS_DIR", str(tmp_path / "credentials"))
    monkeypatch.setenv("PICELI_PROFILES_DIR", str(tmp_path / "profiles"))
    return tmp_path


def _run(*args: str) -> Any:
    return CliRunner().invoke(app, list(args))


def _private_body(name: str) -> str:
    stored = json.loads((credentials.credentials_dir() / f"{name}.json").read_text())
    return base64.b64decode(stored["value"]).decode()


def _write_key(path: Path, private: str) -> Path:
    path.write_text(private)
    path.chmod(0o600)
    return path


# ------------------------------------------------------------- the format


@needs_ssh_keygen
def test_the_signature_is_the_one_ssh_keygen_writes(tmp_path: Path) -> None:
    private = generate_private_key()
    key_file = _write_key(tmp_path / "id", private)
    data = tmp_path / "SHA256SUMS"
    data.write_bytes(b"0" * 64 + b"  a.tar.gz\n" * 3000)
    assert SSH_KEYGEN is not None
    subprocess.run(
        [SSH_KEYGEN, "-q", "-Y", "sign", "-f", str(key_file), "-n", "file", str(data)],
        check=True,
        capture_output=True,
    )
    expected = (tmp_path / "SHA256SUMS.sig").read_text()
    ours = sign(private_key_of(private), [data.read_bytes()])
    assert ours == expected
    assert verify(ours, [data.read_bytes()], public_key_line(private_key_of(private)))
    assert not verify(ours, [b"other"], public_key_line(private_key_of(private)))
    assert not verify(
        ours, [data.read_bytes()], public_key_line(private_key_of(private)), "git"
    )


def test_keys_of_other_kinds_or_with_a_passphrase_are_refused(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives.asymmetric import ec

    other = (
        ec.generate_private_key(ec.SECP256R1())
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        )
        .decode()
    )
    with pytest.raises(SigningError, match="not an ed25519 key"):
        private_key_of(other)
    with pytest.raises(SigningError, match="not a private key"):
        private_key_of("ssh-ed25519 AAAA")
    if SSH_KEYGEN is not None:
        path = tmp_path / "locked"
        subprocess.run(
            [SSH_KEYGEN, "-q", "-t", "ed25519", "-N", "a-passphrase", "-f", str(path)],
            check=True,
            capture_output=True,
        )
        with pytest.raises(SigningError, match="ssh-agent") as raised:
            private_key_of(path.read_text())
        assert raised.value.code == "infra-credential-invalid"


def test_allowed_signers_line_and_declaration_checks() -> None:
    public = public_key_line(private_key_of(generate_private_key()), "rel@example.org")
    line = allowed_signers_line(public, "rel@example.org")
    assert line.startswith('rel@example.org namespaces="file" ssh-ed25519 AAAA')
    assert not line.endswith("rel@example.org")
    assert SigningKey("release", public_key=line).public_key == line
    with pytest.raises(SigningError):
        SigningKey("release", public_key="ssh-rsa AAAA")
    with pytest.raises(SigningError):
        SigningKey("../escape")


# ------------------------------------------------------------- the store


@needs_ssh_keygen
def test_generate_sign_and_verify_with_ssh_keygen(store: Path) -> None:
    generated = _run(
        "secrets",
        "signing-key",
        "release",
        "--generate",
        "--comment",
        "rel@example.org",
    )
    assert generated.exit_code == 0, generated.output
    body = json.loads(generated.stdout)
    assert body["state"] == "stored" and body["kind"] == "signing-key"
    assert body["public_key"].endswith(" rel@example.org")
    private = _private_body("release")
    assert private not in generated.output
    assert "PRIVATE KEY" not in generated.output
    path = credentials.credentials_dir() / "release.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert credentials.credentials_dir().stat().st_mode & 0o777 == 0o700
    # Running it again only reads; replacing needs --replace.
    again = _run("secrets", "signing-key", "release")
    assert json.loads(again.stdout)["public_key"] == body["public_key"]
    refused = _run("secrets", "signing-key", "release", "--generate")
    assert refused.exit_code == 2
    assert json.loads(refused.stdout)["reason"] == "infra-credential-exists"

    files = [store / "SHA256SUMS", store / "a.tar.gz"]
    files[0].write_text("checksums\n")
    files[1].write_bytes(os.urandom(4096))
    signed = _run("sign-file", *map(str, files), "--key", "release")
    assert signed.exit_code == 0, signed.output
    result = json.loads(signed.stdout)
    assert result["source"] == "store" and result["namespace"] == "file"
    assert [item["signature"] for item in result["files"]] == [
        "SHA256SUMS.sig",
        "a.tar.gz.sig",
    ]
    assert private not in signed.output and "PRIVATE KEY" not in signed.output
    signers = store / "allowed_signers"
    signers.write_text(body["allowed_signers"] + "\n")
    assert SSH_KEYGEN is not None
    for item in files:
        with item.open("rb") as stdin:
            checked = subprocess.run(
                [SSH_KEYGEN, "-Y", "verify", "-f", str(signers), "-I",
                 "rel@example.org", "-n", "file", "-s", f"{item}.sig"],
                stdin=stdin, capture_output=True,
            )  # fmt: skip
        assert checked.returncode == 0, checked.stderr


@needs_ssh_keygen
def test_an_existing_ed25519_key_is_imported(store: Path) -> None:
    path = store / "id_release"
    assert SSH_KEYGEN is not None
    subprocess.run(
        [SSH_KEYGEN, "-q", "-t", "ed25519", "-N", "", "-C", "old", "-f", str(path)],
        check=True,
        capture_output=True,
    )
    imported = _run(
        "secrets", "signing-key", "release", "--file", str(path), "--comment", "rel"
    )
    assert imported.exit_code == 0, imported.output
    public = Path(f"{path}.pub").read_text().split()[:2]
    assert json.loads(imported.stdout)["public_key"].split()[:2] == public
    key, line = load_signing_key("release")
    assert line.split()[:2] == public and line.endswith(" rel")
    assert path.read_text() not in imported.output


def test_sign_file_refusals(store: Path) -> None:
    target = store / "file.txt"
    target.write_text("x")
    both = _run("sign-file", str(target), "--key", "a", "--agent", str(target))
    assert json.loads(both.stdout)["reason"] == "sign-file-invalid"
    missing = _run("sign-file", str(target), "--key", "absent")
    assert json.loads(missing.stdout)["reason"] == "infra-credential-missing"
    save_signing_key("release", generate_private_key())
    directory = _run("sign-file", str(store), "--key", "release")
    assert json.loads(directory.stdout)["reason"] == "sign-file-invalid"
    bad_space = _run("sign-file", str(target), "--key", "release", "--namespace", "a b")
    assert json.loads(bad_space.stdout)["reason"] == "sign-file-invalid"
    # A store file another user can read is refused, as for every kind.
    (credentials.credentials_dir() / "release.json").chmod(0o644)
    unsafe = _run("sign-file", str(target), "--key", "release")
    assert json.loads(unsafe.stdout)["reason"] == "infra-credential-unsafe"
    assert not (store / "file.txt.sig").exists()


# ------------------------------------------------------------- an agent


class FakeAgent:
    """An SSH agent holding one ed25519 key: answers sign requests (type 13)."""

    def __init__(self, directory: Path, private: str) -> None:
        self.key = private_key_of(private)
        self.path = directory / "agent.sock"
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.path))
        self.server.listen(1)
        self.requests = 0
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn:
                (length,) = struct.unpack(">I", conn.recv(4, socket.MSG_WAITALL))
                body = conn.recv(length, socket.MSG_WAITALL)
                self.requests += 1
                blob_len = struct.unpack(">I", body[1:5])[0]
                blob = body[5 : 5 + blob_len]
                offset = 5 + blob_len
                data_len = struct.unpack(">I", body[offset : offset + 4])[0]
                data = body[offset + 4 : offset + 4 + data_len]
                ours = base64.b64decode(public_key_line(self.key).split()[1])
                if body[0] != 13 or blob != ours:
                    answer = bytes([5])
                else:
                    raw = self.key.sign(data)
                    sig = (
                        struct.pack(">I", 11) + b"ssh-ed25519"
                        + struct.pack(">I", len(raw)) + raw
                    )  # fmt: skip
                    answer = bytes([14]) + struct.pack(">I", len(sig)) + sig
                conn.sendall(struct.pack(">I", len(answer)) + answer)

    def close(self) -> None:
        self.server.close()


@pytest.fixture
def short_dir() -> Iterator[Path]:
    """A short directory: a unix socket path has at most 104 bytes on macOS."""
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="pcl-") as name:
        yield Path(name)


def test_an_agent_signs_the_same_bytes_as_the_stored_key(
    store: Path, short_dir: Path
) -> None:
    private = generate_private_key()
    agent = FakeAgent(short_dir, private)
    try:
        public = public_key_line(private_key_of(private), "rel")
        pub_file = store / "release.pub"
        pub_file.write_text(public + "\n")
        target = store / "SHA256SUMS"
        target.write_text("sums\n")
        result = _run(
            "sign-file", str(target), "--agent", str(pub_file),
            "--agent-socket", str(agent.path),
        )  # fmt: skip
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["source"] == "agent"
        assert (store / "SHA256SUMS.sig").read_text() == sign(
            private_key_of(private), [b"sums\n"]
        )
        # A key the agent does not hold.
        other = public_key_line(private_key_of(generate_private_key()))
        with pytest.raises(SigningError) as raised:
            agent_sign(other, [b"x"], agent_socket=agent.path)
        assert raised.value.code == "signing-agent-failed"
    finally:
        agent.close()
    with pytest.raises(SigningError) as raised:
        agent_sign(public, [b"x"], agent_socket=short_dir / "absent.sock")
    assert raised.value.code == "signing-agent-failed"


# ------------------------------------------------------------- status and backup


def test_status_lists_a_declared_signing_key(store: Path) -> None:
    namespace = {"release_key": SigningKey("release")}
    rows = {r["name"]: r for r in report("keys.py", namespace)["secrets"]}
    assert rows["release"]["state"] == "missing"
    assert rows["release"]["created_by"] == "piceli"
    assert rows["release"]["next"] == "piceli secrets signing-key release --generate"
    public = save_signing_key("release", generate_private_key(), "rel")
    rows = {r["name"]: r for r in report("keys.py", namespace)["secrets"]}
    assert rows["release"]["state"] == "present"
    # Declared with another public key: damaged, and the next step prints it.
    other = public_key_line(private_key_of(generate_private_key()))
    namespace = {"release_key": SigningKey("release", public_key=other)}
    rows = {r["name"]: r for r in report("keys.py", namespace)["secrets"]}
    assert rows["release"]["state"] == "damaged"
    assert rows["release"]["next"] == "piceli secrets signing-key release"
    namespace = {"release_key": SigningKey("release", public_key=public)}
    rows = {r["name"]: r for r in report("keys.py", namespace)["secrets"]}
    assert rows["release"]["state"] == "present"
    assert _private_body("release") not in json.dumps(report("keys.py", namespace))


def test_setup_generates_a_declared_signing_key(store: Path) -> None:
    module = store / "keys.py"
    module.write_text(
        "from piceli.artifacts.file_signing import SigningKey\n"
        'release_key = SigningKey("release")\n'
    )
    result = _run("secrets", "setup", str(module), "--yes", "--json")
    assert result.exit_code == 0, result.output
    steps = [json.loads(line) for line in result.stdout.splitlines()]
    step = {s["name"]: s for s in steps if "name" in s}["release"]
    assert step["state"] == "generated"
    assert step["public_key"].startswith("ssh-ed25519 ")
    assert _private_body("release") not in result.output
    assert load_signing_key("release")[1].split()[:2] == step["public_key"].split()[:2]


def test_the_backup_lists_a_signing_key_by_kind(store: Path) -> None:
    from piceli.infra.machines.secrets_backup import store_entries

    save_signing_key("release", generate_private_key())
    found, _ = store_entries()
    kinds = {entry.name: entry.kind for entry, _ in found}
    assert kinds["release"] == "signing-key"
