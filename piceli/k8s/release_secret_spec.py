"""Typed ``[secrets.*]`` generator specs for ``release.toml``.

Each table declares one generator; its ``type`` picks the model:

* ``random``: a URL-safe random token.
* ``tls-self-signed``: an RSA key and a self-signed certificate.
* ``tls-ca``: a private CA and leaf certificates it signs.
* ``template``: a string (a DSN, a config file) with ``{name}`` placeholders,
  rendered inside the private store.
* ``import``: an existing value from a file, an environment variable or a live
  Secret key.
* ``static``: a public, non-secret value (a user name, a port) that templates
  and compositions can reference like any other input.

A generator produces named **outputs** (``Output``). Exposed outputs are what
``ReleaseContext.secret(name)`` returns references for; internal outputs (the
CA key) stay in the private store. This module only validates; it never reads
files, environment variables or clusters, and importing it has no side effects.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_DNS = re.compile(
    r"(?:\*\.)?(?:[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?"
)
_LEAF = re.compile(r"[a-z][a-z0-9_-]{0,62}")
_ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_K8S_NAME = re.compile(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?")
_DATA_KEY = re.compile(r"[-._a-zA-Z0-9]{1,253}")
# ``{name}``, ``{name.key}`` or ``{secret:name.key}``; ``{{``/``}}`` are literals.
_TOKEN = re.compile(r"\{\{|\}\}|\{(?:secret:)?([a-z][a-z0-9_.-]{0,190})\}|[{}]")

Encoding = Literal["base64", "raw"]


class SecretError(ValueError):
    """A secret generator, template or reveal was refused; ``code`` is stable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{message} [{code}]")
        self.code = code


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _dns_names(value: tuple[str, ...]) -> tuple[str, ...]:
    for item in value:
        if len(item) > 253 or not _DNS.fullmatch(item):
            raise ValueError(f"invalid DNS name {item!r}")
    return value


def _ip_addresses(value: tuple[str, ...]) -> tuple[str, ...]:
    for item in value:
        ipaddress.ip_address(item)
    return value


