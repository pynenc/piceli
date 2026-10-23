"""Credentials with parts: what each provider's credential holds (0.25.0).

A Porkbun API key pair and an OpenStack application credential have two
parts, an identifier and a secret; a bucket's S3 keys too. ``piceli secrets
provider NAME`` and ``piceli secrets s3 NAME`` ask for each part by the name
the provider's console gives it, check it as it is entered (never printing
it) and store the parts under their own names::

    {"schema": "piceli.credential.v1", "kind": "provider-token",
     "provider": "porkbun", "parts": {"api_key": "...", "secret_api_key": "..."}}

An older credential stored as one ``A:B`` value still reads.
:func:`credential_report` tells whether a stored credential holds every
part (never a value); :func:`require_complete` refuses one that does not
(``infra-credential-incomplete``), which every plan does before any API
call; :func:`credential_reports` reports the whole store.

Which provider a credential is for: ``--provider``, else the declared
provider that uses ``credentials=NAME`` (``--infra MODULE:ATTR``), else the
provider recorded when it was stored, else the name (:func:`shape_for_name`:
``porkbun``, ``openstack`` or ``infomaniak``, ``hetzner`` or ``hcloud``,
alone or before a dash), else one API token.

Importing this module is side-effect free.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from piceli.infra.machines.model import InfraError

__all__ = [
    "PROVIDER_OPTIONS",
    "SHAPES",
    "Part",
    "Shape",
    "check_part",
    "credential_report",
    "credential_reports",
    "fix_command",
    "load_parts",
    "require_complete",
    "shape_for_name",
    "shape_of",
]


@dataclass(frozen=True)
class Part:
    """One part of a credential: how it is asked for, checked and stored."""

    #: The name it is stored under (``api_key``).
    key: str
    #: The prompt, as the provider's console names it.
    label: str
    #: Its name in a sentence (``secret API key``).
    name: str
    #: The provider's standard environment variable (``--from-env``).
    env: str
    #: A secret is never echoed; an identifier may be.
    secret: bool
    #: Where to find it.
    where: str
    #: The known shape (a full match), and how it is said.
    pattern: str | None = None
    expect: str = ""
    #: Prefixes of another part, and what that part is (a swapped paste).
    other: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Shape:
    """What a provider's credential holds, part by part."""

    provider: str
    kind: str
    title: str
    parts: tuple[Part, ...]

    @property
    def keys(self) -> list[str]:
        return [part.key for part in self.parts]

    def names(self) -> str:
        """``API key and secret API key``."""
        names = [part.name for part in self.parts]
        return (
            names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
        )


_PORKBUN_WHERE = (
    "porkbun.com > Account > API Access > Create API Key; the secret is shown "
    "once, and each domain needs API Access turned on"
)
_OPENSTACK_WHERE = (
    "the cloud's dashboard (Horizon): Identity > Application Credentials > "
    "Create Application Credential, or `openstack application credential "
    "create`; the secret is shown once"
)
SHAPES: dict[str, Shape] = {
    "porkbun": Shape(
        "porkbun",
        "provider-token",
        "Porkbun API key pair",
        (
            Part(
                "api_key",
                "Porkbun API key (pk1_…)",
                "API key",
                "PORKBUN_API_KEY",
                False,
                _PORKBUN_WHERE,
                r"pk1_[0-9A-Za-z]{16,}",
                "it starts with pk1_",
                (("sk1_", "the secret API key (sk1_…)"),),
            ),
            Part(
                "secret_api_key",
                "Porkbun secret API key (sk1_…)",
                "secret API key",
                "PORKBUN_SECRET_API_KEY",
                True,
                _PORKBUN_WHERE,
                r"sk1_[0-9A-Za-z]{16,}",
                "it starts with sk1_",
                (("pk1_", "the API key (pk1_…)"),),
            ),
        ),
    ),
    "openstack": Shape(
        "openstack",
        "provider-token",
        "OpenStack application credential",
        (
            Part(
                "application_credential_id",
                "Application credential ID",
                "application credential ID",
                "OS_APPLICATION_CREDENTIAL_ID",
                False,
                _OPENSTACK_WHERE,
                r"[0-9a-fA-F]{32}",
                "it is 32 hex characters",
            ),
            Part(
                "application_credential_secret",
                "Application credential secret",
                "application credential secret",
                "OS_APPLICATION_CREDENTIAL_SECRET",
                True,
                _OPENSTACK_WHERE,
            ),
        ),
    ),
    "hetzner": Shape(
        "hetzner",
        "provider-token",
        "Hetzner Cloud API token",
        (
            Part(
                "token",
                "Hetzner Cloud API token",
                "API token",
                "HCLOUD_TOKEN",
                True,
                "Hetzner Console > your project > Security > API tokens (Read & Write)",
            ),
        ),
    ),
    "token": Shape(
        "token",
        "provider-token",
        "provider API token",
        (
            Part(
                "token",
                "Provider API token",
                "API token",
                "PICELI_PROVIDER_TOKEN",
                True,
                "the provider's console (pass --provider for a known provider: "
                "porkbun, openstack, hetzner)",
            ),
        ),
    ),
    "s3": Shape(
        "s3",
        "s3-keys",
        "S3 access keys",
        (
            Part(
                "access_key_id",
                "S3 access key id",
                "access key id",
                "AWS_ACCESS_KEY_ID",
                False,
                "the bucket provider's S3 or EC2 credentials (`openstack ec2 "
                "credentials create` on OpenStack)",
            ),
            Part(
                "secret_access_key",
                "S3 secret access key",
                "secret access key",
                "AWS_SECRET_ACCESS_KEY",
                True,
                "shown with the access key id",
            ),
        ),
    ),
}
#: ``--provider`` values and the shape each names.
PROVIDER_OPTIONS = {
    "porkbun": "porkbun",
    "openstack": "openstack",
    "infomaniak": "openstack",
    "hetzner": "hetzner",
    "hcloud": "hetzner",
    "token": "token",
}
#: Credential names (or their first dash-separated word) that tell the provider.
_NAME_HINTS = {k: v for k, v in PROVIDER_OPTIONS.items() if k != "token"}
_WORDS = {0: "no part", 1: "one part", 2: "two parts", 3: "three parts"}
_COUNT = {1: "one", 2: "two", 3: "three"}


