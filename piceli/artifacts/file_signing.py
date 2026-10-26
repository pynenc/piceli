"""Signing release files with an ssh-ed25519 key, as ``ssh-keygen -Y sign`` does (0.32.0).

A signature is the OpenSSH ``SSHSIG`` format (``PROTOCOL.sshsig`` in
OpenSSH): the file's SHA-512 signed under a *namespace* (``file`` by
default), armored between ``-----BEGIN SSH SIGNATURE-----`` lines. Anyone
verifies it with OpenSSH alone::

    ssh-keygen -Y verify -f allowed_signers -I release@example.org -n file \\
        -s SHA256SUMS.sig < SHA256SUMS

Ed25519 signatures are deterministic, so the same key and file give the
same bytes ``ssh-keygen -Y sign`` writes.

The key lives in one of two places:

- **The local secret store** (kind ``signing-key``,
  ``piceli secrets signing-key NAME --generate`` or ``--file KEY``): an
  OpenSSH private key without a passphrase, in an owner-only file (``0600``
  in a ``0700`` directory) like every other stored key, covered by
  ``piceli secrets backup``. It is read only to sign and never printed.
- **An SSH agent** (``ssh-add``): Piceli sends the agent the data to sign
  and gets the signature back; the key, also one with a passphrase, never
  leaves the agent.

:class:`SigningKey` declares a stored key in a module, so ``piceli secrets
status`` and ``setup`` list it. Importing this module runs nothing.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import socket
import struct
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

__all__ = [
    "KIND",
    "SigningError",
    "SigningKey",
    "agent_sign",
    "allowed_signers_line",
    "generate_private_key",
    "load_signing_key",
    "parse_public_key",
    "private_key_of",
    "public_key_line",
    "save_signing_key",
    "sign",
    "sign_file",
    "verify",
]

KIND = "signing-key"
NAMESPACE = "file"
_MAGIC = b"SSHSIG"
_VERSION = 1
_HASH = b"sha512"
_ED25519 = b"ssh-ed25519"
_BEGIN = "-----BEGIN SSH SIGNATURE-----"
_END = "-----END SSH SIGNATURE-----"
#: A namespace or principal: what ``ssh-keygen`` accepts, without spaces.
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9@._+-]{0,127}")
_MAX_KEY = 16384
_BLOCK = 1024 * 1024
#: SSH agent protocol (draft-miller-ssh-agent): sign request and answers.
_AGENTC_SIGN_REQUEST = 13
_AGENT_SIGN_RESPONSE = 14
_AGENT_FAILURE = 5


class SigningError(ValueError):
    """A signing input or step failed. ``code`` is a registered error code."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class SigningKey:
    """A release signing key a module declares (kept in the local secret store).

    ``name`` is the credential's name; ``public_key`` (``ssh-ed25519 AAAA...``),
    when given, is the key verifiers trust: ``piceli secrets status`` reports
    a stored key with another public key as damaged. ``purpose`` is shown
    there. Nothing is read when it is created.
    """

    name: str
    public_key: str | None = None
    purpose: str = "signs release files (piceli sign-file)"

    def __post_init__(self) -> None:
        from piceli.profiles import ProfileError, check_name

        try:
            check_name(self.name)
        except ProfileError:
            raise SigningError(
                "infra-credential-invalid", "a key name is like a profile name"
            ) from None
        if self.public_key is not None:
            parse_public_key(self.public_key)


# ------------------------------------------------------------- wire format


def _string(value: bytes) -> bytes:
    return struct.pack(">I", len(value)) + value


def _read_string(data: bytes, offset: int) -> tuple[bytes, int]:
    if offset + 4 > len(data):
        raise SigningError("sign-file-invalid", "truncated signature")
    (length,) = struct.unpack(">I", data[offset : offset + 4])
    end = offset + 4 + length
    if end > len(data):
        raise SigningError("sign-file-invalid", "truncated signature")
    return data[offset + 4 : end], end