class _OpenSsl(_Strict):
    openssl: Path = Path("/usr/bin/openssl")
    openssl_sha256: str | None = None

    @field_validator("openssl")
    @classmethod
    def _tool(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("openssl must be an absolute path (a pinned tool)")
        return value

    @field_validator("openssl_sha256")
    @classmethod
    def _tool_digest(cls, value: str | None) -> str | None:
        if value is not None and not _DIGEST.fullmatch(value):
            raise ValueError("openssl_sha256 must be sha256:<64 hex>")
        return value


class RandomSecretSpec(_Strict):
    type: Literal["random"]
    bytes: int = Field(default=32, ge=16, le=512)
    encoding: Encoding = "base64"


class TlsSelfSignedSpec(_OpenSsl):
    type: Literal["tls-self-signed"]
    dns_names: tuple[str, ...] = Field(min_length=1, max_length=32)
    ip_addresses: tuple[str, ...] = ()
    days: int = Field(default=365, ge=1, le=3650)
    rsa_bits: Literal[2048, 3072, 4096] = 2048
    encoding: Encoding = "base64"

    @field_validator("dns_names")
    @classmethod
    def _dns(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _dns_names(value)

    @field_validator("ip_addresses")
    @classmethod
    def _ips(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _ip_addresses(value)


class TlsLeafSpec(_Strict):
    """One leaf certificate signed by a ``tls-ca``; needs a DNS name or an IP."""

    dns_names: tuple[str, ...] = Field(default=(), max_length=32)
    ip_addresses: tuple[str, ...] = Field(default=(), max_length=32)

    @field_validator("dns_names")
    @classmethod
    def _dns(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _dns_names(value)

    @field_validator("ip_addresses")
    @classmethod
    def _ips(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _ip_addresses(value)

    @model_validator(mode="before")
    @classmethod
    def _shorthand(cls, value: Any) -> Any:
        # ``cache = ["cache", "cache.shop.svc"]`` is short for dns_names.
        if isinstance(value, list | tuple):
            return {"dns_names": tuple(value)}
        return value

    @model_validator(mode="after")
    def _names(self) -> TlsLeafSpec:
        if not self.dns_names and not self.ip_addresses:
            raise ValueError("a leaf needs at least one DNS name or IP address")
        return self


class TlsCaSpec(_OpenSsl):
    """A private CA plus leaves; clients trust one ``ca.crt``."""

    type: Literal["tls-ca"]
    common_name: str = Field(default="piceli private CA", min_length=1, max_length=64)
    ca_days: int = Field(default=3650, ge=1, le=7300)
    days: int = Field(default=365, ge=1, le=3650)
    rsa_bits: Literal[2048, 3072, 4096] = 2048
    encoding: Encoding = "base64"
    leaves: dict[str, TlsLeafSpec] = Field(min_length=1, max_length=64)

    @field_validator("common_name")
    @classmethod
    def _common_name(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]*", value):
            raise ValueError("common_name may use letters, digits, space, . _ -")
        return value

    @field_validator("leaves")
    @classmethod
    def _leaves(cls, value: dict[str, TlsLeafSpec]) -> dict[str, TlsLeafSpec]:
        for name in value:
            if not _LEAF.fullmatch(name) or name == "ca":
                raise ValueError(f"invalid leaf name {name!r} ('ca' is reserved)")
        return value

    @model_validator(mode="after")
    def _validity(self) -> TlsCaSpec:
        if self.days > self.ca_days:
            raise ValueError("leaf days cannot exceed ca_days")
        return self


class TemplateSecretSpec(_Strict):
    """Text with ``{name}`` / ``{name.key}`` placeholders for other outputs."""

    type: Literal["template"]
    template: str = Field(min_length=1, max_length=256_000)
    encoding: Encoding = "base64"

    @field_validator("template")
    @classmethod
    def _parse(cls, value: str) -> str:
        template_references(value)
        return value


class LiveSecretKey(_Strict):
    name: str
    key: str

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not _K8S_NAME.fullmatch(value):
            raise ValueError(f"invalid Secret name {value!r}")
        return value

    @field_validator("key")
    @classmethod
    def _key(cls, value: str) -> str:
        if not _DATA_KEY.fullmatch(value):
            raise ValueError(f"invalid Secret data key {value!r}")
        return value


class ImportSecretSpec(_Strict):
    """An existing value: exactly one of ``file``, ``env`` or ``secret``.

    The import is the first version. Later releases carry it over; ``--rotate``
    replaces it with ``rotate = "random"`` (a new token of ``bytes``) or reads
    the source again with ``rotate = "reimport"``.
    """

    type: Literal["import"]
    file: Path | None = None
    env: str | None = None
    secret: LiveSecretKey | None = None
    trim_newline: bool = True
    encoding: Encoding = "base64"
    rotate: Literal["random", "reimport"] = "random"
    bytes: int = Field(default=32, ge=16, le=512)

    @field_validator("env")
    @classmethod
    def _env(cls, value: str | None) -> str | None:
        if value is not None and not _ENV.fullmatch(value):
            raise ValueError(f"invalid environment variable name {value!r}")
        return value

    @model_validator(mode="after")
    def _one_source(self) -> ImportSecretSpec:
        sources = [self.file, self.env, self.secret]
        if sum(item is not None for item in sources) != 1:
            raise ValueError("declare exactly one of file, env or secret")
        return self

    def source(self) -> str:
        """Public description of where the value comes from (never the value)."""
        if self.file is not None:
            return f"file:{self.file}"
        if self.env is not None:
            return f"env:{self.env}"
        assert self.secret is not None
        return f"secret:{self.secret.name}/{self.secret.key}"


class StaticSecretSpec(_Strict):
    """A public value; it is stored like the others but is not secret."""

    type: Literal["static"]
    value: str = Field(max_length=64_000)
    encoding: Encoding = "base64"


GeneratorSpec = (
    RandomSecretSpec
    | TlsSelfSignedSpec
    | TlsCaSpec
    | TemplateSecretSpec
    | ImportSecretSpec
    | StaticSecretSpec
)
SecretSpec = Annotated[GeneratorSpec, Field(discriminator="type")]


@dataclass(frozen=True)
class Output:
    """One named value a generator produces.

    ``name`` is the session input name (``ReleaseContext.secret(name)``);
    ``key`` is the name within the generator (``None`` for single outputs);
    ``part`` groups outputs that are carried over or regenerated together.
    """

    name: str
    key: str | None = None
    internal: bool = False
    part: str = ""


def outputs(name: str, spec: GeneratorSpec) -> tuple[Output, ...]:
    """All outputs of one generator, internal ones included."""
    if isinstance(spec, TlsSelfSignedSpec):
        return (Output(f"{name}.crt", "crt"), Output(f"{name}.key", "key"))
    if isinstance(spec, TlsCaSpec):
        result = [
            Output(f"{name}.ca.crt", "ca.crt", part="ca"),
            Output(f"{name}.ca.key", "ca.key", internal=True, part="ca"),
        ]
        for leaf in sorted(spec.leaves):
            part = f"leaf:{leaf}"
            result.append(Output(f"{name}.{leaf}.crt", f"{leaf}.crt", part=part))
            result.append(Output(f"{name}.{leaf}.key", f"{leaf}.key", part=part))
        return tuple(result)
    return (Output(name),)


def template_references(template: str) -> tuple[str, ...]:
    """Placeholder names in order; a stray brace is an error, not a literal."""
    names: list[str] = []
    for match in _TOKEN.finditer(template):
        token = match.group(0)
        if token in {"{{", "}}"}:
            continue
        if match.group(1) is None:
            raise SecretError(
                "secret-template-invalid",
                f"stray {token!r} at offset {match.start()} in a template; write "
                "'{{' or '}}' for a literal brace",
            )
        names.append(match.group(1))
    return tuple(names)


def render_template(template: str, values: Mapping[str, str]) -> str:
    """Substitute placeholders; callers validated every name beforehand."""

    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        if token == "{{":
            return "{"
        if token == "}}":
            return "}"
        return values[match.group(1)]

    return _TOKEN.sub(replace, template)


def template_dependencies(
    secrets: Mapping[str, GeneratorSpec],
) -> dict[str, dict[str, str]]:
    """``{template: {placeholder: output name}}``; unknown names are refused."""
    exposed: dict[str, str] = {}
    multi: dict[str, list[str]] = {}
    for name, spec in secrets.items():
        for output in outputs(name, spec):
            if not output.internal:
                exposed[output.name] = name
        keys = [o.key for o in outputs(name, spec) if o.key and not o.internal]
        if keys:
            multi[name] = keys
    result: dict[str, dict[str, str]] = {}
    for name, spec in secrets.items():
        if not isinstance(spec, TemplateSecretSpec):
            continue
        mapping: dict[str, str] = {}
        for reference in template_references(spec.template):
            if reference in exposed:
                mapping[reference] = reference
                continue
            hint = (
                f"; use one of {sorted(f'{reference}.{key}' for key in multi[reference])}"
                if reference in multi
                else ""
            )
            raise SecretError(
                "secret-unknown-reference",
                f"template {name!r} references {{{reference}}}, which is not an "
                f"exposed output of a declared generator{hint}",
            )
        result[name] = mapping
    return result


def generation_order(secrets: Mapping[str, GeneratorSpec]) -> tuple[str, ...]:
    """Generators in dependency order (templates after what they reference)."""
    dependencies = template_dependencies(secrets)
    owner = {
        output.name: name
        for name, spec in secrets.items()
        for output in outputs(name, spec)
    }
    graph = {
        name: sorted({owner[item] for item in dependencies.get(name, {}).values()})
        for name in secrets
    }
    order: list[str] = []
    state: dict[str, int] = {}

    def visit(name: str, path: tuple[str, ...]) -> None:
        if state.get(name) == 2:
            return
        if state.get(name) == 1:
            cycle = " -> ".join((*path[path.index(name) :], name))
            raise SecretError(
                "secret-dependency-cycle", f"secret templates form a cycle: {cycle}"
            )
        state[name] = 1
        for dependency in graph[name]:
            visit(dependency, (*path, name))
        state[name] = 2
        order.append(name)

    for name in sorted(secrets):
        visit(name, ())
    return tuple(order)


def consumed_outputs(secrets: Mapping[str, GeneratorSpec]) -> set[str]:
    """Exposed outputs that a template uses; binding them is optional."""
    return {
        item
        for mapping in template_dependencies(secrets).values()
        for item in mapping.values()
    }


def check_secrets(secrets: Mapping[str, GeneratorSpec]) -> None:
    """Validate template references and ordering; raises :class:`SecretError`."""
    generation_order(secrets)