def shape_of(provider: str | None) -> Shape:
    """The shape of a provider (its ``Provider.name`` or a ``--provider``
    value); one API token for a provider without a known one."""
    key = PROVIDER_OPTIONS.get(provider or "", provider or "")
    return SHAPES.get(key) or SHAPES["token"]


def shape_for_name(name: str) -> Shape:
    """The shape a credential's name tells: the name or its first word."""
    hint = _NAME_HINTS.get(name) or _NAME_HINTS.get(name.split("-", 1)[0])
    return SHAPES[hint or "token"]


def fix_command(name: str, shape: Shape) -> str:
    """The command that stores ``name`` again, with ``--provider`` when the
    name does not tell it."""
    if shape.kind == "s3-keys":
        return f"piceli secrets s3 {name} --prompt"
    option = (
        f" --provider {shape.provider}"
        if shape_for_name(name).provider != shape.provider
        else ""
    )
    return f"piceli secrets provider {name}{option} --prompt"


def check_part(
    shape: Shape, part: Part, value: str, earlier: Mapping[str, str] = {}
) -> None:
    """Refuse a wrong part (``infra-credential-part-invalid``, naming the
    part, never the value): empty, whitespace around it, a ``:`` inside, the
    wrong shape, or the same as another part."""
    why = None
    if not value:
        why = "it is empty"
    elif value != value.strip():
        why = "it has a space, tab or line break around it"
    elif any(ord(c) < 32 or ord(c) == 127 for c in value) or len(value) > 4096:
        why = "it has control characters or is longer than 4096 characters"
    elif ":" in value and len(shape.parts) > 1:
        why = "it contains ':'; give one part per line, not ID:SECRET"
    else:
        swapped = next((what for prefix, what in part.other
                        if value.startswith(prefix)), None)  # fmt: skip
        if swapped is not None:
            why = f"this is {swapped}; {part.expect}"
        elif part.pattern is not None and not re.fullmatch(part.pattern, value):
            why = f"this is not a {part.name}: {part.expect}"
        elif value in earlier.values():
            why = "it is the same as the part before it"
    if why is not None:
        raise InfraError(
            "infra-credential-part-invalid",
            f"{part.label}: {why}; nothing was stored",
            fields={"part": part.key},
        )


# ------------------------------------------------------------- reading back


def _body(name: str) -> tuple[str, dict[str, Any] | None]:
    """``missing``, ``damaged`` or ``present`` and the stored JSON."""
    from piceli.infra.machines.credentials import _path

    path = _path(name)
    if not path.is_file():
        return "missing", None
    try:
        body = json.loads(path.read_text())
    except (OSError, ValueError, UnicodeDecodeError):
        return "damaged", None
    return ("present", body) if isinstance(body, dict) else ("damaged", None)


def _shape_of_body(name: str, body: Mapping[str, Any]) -> Shape | None:
    kind = body.get("kind")
    if kind == "s3-keys":
        return SHAPES["s3"]
    if kind != "provider-token":
        return None
    stored = body.get("provider")
    if isinstance(stored, str) and stored in SHAPES:
        return SHAPES[stored]
    return shape_for_name(name)


def _split(shape: Shape, body: Mapping[str, Any]) -> tuple[str, dict[str, str]]:
    """How it is stored (``parts``, ``joined``, ``single``) and the parts
    it holds by key. A stored part of another shape counts for nothing."""
    parts = body.get("parts")
    if isinstance(parts, dict):
        return "parts", {k: v for k, v in parts.items()
                         if k in shape.keys and isinstance(v, str) and v}  # fmt: skip
    value = body.get("value")
    value = value if isinstance(value, str) else ""
    if len(shape.parts) == 1:
        return "single", {shape.keys[0]: value} if value else {}
    first, separator, rest = value.partition(":")
    if separator and first and rest:
        return "joined", dict(zip(shape.keys, (first, rest), strict=False))
    return "single", {"?": value} if value else {}