def _public_blob(raw: bytes) -> bytes:
    return _string(_ED25519) + _string(raw)


def parse_public_key(line: str) -> bytes:
    """The 32 raw bytes of an ``ssh-ed25519 AAAA... [comment]`` line.

    Also takes an ``allowed_signers`` line (principals and options first).
    """
    parts = line.strip().split()
    if _ED25519.decode() in parts:
        parts = parts[parts.index(_ED25519.decode()) :]
    try:
        if len(parts) < 2 or parts[0] != "ssh-ed25519":
            raise ValueError
        blob = base64.b64decode(parts[1], validate=True)
        kind, offset = _read_string(blob, 0)
        raw, end = _read_string(blob, offset)
        if kind != _ED25519 or len(raw) != 32 or end != len(blob):
            raise ValueError
    except (ValueError, SigningError):
        raise SigningError(
            "sign-key-invalid", "not an ssh-ed25519 public key"
        ) from None
    return raw


def _signed_data(namespace: str, message_hash: bytes) -> bytes:
    return (
        _MAGIC
        + _string(namespace.encode())
        + _string(b"")
        + _string(_HASH)
        + _string(message_hash)
    )


def _armor(public_raw: bytes, namespace: str, signature: bytes) -> str:
    blob = (
        _MAGIC
        + struct.pack(">I", _VERSION)
        + _string(_public_blob(public_raw))
        + _string(namespace.encode())
        + _string(b"")
        + _string(_HASH)
        + _string(signature)
    )
    text = base64.b64encode(blob).decode()
    lines = [text[i : i + 70] for i in range(0, len(text), 70)]
    return "\n".join([_BEGIN, *lines, _END]) + "\n"


def _check_namespace(namespace: str) -> str:
    if not isinstance(namespace, str) or not _WORD.fullmatch(namespace):
        raise SigningError("sign-file-invalid", "invalid namespace")
    return namespace


def _hash_stream(chunks: Iterable[bytes]) -> bytes:
    hasher = hashlib.sha512()
    for chunk in chunks:
        hasher.update(chunk)
    return hasher.digest()


def _file_chunks(path: Path) -> Iterable[bytes]:
    try:
        with path.open("rb") as stream:
            while block := stream.read(_BLOCK):
                yield block
    except OSError:
        raise SigningError("sign-file-invalid", "a file is unreadable") from None


# ------------------------------------------------------------- keys


def generate_private_key() -> str:
    """A new ed25519 key as an OpenSSH private key (no passphrase)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    return (
        Ed25519PrivateKey.generate()
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        )
        .decode()
    )


def private_key_of(text: str) -> Ed25519PrivateKey:
    """Parse an OpenSSH (or PKCS#8) ed25519 private key without a passphrase."""
    from cryptography.exceptions import UnsupportedAlgorithm
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    data = text.encode() if isinstance(text, str) else b""
    if not data or len(data) > _MAX_KEY or b"PRIVATE KEY-----" not in data:
        raise SigningError("infra-credential-invalid", "not a private key")
    try:
        if b"OPENSSH PRIVATE KEY" in data:
            key: Any = serialization.load_ssh_private_key(data, password=None)
        else:
            key = serialization.load_pem_private_key(data, password=None)
    except TypeError:
        raise SigningError(
            "infra-credential-invalid",
            "the key has a passphrase: load it into ssh-agent (ssh-add) and "
            "sign with --agent, or store a key without one",
        ) from None
    except (ValueError, UnsupportedAlgorithm):
        raise SigningError("infra-credential-invalid", "not a private key") from None
    if not isinstance(key, Ed25519PrivateKey):
        raise SigningError("infra-credential-invalid", "not an ed25519 key")
    return key


def _raw_public(key: Ed25519PrivateKey) -> bytes:
    from cryptography.hazmat.primitives import serialization

    return key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )


