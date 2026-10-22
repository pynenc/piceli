"""age identities: Bech32 and X25519 against published vectors (0.24.0)."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from piceli.hosts import age

#: RFC 7748, section 6.1: Alice's private key and its public key.
ALICE = bytes.fromhex(
    "77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a"
)
ALICE_PUBLIC = "8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a"


def test_bech32_reads_the_bip173_vectors_and_refuses_a_changed_one() -> None:
    assert age.bech32_decode("A12UEL5L") == ("a", b"")
    hrp, data = age.bech32_decode("abcdef1qpzry9x8gf2tvdw0s3jn54khce6mua7lmqqqxw")
    assert hrp == "abcdef" and len(data) == 20
    assert age.bech32_encode("abcdef", data) == (
        "abcdef1qpzry9x8gf2tvdw0s3jn54khce6mua7lmqqqxw"
    )
    # age's own README recipient has a valid checksum.
    assert (
        age.bech32_decode(
            "age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p"
        )[0]
        == "age"
    )
    for bad in ("abcdef1qpzry9x8gf2tvdw0s3jn54khce6mua7lmqqqxx", "A12UeL5L", "x1"):
        with pytest.raises(age.AgeError):
            age.bech32_decode(bad)


def test_an_identity_and_its_recipient_follow_age() -> None:
    identity = age.bech32_encode(age.SECRET_PREFIX, ALICE).upper()
    assert identity.startswith("AGE-SECRET-KEY-1") and len(identity) == 74
    found = age.recipient(identity)
    assert found.startswith("age1") and found == found.lower()
    assert age.bech32_decode(found) == ("age", bytes.fromhex(ALICE_PUBLIC))
    generated = age.generate_identity()
    assert generated == age.check_identity(f" {generated}\n")
    assert age.recipient(generated) != found
    for bad in (identity.lower(), found, "AGE-SECRET-KEY-1", ""):
        with pytest.raises(age.AgeError):
            age.check_identity(bad)


@pytest.mark.skipif(shutil.which("age-keygen") is None, reason="no age-keygen")
def test_age_keygen_derives_the_same_recipient() -> None:
    identity = age.generate_identity()
    done = subprocess.run(
        ["age-keygen", "-y"], input=identity + "\n", capture_output=True,
        text=True, check=True, timeout=30,
    )  # fmt: skip
    assert done.stdout.strip() == age.recipient(identity)