def credential_report(name: str, provider: str | None = None) -> dict[str, Any]:
    """Whether ``name`` holds every part of its provider's credential.

    ``provider`` is the provider that uses it (its ``Provider.name``);
    otherwise the one recorded when it was stored, or its name tells. The
    report carries names and counts, never a value::

        {"name", "kind", "provider", "expected": [part, ...], "stored": n,
         "storage": "parts"|"joined"|"single"|"missing"|"damaged",
         "complete": bool, "fix": command, "message": text}
    """
    state, body = _body(name)
    shape = (
        shape_of(provider)
        if provider is not None
        else (_shape_of_body(name, body) if body else None) or shape_for_name(name)
    )
    if body is not None and body.get("kind") not in (None, "provider-token", "s3-keys"):
        # One-value kinds (a passphrase, a key): complete when a value is there.
        value = body.get("value")
        ok = isinstance(value, str) and bool(value)
        return {
            "name": name, "kind": body.get("kind"), "provider": None,
            "expected": ["value"], "stored": int(ok), "storage": "single",
            "complete": ok, "fix": None,
            "message": f"{name}: complete" if ok else f"{name}: no value stored",
        }  # fmt: skip
    if body is not None and body.get("kind") == "s3-keys":
        shape = SHAPES["s3"]
    fix = fix_command(name, shape)
    expected = shape.keys
    held: dict[str, str] = {}
    storage = state
    if body is not None:
        storage, held = _split(shape, body)
    stored = len(held)
    stored_provider = body.get("provider") if body else None
    mismatch = (
        storage == "parts"
        and isinstance(stored_provider, str)
        and stored_provider != shape.provider
    )
    complete = not mismatch and set(held) == set(expected)
    if complete:
        message = f"{name}: complete"
    elif mismatch:
        message = (
            f"{name}: holds a {stored_provider} credential, not {shape.title}; "
            f"re-run `{fix}`"
        )
    elif storage == "missing":
        message = f"{name}: not stored; run `{fix}`"
    elif storage == "damaged":
        message = f"{name}: unreadable; re-run `{fix}`"
    else:
        message = (
            f"{name}: {_WORDS.get(stored, f'{stored} parts')} of "
            f"{_COUNT.get(len(expected), str(len(expected)))} stored, re-run `{fix}`"
        )
    return {
        "name": name,
        "kind": shape.kind,
        "provider": shape.provider,
        "expected": expected,
        "stored": stored,
        "storage": storage,
        "complete": complete,
        "fix": fix,
        "message": message,
    }


def credential_reports() -> list[dict[str, Any]]:
    """:func:`credential_report` of every ``NAME.json`` in the store, by name."""
    from piceli.infra.machines.credentials import credentials_dir

    directory = credentials_dir()
    if not directory.is_dir():
        return []
    return [
        credential_report(path.stem)
        for path in sorted(directory.glob("*.json"))
        if path.is_file() and not path.name.startswith(".")
    ]


def require_complete(name: str, provider: str | None = None) -> None:
    """Refuse (``infra-credential-incomplete``) a stored credential that
    lacks a part; a missing one is left to the loader's own refusal."""
    report = credential_report(name, provider)
    if report["complete"] or report["storage"] == "missing":
        return
    raise InfraError(
        "infra-credential-incomplete", report["message"], fields={"credential": report}
    )


def joined(name: str, body: Mapping[str, Any]) -> str:
    """The value of a stored credential, parts joined with ``:`` in order
    (what the providers split); ``infra-credential-incomplete`` when a part
    is missing. An older value whose provider was never recorded is returned
    as it is: the plan checks it against the provider that uses it."""
    shape = _shape_of_body(name, body)
    legacy = body.get("kind") == "provider-token" and "provider" not in body
    if shape is None or (legacy and "parts" not in body):
        return str(body.get("value"))
    _, held = _split(shape, body)
    if set(held) != set(shape.keys):
        raise InfraError(
            "infra-credential-incomplete",
            credential_report(name, shape.provider)["message"],
        )
    return ":".join(held[key] for key in shape.keys)


def load_parts(name: str, provider: str | None = None) -> dict[str, str]:
    """The parts stored under ``name`` by key (an older ``A:B`` value split),
    after :func:`require_complete`."""
    from piceli.infra.machines.credentials import load_credential

    require_complete(name, provider)
    _, body = _body(name)
    assert body is not None
    shape = shape_of(provider) if provider is not None else _shape_of_body(name, body)
    assert shape is not None
    value = load_credential(name, shape.kind)
    if len(shape.parts) == 1:
        return {shape.keys[0]: value}
    first, _, rest = value.partition(":")
    return dict(zip(shape.keys, (first, rest), strict=True))
