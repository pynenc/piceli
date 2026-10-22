"""age X25519 identities: a host's bootstrap secret for sops-nix (0.24.0).

``piceli secrets age NAME --generate`` keeps an identity in Piceli's local
credentials and prints its recipient (``age1...``) for ``.sops.yaml``;
``piceli host secrets`` puts it on the host as sops-nix's
``sops.age.keyFile``. The encoding is age's: Bech32 (BIP 173, without its
90-character limit) of the 32-byte X25519 scalar under the upper-case
``AGE-SECRET-KEY-`` prefix, and of the public key under ``age``.

Importing this module is side-effect free.
"""

from __future__ import annotations

__all__ = [
    "bech32_decode",
    "bech32_encode",
    "check_identity",
    "generate_identity",
    "recipient",
]

_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_GENERATOR = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
SECRET_PREFIX = "AGE-SECRET-KEY-"
PUBLIC_PREFIX = "age"


class AgeError(ValueError):
    """Not an age identity (the code is ``infra-credential-invalid``)."""

    code = "infra-credential-invalid"


def _polymod(values: list[int]) -> int:
    check = 1
    for value in values:
        top = check >> 25
        check = (check & 0x1FFFFFF) << 5 ^ value
        for i in range(5):
            if (top >> i) & 1:
                check ^= _GENERATOR[i]
    return check


def _expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _convert(data: bytes | list[int], source: int, target: int, pad: bool) -> list[int]:
    acc, bits, out = 0, 0, []
    for value in data:
        acc = (acc << source) | value
        bits += source
        while bits >= target:
            bits -= target
            out.append((acc >> bits) & ((1 << target) - 1))
    if pad and bits:
        out.append((acc << (target - bits)) & ((1 << target) - 1))
    elif not pad and (bits >= source or (acc << (target - bits)) & ((1 << target) - 1)):
        raise AgeError("bech32: bad padding")
    return out


def bech32_encode(hrp: str, data: bytes) -> str:
    """Lower-case Bech32 of ``data`` (8-bit bytes) under ``hrp``."""
    hrp = hrp.lower()
    words = _convert(data, 8, 5, True)
    check = _polymod(_expand(hrp) + words + [0] * 6) ^ 1
    words += [(check >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_CHARSET[w] for w in words)


def bech32_decode(text: str) -> tuple[str, bytes]:
    """``(hrp, bytes)`` of a Bech32 string (one case only, checksum checked)."""
    if text.lower() != text and text.upper() != text:
        raise AgeError("bech32: mixed case")
    lowered = text.lower()
    hrp, sep, rest = lowered.rpartition("1")
    if not sep or not hrp or len(rest) < 6 or any(c not in _CHARSET for c in rest):
        raise AgeError("bech32: malformed")
    if any(not 33 <= ord(c) <= 126 for c in hrp):
        raise AgeError("bech32: malformed")
    words = [_CHARSET.index(c) for c in rest]
    if _polymod(_expand(hrp) + words) != 1:
        raise AgeError("bech32: bad checksum")
    return hrp, bytes(_convert(words[:-6], 5, 8, False))


def generate_identity() -> str:
    """A new identity, ``AGE-SECRET-KEY-1...`` (never printed by Piceli)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    raw = X25519PrivateKey.generate().private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    return bech32_encode(SECRET_PREFIX, raw).upper()


def _scalar(identity: str) -> bytes:
    try:
        hrp, raw = bech32_decode(identity.strip())
    except AgeError:
        raise AgeError("not an age identity (AGE-SECRET-KEY-1...)") from None
    if (
        hrp != SECRET_PREFIX.lower()
        or len(raw) != 32
        or identity.strip() != identity.strip().upper()
    ):
        raise AgeError("not an age identity (AGE-SECRET-KEY-1...)")
    return raw


def check_identity(identity: str) -> str:
    """The identity, normalised (upper case, stripped); ``AgeError`` otherwise."""
    _scalar(identity)
    return identity.strip()


def recipient(identity: str) -> str:
    """The identity's public key, ``age1...`` (what ``.sops.yaml`` lists)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    public = (
        X25519PrivateKey.from_private_bytes(_scalar(identity))
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    return bech32_encode(PUBLIC_PREFIX, public)
