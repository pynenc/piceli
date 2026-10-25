"""0.31.0: ``piceli access`` prints a service by what it speaks: web pages
as links (first, with their label), anything else (TURN, PostgreSQL, a raw
TCP port) marked "not a web page"."""

from __future__ import annotations

from typing import Any

import pytest

from piceli import App
from piceli.app.model import ServicePort
from piceli.k8s.cli.access import _entry_line, _web_first
from piceli.k8s.env_access import service_forwards, tailnet_urls
from piceli.k8s.ui_config import UiShortcut, port_scheme, service_url


@pytest.mark.parametrize(
    ("port", "kw", "scheme"),
    [
        (8080, {}, "http"),
        (3478, {}, "turn"),
        (3478, {"protocol": "UDP"}, "turn"),
        (5432, {}, "postgres"),
        (9999, {"protocol": "UDP"}, "udp"),
        (9999, {"name": "turn-tcp"}, "turn"),
        (9999, {"name": "http-api"}, "http"),
        (9999, {"name": "https"}, "https"),
        (3478, {"app_protocol": "kubernetes.io/h2c"}, "http"),
        (80, {"app_protocol": "turn"}, "turn"),
        (80, {"app_protocol": "example.org/custom"}, "custom"),
    ],
)
def test_the_scheme_of_a_port(port: int, kw: dict[str, Any], scheme: str) -> None:
    assert port_scheme(port, **kw) == scheme


def test_urls_by_scheme() -> None:
    assert service_url("http", "h", 80, "/x") == "http://h:80/x"
    assert service_url("turn", "h", 3478, "/") == "turn:h:3478"
    assert service_url("postgres", "h", 5432, "/") == "postgres://h:5432"
    shortcut = UiShortcut(id="t", label="t", target="service/t", local_port=1,
                          remote_port=3478, scheme="turn")  # fmt: skip
    assert (shortcut.url, shortcut.web) == ("turn:127.0.0.1:1", False)


def test_declared_forwards_take_the_port_s_kind() -> None:
    app = App("shop")
    web = app.deployment("web", image="example.org/web@sha256:" + "0" * 64,
                         ports=[8090])  # fmt: skip
    turn = app.deployment("turn", image="example.org/turn@sha256:" + "0" * 64,
                          ports=[3478])  # fmt: skip
    app.service(web, port=8090,
                access=app.access.forward(local=18090, label="Console"))  # fmt: skip
    app.service(turn, ports=[ServicePort(port=3478, name="turn", app_protocol="turn")],
                access=app.access.forward(local=13478))  # fmt: skip
    rendered = list(app.shortcuts("shop"))
    assert [(s.id, s.scheme, s.url) for s in rendered] == [
        ("web", "http", "http://127.0.0.1:18090/"),
        ("turn", "turn", "turn:127.0.0.1:13478"),
    ]
    forced = app.access.forward(local=1, scheme="tcp")
    assert forced.shortcut("x", "service/x", 3478, None).scheme == "tcp"
    manifests = {
        r.ref.name: r.manifest
        for component in app.render("shop").components
        for r in component.resources
        if r.ref.kind == "Service"
    }
    assert manifests["turn"]["spec"]["ports"][0]["appProtocol"] == "turn"
    assert "appProtocol" not in manifests["web"]["spec"]["ports"][0]


def test_live_services_and_tailnet_names_keep_the_kind() -> None:
    services = [
        {"metadata": {"name": "coturn"}, "spec": {"ports": [{"port": 3478}]}},
        {"metadata": {"name": "web"}, "spec": {"ports": [{"port": 8090}]}},
    ]
    ports = iter(range(41000, 41010))
    found = service_forwards("ns", services, port=lambda: next(ports))
    assert [(s.id, s.scheme) for s in found] == [
        ("coturn-3478", "turn"), ("web-8090", "http")]  # fmt: skip
    companion = {
        "kind": "Service",
        "metadata": {"name": "coturn-tailnet", "labels": {
            "piceli.io/access": "tailnet", "piceli.io/access-service": "coturn"}},
        "spec": {"ports": [{"port": 3478}]},
        "status": {"loadBalancer": {"ingress": [{"hostname": "turn.ts.net"}]}},
    }  # fmt: skip
    (entry,) = tailnet_urls(found, [companion])
    assert (entry["url"], entry["web"]) == ("turn:turn.ts.net:3478", False)


def test_web_pages_first_with_their_label() -> None:
    entries = [
        {"id": "coturn", "label": "coturn:3478", "url": "turn:h:3478",
         "scheme": "turn", "web": False},
        {"id": "console", "label": "Console", "url": "http://h:8090/",
         "scheme": "http", "web": True},
    ]  # fmt: skip
    lines = [_entry_line(e, "tailnet") for e in _web_first(entries)]
    assert lines == [
        "  Console (console): http://h:8090/  (tailnet)",
        "  coturn       turn:h:3478  (tailnet, TURN, not a web page)",
    ]
