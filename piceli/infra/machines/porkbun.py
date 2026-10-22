"""Porkbun: DNS records at the registrar, for servers at any provider.

Rendered for the ``marcfrederick/porkbun`` OpenTofu provider (Porkbun API
v3), pinned to one version and its lock-file hashes. The credential is an
API key pair stored as ``APIKEY:SECRETKEY`` (``piceli secrets provider NAME
--prompt``); OpenTofu reads it from ``PORKBUN_API_KEY`` and
``PORKBUN_SECRET_API_KEY`` in its environment, never from the
configuration, a plan or the state.

Porkbun holds no servers: ``DnsRecord(..., provider=Porkbun(...))`` points
at servers of another provider (an OpenStack or Hetzner server). Their
addresses are references into the same configuration, resolved through each
server's own provider, so a record follows a replaced server (or the
persistent port it keeps). One ``porkbun_dns_record`` per answer: a record
with several servers renders one resource per server.

Ownership: Porkbun records carry no labels (the OpenTofu provider cannot set
a record's notes), so the ledger alone says Piceli created one. Porkbun
refuses a second record with the same name, type and content, so Piceli
never adopts a record it did not create. Each domain needs API access turned
on in Porkbun's console; a refusal for that is ``infra-dns-api-disabled``.

Plan-time check: a plan that changes records at Porkbun asks Porkbun's API
(``POST /dns/retrieve/DOMAIN``) once per domain whether it answers for that
key pair, so an approved plan does not fail at apply for a domain without
API access. A domain without it fails the plan (``infra-dns-api-disabled``);
a check that cannot run (no key pair, no answer, another refusal) fails it
too (``infra-dns-check-failed``): the plan never passes a check it did not
run. The key pair goes only into the request body, never into a message,
the plan or the state. The plan's ``checks`` record whether it ran.

Importing this module is side-effect free.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, NoReturn

from piceli.infra.machines.model import (
    DnsRecord,
    Firewall,
    InfraError,
    PrimaryIp,
    Server,
)
from piceli.infra.machines.provider import Pin, Provider, Resource

__all__ = ["PORKBUN_PIN", "Porkbun", "api_access"]

#: marcfrederick/porkbun, as ``tofu providers lock`` locked it for every
#: platform the registry publishes (refresh: see the docs).
PORKBUN_PIN = Pin(
    local_name="porkbun",
    source="registry.opentofu.org/marcfrederick/porkbun",
    version="1.3.3",
    hashes=(
        "h1:7pd/XzGn1uU8dB2Q3p+OmNM0apgvNoziaQgV1GgqWxc=",
        "h1:ETE198D4KRlETcvui15+a++IRnxSsOSk2X9FIBeare4=",
        "h1:FIvrECEORerVTmuqslVWE/SnM1mZ9PUAPDh4zWrotMg=",
        "h1:IMWtn8KU7Ee5fV6xtXcHFzd8gchEQ0G2zflh3ap4/hI=",
        "h1:RDIV2kXgFdVL8oMxDzZMsiw1a1SIxqpg46F9GRuq/vQ=",
        "h1:VMkW8FjFqUVGJ3AZaG8mb0MZpE6Gy8iVZ28LL6rJ8Pc=",
        "h1:eWjEf8lM0rYz5dQ0OSN8GiBTFMjRypfZYdougdvaeZE=",
        "h1:lqX/HzE1Egv3MpJX1gZvf9xym6J7nzbLoLHDWdAvrXc=",
        "h1:oC1q2jDns+OSy1b6POZzcvXGHjsAm02PrQV2b/EsPrc=",
        "h1:ol8kESoWenFzbD5mnSxGbtGtfIdlpd4/RwRQdZQHGww=",
        "h1:pCKjLsAoSAlOhfnDiQfj/iaSD5dh3gakRz4P7v7sv4Q=",
        "h1:tf9ViGy07GKQRHtPpD/q2Y83Jw3wxf6ChpzQvZPRNmE=",
        "h1:zuVK81Q9A3kNE4K/U24O2IqyLZWIQkBrV8uzmrbqYPY=",
        "zh:22aa92d9b7d62aed053a9fda804ed707f613ccf91b142069486d6fe60390d695",
        "zh:2e1c40b509d87b04c7e06db5afef36b433e66219784cc4f89069467504b40a75",
        "zh:33b58d18de6a6bbce957dccd88e667dab8dcce7182f2edc1ca65f9baef2678c4",
        "zh:512be9f8c3b85bb3f2ae9c8e061ab6d8c4c2939a1e2e5189cba36cba08b22dd6",
        "zh:554134e0765d52e5ac98428306afa5733ffea991832ae96e110c752a37426b7e",
        "zh:72483db4a717cc605dcfcc141aa0003993cfced79374b4468d1674d41ecdf171",
        "zh:7353bfb1484b6771145b03becf612921ec11ee1242467b4c1ab6a13936ad306b",
        "zh:76be10c1b4912f5905df73d9c860e2a625e4963a55d5a9b57b24fae4f2c7bb81",
        "zh:890df766e9b839623b1f0437355032a3c006226a6c200cd911e15ee1a9014e9f",
        "zh:9d4d41c5b02ba5d15cdaae0e322f719d33f62c7317bf9547005b8601ef9853a6",
        "zh:b0afb80c8113060ddd555ac8f051ddc8243df101986f851b277831b87a4ad084",
        "zh:bce4c04dc2f06b129d957a83d500b3d70748018c5c2c7ddab2ded12319bf1ace",
        "zh:caa1e4f2087141fa8b35c33b1957b73a8ecc6c507175703ae95509adb3bb5aaa",
        "zh:e9e89b7a308225e649c1015c5bfcbf23feb49e1d6a3d94c9bd230124323bf1a2",
    ),
)
KEY_ENV = "PORKBUN_API_KEY"
SECRET_ENV = "PORKBUN_SECRET_API_KEY"
RECORD_TYPE = "porkbun_dns_record"
#: Porkbun's API v3 (the OpenTofu provider's too).
API = "https://api.porkbun.com/api/json/v3"
#: Seconds to wait for each domain's answer.
CHECK_TIMEOUT = 15.0
CHECK = "porkbun-api-access"
#: Porkbun's answer for a domain without API access (its code, or its message).
_API_DISABLED = re.compile(
    r"API_ACCESS_DISABLED|opted in to API access|API access (?:is )?(?:disabled|not enabled)",
    re.IGNORECASE,
)


def _post(url: str, body: bytes, timeout: float) -> tuple[int, bytes]:
    """POST JSON ``body`` to ``url``: the status and up to 1 MiB of the answer."""
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "piceli"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status), response.read(1 << 20)
    except urllib.error.HTTPError as error:
        with error:
            return int(error.code), error.read(1 << 20)


def _key_pair(credential: str | None) -> tuple[str, str]:
    key, separator, secret = (credential or "").partition(":")
    if not separator or not key or not secret:
        raise InfraError(
            "infra-dns-check-failed",
            "no Porkbun key pair (APIKEY:SECRETKEY) was loaded, so the plan "
            "cannot confirm API access for its domains",
        )
    return key, secret


def api_access(
    domains: Iterable[str],
    credential: str | None,
    *,
    post: Any = None,
    timeout: float = CHECK_TIMEOUT,
) -> dict[str, str]:
    """``{domain: "enabled"}`` once Porkbun answers for each domain with this key pair.

    Raises ``infra-dns-api-disabled`` for a domain without API access and
    ``infra-dns-check-failed`` when the check cannot confirm it. Neither
    error carries the key pair or Porkbun's own message.
    """
    key, secret = _key_pair(credential)
    send = post or _post
    body = json.dumps({"apikey": key, "secretapikey": secret}).encode()
    found: dict[str, str] = {}
    for domain in sorted(set(domains)):
        url = f"{API}/dns/retrieve/{urllib.parse.quote(domain, safe='')}"
        try:
            status, raw = send(url, body, timeout)
        except (OSError, ValueError):  # URLError, timeouts, refused connections
            raise _unconfirmed(domain, "Porkbun's API did not answer") from None
        try:
            answer = json.loads(raw)
        except ValueError:
            answer = None
        if not isinstance(answer, dict):
            raise _unconfirmed(domain, f"Porkbun's API answered HTTP {status}")
        if answer.get("status") == "SUCCESS" and status == 200:
            found[domain] = "enabled"
            continue
        if _API_DISABLED.search(str(answer.get("message") or "")):
            raise InfraError(
                "infra-dns-api-disabled",
                f"Porkbun API access is off for {domain}; turn it on (Domain "
                "Management → Details → API Access), then plan again",
            )
        raise _unconfirmed(
            domain,
            f"Porkbun refused the request (HTTP {status}): check the key pair "
            "and that the domain is in its account",
        )
    return found


def _unconfirmed(domain: str, why: str) -> InfraError:
    return InfraError(
        "infra-dns-check-failed", f"could not confirm API access for {domain}: {why}"
    )


@dataclass(frozen=True)
class Porkbun(Provider):
    """Porkbun's DNS, for domains registered at Porkbun with API access on.

    :param credentials: The local credential holding ``APIKEY:SECRETKEY``
        (``piceli secrets provider NAME --prompt``).
    """

    credentials: str | None = None
    location: str | None = field(default=None, repr=False)

    name = "porkbun"
    supports_servers = False
    #: Porkbun's minimum (and default) TTL.
    default_ttl = 600
    min_ttl = 600

    def __post_init__(self) -> None:
        from piceli.profiles import ProfileError, check_name

        if self.credentials is None:
            raise InfraError(
                "infra-invalid", "Porkbun(credentials=) names the API key's credential"
            )
        try:
            check_name(self.credentials)
        except ProfileError:
            raise InfraError(
                "infra-invalid", "Porkbun(credentials=) must be a credential name"
            ) from None

    # ------------------------------------------------------------ pinning
    def pin(self) -> Pin:
        return PORKBUN_PIN

    def token_env(self) -> str:
        return SECRET_ENV

    def credential_env(self, value: str) -> dict[str, str]:
        key, separator, secret = value.partition(":")
        if not separator or not key or not secret:
            raise InfraError(
                "infra-credential-invalid",
                f"credential {self.credentials!r} is not APIKEY:SECRETKEY",
            )
        return {KEY_ENV: key, SECRET_ENV: secret}

    def provider_block(self) -> dict[str, Any]:
        return {}

    def known_error(self, diagnostics: Sequence[str]) -> InfraError | None:
        if any(_API_DISABLED.search(line) for line in diagnostics):
            return InfraError(
                "infra-dns-api-disabled",
                "Porkbun refused: API access is off for the record's domain; turn it "
                "on (Domain Management → Details → API Access), then plan again",
                diagnostics,
            )
        return None

    def plan_check(
        self, document: Mapping[str, Any], credential: str | None
    ) -> dict[str, Any] | None:
        """API access of each domain whose records the plan changes."""
        domains: set[str] = set()
        for item in document.get("resource_changes") or ():
            if not isinstance(item, dict) or item.get("type") != RECORD_TYPE:
                continue
            change = item.get("change") or {}
            actions = tuple(change.get("actions") or ())
            if not actions or actions in (("no-op",), ("read",)):
                continue
            for side in ("after", "before"):
                domain = (change.get(side) or {}).get("domain")
                if isinstance(domain, str) and domain:
                    domains.add(domain)
        if not domains:
            return {
                "check": CHECK,
                "ran": False,
                "why": "no record changes at Porkbun",
                "domains": {},
            }
        return {"check": CHECK, "ran": True, "domains": api_access(domains, credential)}

    # ------------------------------------------------------------ rendering
    def _no_servers(self) -> NoReturn:
        raise InfraError("infra-invalid", "Porkbun holds DNS records, not servers")

    def render_primary_ip(
        self, ip: PrimaryIp, server: Server, labels: Mapping[str, str]
    ) -> Resource:
        self._no_servers()

    def render_firewall(
        self, firewall: Firewall, labels: Mapping[str, str]
    ) -> list[Resource]:
        self._no_servers()

    def render_server(
        self,
        server: Server,
        *,
        ips: Mapping[str, Resource],
        firewall: Resource | None,
        labels: Mapping[str, str],
        ssh_keys: list[Resource],
    ) -> Resource:
        self._no_servers()

    def render_record(
        self, record: DnsRecord, values: list[str], labels: Mapping[str, str]
    ) -> Resource:
        (single,) = self.render_records(record, values[:1], labels)
        return single

    def render_records(
        self, record: DnsRecord, values: list[str], labels: Mapping[str, str]
    ) -> list[Resource]:
        """One record per answer, named by the answer (a reference or a value).

        A server's answer is the reference to its address, so the resource
        keeps its name when the server is replaced and only its content
        follows the new address.
        """
        subdomain = {"@": ""}.get(record.name, record.name)
        found = []
        for value in values:
            digest = hashlib.sha256(value.encode()).hexdigest()[:8]
            found.append(
                Resource(
                    RECORD_TYPE,
                    f"{record.resource_name}-{digest}",
                    {
                        "domain": record.zone,
                        "subdomain": subdomain,
                        "type": record.type,
                        "content": value,
                        "ttl": record.ttl,
                    },
                )
            )
        return sorted(found, key=lambda item: item.name)

    def address(self, server: Resource, family: str) -> str:
        self._no_servers()

    def server_id(self, server: Resource) -> str:
        self._no_servers()

    # ------------------------------------------------------------ state
    def labels_of(self, values: Mapping[str, Any]) -> Mapping[str, str]:
        return {}

    def ledger_owned(self, type_: str) -> bool:
        # Records carry no labels: the ledger alone says Piceli created one.
        return type_ == RECORD_TYPE
