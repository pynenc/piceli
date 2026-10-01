"""The component contract (``[component.<name>]`` in ``piceli.toml``)."""

from __future__ import annotations

import pytest

from piceli.infra import CompositionError
from piceli.infra.contract import contract_digest, image_contract, parse_contracts

BASE = "docker.io/library/debian@sha256:" + "a" * 64

FULL = f"""
[component.api]
build = {{ rust = "crates/api", bin = "api", page_size = "from node" }}
image = {{ base = "{BASE}", user = 10001, dirs = {{ "/var/lib/api" = "0700" }} }}
ports = {{ http = 18080, graph-tls = 18443 }}
health = {{ ready = "GET /health", check = ["api", "check-config"] }}
upgrade_check = ["api", "store", "verify", "--read-only", "/var/lib/api"]
volumes = {{ data = {{ path = "/var/lib/api", size = "8Gi", retained = true }} }}
needs = ["cache?", "secret:api-token", "component:db"]
settings = {{ memory_soft_limit = "auto", retention = "default" }}
emits = ["otlp:metrics", "otlp:logs"]

[component.site]
build = {{ files = {{ "site" = "/srv/site" }} }}
image = {{ base = "{BASE}", cmd = ["httpd", "-f", "-p", "8080", "-h", "/srv/site"] }}
ports = {{ http = 8080 }}
health = {{ ready = "TCP http" }}
"""


def test_the_full_format_parses() -> None:
    found = parse_contracts(FULL)
    api = found["api"]
    assert api.build is not None and api.build.kind == "rust"
    assert api.build.paths == ("crates/api", "Cargo.toml", "Cargo.lock")
    assert api.image.base_ref == BASE and (api.image.uid, api.image.gid) == (
        10001,
        10001,
    )
    assert dict(api.ports) == {"http": 18080, "graph-tls": 18443}
    assert api.health.ready == ("GET", "/health")
    assert api.health.check == ("api", "check-config")
    assert [str(n) for n in api.needs] == [
        "component:cache?",
        "secret:api-token",
        "component:db",
    ]
    assert api.retained[0].size == "8Gi"
    site = found["site"]
    assert site.build is not None and site.build.files == (("site", "/srv/site"),)
    assert site.health.ready == ("TCP", "http")


IMAGE = f'image = {{ base = "{BASE}" }}\n'
FILES = '[component.a]\nbuild = { files = { "x" = "/x" } }\n' + IMAGE


@pytest.mark.parametrize(
    "text",
    [
        FILES + "port = 1\n",  # unknown key
        '[component.a]\nbuild = { files = { "x" = "/x" }, typo = 1 }\n' + IMAGE,
        '[component.a]\nbuild = { rust = "c" }\nimage = { base = "debian:bookworm-slim" }\n',
        '[component.a]\nbuild = { rust = "../c" }\n' + IMAGE,
        '[component.a]\nbuild = { rust = "c", python = "p" }\n' + IMAGE,
        FILES + 'needs = ["db:x"]\n',
        FILES + 'volumes = { d = { path = "/d", retained = true } }\n',
        FILES + 'upgrade_check = ["x"]\n',
        '[component.a]\nbuild = { rust = "c" }\n',  # rust needs a base
        "[other]\nx = 1\n",
        "not toml [",
    ],
)
def test_invalid_contracts_are_refused(text: str) -> None:
    with pytest.raises(CompositionError) as raised:
        parse_contracts(text)
    assert raised.value.code == "component-contract-invalid"


def test_a_third_party_image_takes_no_build() -> None:
    found = image_contract("cache", {"ports": {"redis": 6379}})
    assert found.build is None and dict(found.ports) == {"redis": 6379}
    with pytest.raises(CompositionError):
        image_contract("cache", {"build": {"files": {"a": "/a"}}})


def test_the_digest_follows_the_build_and_its_trees_only() -> None:
    api = parse_contracts(FULL)["api"]
    trees = {"crates/api": "1" * 40, "Cargo.toml": "2" * 40, "Cargo.lock": "3" * 40}
    first = contract_digest(api, trees)
    assert first == contract_digest(api, dict(reversed(trees.items())))
    assert first != contract_digest(api, {**trees, "crates/api": "4" * 40})
    changed = parse_contracts(
        FULL.replace('retention = "default"', 'retention = "long"')
    )
    assert contract_digest(changed["api"], trees) == first  # settings do not rebuild
