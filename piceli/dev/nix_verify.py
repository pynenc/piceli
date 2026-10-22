"""Verify Nix cache signatures and Piceli build manifests, standard library only (0.24.0).

A host that switches to a prebuilt system checks, before it trusts a store
path, that the ref pointer, the build manifest and the path's ``.narinfo``
are signed by the cache's public key (``NAME:base64`` of an Ed25519 key,
Nix's format). This module needs nothing but the standard library (Ed25519
verification as in RFC 8032, section 6), so it runs on a host without
third-party packages and without calling ``nix``; signing
(:mod:`piceli.dev.nix`) needs ``cryptography``. The host agent
(:mod:`piceli.hosts.agent`) carries a copy of this file, and also reads a
private bucket with :func:`sigv4_headers`.

Importing this module is side-effect free.
"""

from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import hmac
import json
import re
import urllib.parse
from collections.abc import Mapping
from typing import Any

MANIFEST_SCHEMA = "piceli.nix-build.v1"
#: A signed pointer from a Git ref to a build manifest (0.24.0).
POINTER_SCHEMA = "piceli.nix-ref.v1"
_KEY_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,62}")

# ------------------------------------------------------------------ Ed25519

_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)
_Point = tuple[int, int, int, int]


def _add(a: _Point, b: _Point) -> _Point:
    x = (a[1] - a[0]) * (b[1] - b[0]) % _P
    y = (a[1] + a[0]) * (b[1] + b[0]) % _P
    c = 2 * a[3] * b[3] * _D % _P
    d = 2 * a[2] * b[2] % _P
    e, f, g, h = y - x, d - c, d + c, y + x
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _mul(scalar: int, point: _Point) -> _Point:
    result: _Point = (0, 1, 1, 0)
    while scalar > 0:
        if scalar & 1:
            result = _add(result, point)
        point = _add(point, point)
        scalar >>= 1
    return result


def _equal(a: _Point, b: _Point) -> bool:
    return (a[0] * b[2] - b[0] * a[2]) % _P == 0 and (
        a[1] * b[2] - b[1] * a[2]
    ) % _P == 0


def _x(y: int, sign: int) -> int | None:
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P:
        return None
    return _P - x if (x & 1) != sign else x


