"""A small app for the bundle tests: safe for a foreign cluster by default."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from piceli import (
    App,
    ClaimTemplate,
    MemoryVolume,
    PodDefaults,
    Random,
    Resources,
    Rule,
    Secrets,
    SecretVolume,
    Security,
    Static,
    Template,
    TlsCa,
)
from piceli.app.render import rendered

IMAGE = "registry.example/shop/cache@sha256:" + "c" * 64
BUILD = "pending-build.piceli.invalid/web@sha256:" + "0" * 64
RESOURCES = Resources(cpu="10m", memory="16Mi", cpu_limit="100m", memory_limit="32Mi")


def secrets() -> Secrets:
    return Secrets(
        token=Random(32),
        user=Static("shop"),
        url=Template("redis://{user}:{token}@cache:6379/0"),
        tls=TlsCa({"cache": ["cache"]}, common_name="shop test CA", days=30),
    )


def app(*, web_image: str = IMAGE) -> tuple[App, Secrets]:
    generators = secrets()
    shop = App(
        "shop",
        pod_defaults=PodDefaults(
            security=Security.restricted(user=10001, read_only_root_filesystem=True)
        ),
    )
    private = shop.secret(
        "shop-private",
        {
            "token": generators.ref("token"),
            "url": generators.ref("url"),
            "ca.crt": generators.ref("tls.ca.crt"),
            "tls.crt": generators.ref("tls.cache.crt"),
            "tls.key": generators.ref("tls.cache.key"),
        },
    )
    cache = shop.stateful_set(
        "cache",
        image=IMAGE,
        ports=[6379],
        volumes={
            "/data": _claim(),
            "/run/private": SecretVolume(private, default_mode=0o440),
        },
        resources=RESOURCES,
    )
    web = shop.deployment(
        "web",
        image=web_image,
        ports=[8080],
        volumes={"/tmp": MemoryVolume(size_limit="8Mi")},
        resources=RESOURCES,
    )
    shop.service(web, port=8080, access=shop.access.forward(local=18080))
    shop.network_policy(web, egress=[cache], allow_dns=True)
    shop.service_account(
        "reader",
        cluster_rules=[Rule(resources=["nodes"], verbs=["get", "list"])],
    )
    return shop, generators


def _claim() -> ClaimTemplate:
    return ClaimTemplate("data", size="1Gi")


def components(
    shop: App, generators: Secrets, namespace: str = "shop"
) -> list[dict[str, Any]]:
    """The render ``piceli render`` would print for ``shop``."""
    inputs = {name: generators.ref(name) for name in generators.inputs()}
    return rendered(shop.render(namespace), inputs)


def manifests(items: list[dict[str, Any]]) -> list[Mapping[str, Any]]:
    return [
        resource["manifest"]
        for component in items
        for resource in component["resources"]
    ]