def public_key_line(key: Ed25519PrivateKey, comment: str | None = None) -> str:
    """``ssh-ed25519 AAAA... [comment]`` of ``key``."""
    text = "ssh-ed25519 " + base64.b64encode(_public_blob(_raw_public(key))).decode()
    return f"{text} {comment}" if comment else text


def allowed_signers_line(
    public_key: str, principal: str, namespace: str = NAMESPACE
) -> str:
    """The ``allowed_signers`` line ``ssh-keygen -Y verify -I PRINCIPAL`` reads."""
    if not _WORD.fullmatch(principal):
        raise SigningError("sign-file-invalid", "invalid principal")
    _check_namespace(namespace)
    parse_public_key(public_key)
    key = " ".join(public_key.split()[:2])
    return f'{principal} namespaces="{namespace}" {key}'


def save_signing_key(name: str, private: str, comment: str | None = None) -> str:
    """Store ``private`` as credential ``name`` (kind ``signing-key``); its public key."""
    from piceli.infra.machines.credentials import _write

    key = private_key_of(private)
    public = public_key_line(key, comment)
    del key
    _write(
        name,
        {
            "schema": "piceli.credential.v1",
            "kind": KIND,
            "value": base64.b64encode(private.encode()).decode(),
            "public_key": public,
        },
    )
    return public


def load_signing_key(name: str) -> tuple[Ed25519PrivateKey, str]:
    """The stored key ``name`` and its public key line.

    :raises InfraError: missing, unsafe or another kind (registered codes).
    """
    import json

    from piceli.infra.machines.credentials import _path, load_credential

    value = load_credential(name, KIND)
    try:
        private = base64.b64decode(value, validate=True).decode()
    except (ValueError, UnicodeDecodeError):
        raise SigningError("infra-credential-invalid", "not a signing key") from None
    key = private_key_of(private)
    del private, value
    try:
        comment = json.loads(_path(name).read_text()).get("public_key", "")
        parts = str(comment).split(maxsplit=2)
        label = parts[2] if len(parts) == 3 else None
    except (OSError, ValueError, AttributeError):
        label = None
    return key, public_key_line(key, label)


# ------------------------------------------------------------- signing


def sign(
    key: Ed25519PrivateKey, chunks: Iterable[bytes], namespace: str = NAMESPACE
) -> str:
    """The armored SSHSIG signature of the data in ``chunks``."""
    _check_namespace(namespace)
    data = _signed_data(namespace, _hash_stream(chunks))
    raw = key.sign(data)
    signature = _string(_ED25519) + _string(raw)
    return _armor(_raw_public(key), namespace, signature)


def _agent_request(sock: socket.socket, body: bytes) -> bytes:
    sock.sendall(struct.pack(">I", len(body)) + body)
    head = _recv(sock, 4)
    (length,) = struct.unpack(">I", head)
    if not 0 < length <= 262144:
        raise SigningError("signing-agent-failed")
    return _recv(sock, length)


def _recv(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise SigningError("signing-agent-failed")
        data += chunk
    return data


def agent_sign(
    public_key: str,
    chunks: Iterable[bytes],
    namespace: str = NAMESPACE,
    *,
    agent_socket: Path | None = None,
    timeout: float = 60.0,
) -> str:
    """Sign with the key of ``public_key`` held by an SSH agent.

    ``agent_socket`` defaults to ``$SSH_AUTH_SOCK``. The agent may ask its
    owner to confirm. The signature is checked against ``public_key``.
    """
    _check_namespace(namespace)
    raw = parse_public_key(public_key)
    path = agent_socket or (
        Path(os.environ["SSH_AUTH_SOCK"]) if os.environ.get("SSH_AUTH_SOCK") else None
    )
    if path is None:
        raise SigningError("signing-agent-failed", "no SSH agent (SSH_AUTH_SOCK)")
    data = _signed_data(namespace, _hash_stream(chunks))
    request = (
        bytes([_AGENTC_SIGN_REQUEST])
        + _string(_public_blob(raw))
        + _string(data)
        + struct.pack(">I", 0)
    )
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(path))
            answer = _agent_request(sock, request)
    except OSError:
        raise SigningError(
            "signing-agent-failed", "the SSH agent is unreachable"
        ) from None
    if not answer or answer[0] != _AGENT_SIGN_RESPONSE:
        raise SigningError(
            "signing-agent-failed", "the SSH agent does not hold that key or refused"
        )
    signature, end = _read_string(answer, 1)
    kind, offset = _read_string(signature, 0)
    value, _ = _read_string(signature, offset)
    if kind != _ED25519 or not _verify_raw(raw, data, value):
        raise SigningError("signing-agent-failed", "the agent's signature is invalid")
    return _armor(raw, namespace, signature)