def _point(raw: bytes) -> _Point | None:
    if len(raw) != 32:
        return None
    y = int.from_bytes(raw, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    x = _x(y, sign)
    return None if x is None else (x, y, 1, x * y % _P)


_GY = 4 * pow(5, _P - 2, _P) % _P
_G: _Point = (_x(_GY, 0) or 0, _GY, 1, (_x(_GY, 0) or 0) * _GY % _P)


def ed25519_verify(public: bytes, message: bytes, signature: bytes) -> bool:
    """RFC 8032 Ed25519 verification of a 64-byte signature by a 32-byte key."""
    if len(signature) != 64:
        return False
    a, r = _point(public), _point(signature[:32])
    s = int.from_bytes(signature[32:], "little")
    if a is None or r is None or s >= _L:
        return False
    h = (
        int.from_bytes(
            hashlib.sha512(signature[:32] + public + message).digest(), "little"
        )
        % _L
    )
    return _equal(_mul(s, _G), _add(r, _mul(h, a)))


# ------------------------------------------------------------------ Nix keys


def decode(text: str, size: int) -> tuple[str, bytes] | None:
    """``(name, raw)`` of a Nix ``NAME:base64`` value of ``size`` bytes."""
    name, sep, body = str(text).strip().partition(":")
    try:
        raw = base64.b64decode(body, validate=True)
    except ValueError:
        return None
    if not sep or not _KEY_NAME.fullmatch(name) or len(raw) != size:
        return None
    return name, raw


def verify(public: str, message: bytes, signature: str) -> bool:
    """Whether ``signature`` (``NAME:base64``) is ``public``'s over ``message``."""
    key, sig = decode(public, 32), decode(signature, 64)
    if key is None or sig is None or key[0] != sig[0]:
        return False
    return ed25519_verify(key[1], message, sig[1])


def canonical(value: Mapping[str, Any]) -> bytes:
    """The bytes a manifest's signature covers: sorted keys, no spaces."""
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def verify_signed(body: Mapping[str, Any], public: str, schema: str) -> bool:
    """Whether ``body`` has ``schema`` and ``public``'s signature over every
    other field (canonical JSON)."""
    unsigned = {k: v for k, v in body.items() if k != "signature"}
    return (
        body.get("schema") == schema
        and body.get("signed_by") == str(public).strip().partition(":")[0]
        and isinstance(body.get("signature"), str)
        and verify(public, canonical(unsigned), str(body["signature"]))
    )


def verify_manifest(body: Mapping[str, Any], public: str) -> bool:
    """Whether ``body`` is a ``piceli.nix-build.v1`` manifest signed by ``public``
    (the signature covers every other field)."""
    return verify_signed(body, public, MANIFEST_SCHEMA)


def verify_pointer(body: Mapping[str, Any], public: str) -> bool:
    """Whether ``body`` is a ``piceli.nix-ref.v1`` ref pointer signed by ``public``."""
    return verify_signed(body, public, POINTER_SCHEMA)


def manifest_digest(body: Mapping[str, Any]) -> str:
    """``sha256:HEX`` of a signed manifest's canonical JSON (a pointer names it)."""
    return "sha256:" + hashlib.sha256(canonical(body)).hexdigest()


_NIX32 = "0123456789abcdfghijklmnpqrsvwxyz"


def nix32(raw: bytes) -> str:
    """Nix's base-32 encoding (the ``NarHash`` of a ``.narinfo``)."""
    size = (len(raw) * 8 + 4) // 5
    out = []
    for n in range(size - 1, -1, -1):
        i, j = divmod(n * 5, 8)
        c = raw[i] >> j
        if i + 1 < len(raw):
            c |= raw[i + 1] << (8 - j)
        out.append(_NIX32[c & 0x1F])
    return "".join(out)


def nar_hash(text: str) -> str:
    """``sha256:<nix32>`` as a ``.narinfo`` writes it, from that form or the
    SRI form (``sha256-<base64>``) newer ``nix path-info --json`` prints."""
    algo, sep, body = str(text).partition("-")
    if sep and algo == "sha256" and ":" not in text:
        try:
            return f"sha256:{nix32(base64.b64decode(body, validate=True))}"
        except ValueError:
            return str(text)
    return str(text)


def narinfo_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition(": ")
        if sep and key not in fields:
            fields[key] = value.strip()
    return fields


def verify_narinfo(text: str, public: str, store_dir: str = "/nix/store") -> bool:
    """Whether a ``.narinfo`` carries ``public``'s signature over its fingerprint
    (``1;PATH;NAR_HASH;NAR_SIZE;REFERENCES``), as Nix checks it."""
    fields = narinfo_fields(text)
    references = ",".join(
        f"{store_dir}/{ref}" for ref in fields.get("References", "").split() if ref
    )
    fingerprint = (
        f"1;{fields.get('StorePath')};{fields.get('NarHash')};"
        f"{fields.get('NarSize')};{references}"
    ).encode()
    return any(
        verify(public, fingerprint, line[len("Sig: ") :].strip())
        for line in text.splitlines()
        if line.startswith("Sig: ")
    )


# ------------------------------------------------------------------ S3


def _quote(text: str) -> str:
    return urllib.parse.quote(text, safe="-_.~")


def sigv4_headers(
    method: str,
    url: str,
    body: bytes,
    *,
    region: str,
    access_key: str,
    secret_key: str,
    now: _dt.datetime,
    content_type: str | None = None,
) -> dict[str, str]:
    """AWS Signature Version 4 headers of one S3 request (path-style)."""
    parts = urllib.parse.urlsplit(url)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    day = amz_date[:8]
    payload = hashlib.sha256(body).hexdigest()
    headers = {
        **({"content-type": content_type} if content_type else {}),
        "host": parts.netloc,
        "x-amz-content-sha256": payload,
        "x-amz-date": amz_date,
    }
    signed = ";".join(sorted(headers))
    query = "&".join(
        f"{_quote(k)}={_quote(v)}"
        for k, v in sorted(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    )
    request = "\n".join(
        [
            method,
            urllib.parse.quote(urllib.parse.unquote(parts.path) or "/", safe="/-_.~"),
            query,
            "".join(f"{k}:{headers[k]}\n" for k in sorted(headers)),
            signed,
            payload,
        ]
    )
    scope = f"{day}/{region}/s3/aws4_request"
    to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(request.encode()).hexdigest(),
        ]
    )
    key = f"AWS4{secret_key}".encode()
    for part in (day, region, "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed}, Signature={signature}"
    )
    return headers
