"""Access declarations in the typed model: validated, pure, never rendered."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from piceli import App, ContainerPort, HealthProbe, ServicePort
from piceli.app.access import Access, Forward
from piceli.k8s.ui_config import RestartPolicy, UiShortcut

from .conftest import canonical

IMAGE = "registry.example/web@sha256:" + "1" * 64


def _app(access: Forward | None = None) -> App:
    app = App("shop")
    web = app.deployment("web", image=IMAGE, ports=[3000])
    app.service(web, port=3000, access=access)
    return app


def test_forward_factory_builds_probes() -> None:
    assert App.access is Access
    assert Access.forward(18080).health == HealthProbe()
    assert Access.forward(18080, health="tcp").health == HealthProbe()
    http = Access.forward(18080, health="/healthz").health
    assert (http.type, http.path, http.expect_status) == (
        "http",
        "/healthz",
        (200, 399),
    )
    probe = HealthProbe(type="http", path="/", expect_status=(200, 499))
    assert Access.forward(18080, health=probe).health is probe
    restart = RestartPolicy(max_restarts=2)
    assert Access.forward(18080, restart=restart).restart == restart
    with pytest.raises(ValueError, match="health must be"):
        Access.forward(18080, health="healthz")
    with pytest.raises(ValidationError):
        Access.forward(0)
    with pytest.raises(ValidationError):
        Access.forward(18080, path="login")
    with pytest.raises(ValidationError):
        Access.forward(18080, name="Not_A_Name")


def test_access_never_changes_the_rendered_manifests() -> None:
    plain = _app().render("shop")
    declared = _app(App.access.forward(local=18080, path="/login")).render("shop")
    assert canonical(declared) == canonical(plain)
    assert "18080" not in canonical(declared)


def test_service_access_renders_to_a_shortcut() -> None:
    app = _app(
        App.access.forward(
            local=18080, path="/login", health="/healthz", label="Web", required=False
        )
    )
    (shortcut,) = app.shortcuts("shop")
    assert isinstance(shortcut, UiShortcut)
    assert shortcut.id == "web"
    assert shortcut.label == "Web"
    assert shortcut.target == "service/web"
    assert shortcut.namespace == "shop"
    assert (shortcut.local_port, shortcut.remote_port) == (18080, 3000)
    assert shortcut.url == "http://127.0.0.1:18080/login"
    assert shortcut.probe.path == "/healthz"
    assert shortcut.required is False
    assert app.shortcuts()[0].namespace is None


def test_named_ports_resolve_to_numbers() -> None:
    app = App("shop")
    api = app.deployment(
        "api",
        image=IMAGE,
        ports=[
            ContainerPort(port=8080, name="http"),
            ContainerPort(port=9090, name="metrics"),
        ],
        access=App.access.forward(local=19090, port="metrics", name="api-metrics"),
    )
    app.service(
        api,
        ports=[ServicePort(port=80, target_port="http", name="http")],
        access=App.access.forward(local=18081, port="http"),
    )
    shortcuts = {item.id: item for item in app.shortcuts("shop")}
    assert shortcuts["api"].target == "service/api"
    assert shortcuts["api"].remote_port == 80
    assert shortcuts["api-metrics"].target == "deployment/api"
    assert shortcuts["api-metrics"].remote_port == 9090


def test_access_port_must_exist() -> None:
    app = App("shop")
    web = app.deployment("web", image=IMAGE, ports=[3000])
    with pytest.raises(ValidationError, match="not one of its ports"):
        app.service(web, port=3000, access=App.access.forward(local=1, port=81))
    with pytest.raises(ValidationError, match="no port to forward to"):
        app.deployment("idle", image=IMAGE, access=App.access.forward(local=2))


def test_ids_and_local_ports_are_unique() -> None:
    app = _app(App.access.forward(local=18080))
    api = app.deployment("api", image=IMAGE, ports=[8080])
    with pytest.raises(ValueError, match="local port 18080 is already used by 'web'"):
        app.service(api, port=8080, access=App.access.forward(local=18080))
    with pytest.raises(ValueError, match="'web' is already declared"):
        app.service(
            api, port=8080, name="api", access=App.access.forward(local=1, name="web")
        )
    app.service(api, port=8080, access=App.access.forward(local=18081))
    assert [item.id for item in app.shortcuts()] == ["web", "api"]