def _verify_raw(raw: bytes, data: bytes, value: bytes) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        Ed25519PublicKey.from_public_bytes(raw).verify(value, data)
    except (InvalidSignature, ValueError):
        return False
    return True


def verify(
    armored: str, chunks: Iterable[bytes], public_key: str, namespace: str = NAMESPACE
) -> bool:
    """Whether ``armored`` is ``public_key``'s signature of the data, in ``namespace``."""
    text = armored.strip()
    if not text.startswith(_BEGIN) or not text.endswith(_END):
        return False
    try:
        blob = base64.b64decode("".join(text[len(_BEGIN) : -len(_END)].split()))
        if blob[:6] != _MAGIC or struct.unpack(">I", blob[6:10])[0] != _VERSION:
            return False
        offset = 10
        key_blob, offset = _read_string(blob, offset)
        space, offset = _read_string(blob, offset)
        _, offset = _read_string(blob, offset)
        algorithm, offset = _read_string(blob, offset)
        signature, offset = _read_string(blob, offset)
        kind, inner = _read_string(signature, 0)
        value, _ = _read_string(signature, inner)
    except (ValueError, SigningError, struct.error):
        return False
    raw = parse_public_key(public_key)
    if key_blob != _public_blob(raw) or kind != _ED25519 or algorithm != _HASH:
        return False
    if space != namespace.encode():
        return False
    return _verify_raw(raw, _signed_data(namespace, _hash_stream(chunks)), value)


def sign_file(
    path: Path,
    *,
    key: Ed25519PrivateKey | None = None,
    agent_key: str | None = None,
    agent_socket: Path | None = None,
    namespace: str = NAMESPACE,
) -> dict[str, Any]:
    """Write ``PATH.sig`` (atomically, replacing one); what was signed.

    Exactly one of ``key`` (a stored key) or ``agent_key`` (the public key of
    a key an SSH agent holds).
    """
    if (key is None) == (agent_key is None):
        raise SigningError("sign-file-invalid", "sign with a stored key or an agent")
    path = Path(path).absolute()
    if path.is_symlink() or not path.is_file() or path.name.startswith("."):
        raise SigningError("sign-file-invalid", "every input is a regular file")
    sha = hashlib.sha256()

    def chunks() -> Iterable[bytes]:
        for block in _file_chunks(path):
            sha.update(block)
            yield block

    if key is not None:
        armored = sign(key, chunks(), namespace)
    else:
        assert agent_key is not None
        armored = agent_sign(agent_key, chunks(), namespace, agent_socket=agent_socket)
    target = path.with_name(path.name + ".sig")
    partial = path.with_name(f".{target.name}.{os.getpid()}.partial")
    try:
        descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(armored)
        os.replace(partial, target)
    except OSError:
        raise SigningError("sign-file-invalid", "cannot write the signature") from None
    finally:
        partial.unlink(missing_ok=True)
    return {
        "file": path.name,
        "sha256": "sha256:" + sha.hexdigest(),
        "signature": target.name,
    }
