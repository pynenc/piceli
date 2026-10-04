"""The local profile picker exposes names only and switches after CSRF review."""

from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from piceli.profiles import Profile, ProfileError, ResolvedCredentials
from piceli.server.app import create_app
from piceli.services.query import QueryService


def test_local_profile_names_and_switch_boundary(tmp_path: Path, monkeypatch) -> None:
    config = tmp_path / "credentials"
    config.write_text("fake fixture, never returned")
    (tmp_path / "index.html").write_text("<!doctype html><title>Piceli</title>")
    profiles = [Profile("alpha", config, "fake"), Profile("beta", config, "fake")]
    monkeypatch.setattr("piceli.profiles.list_profiles", lambda: profiles)

    def resolve(name: str) -> ResolvedCredentials:
        if name not in {"alpha", "beta"}:
            raise ProfileError("missing", code="profile-not-found")
        return ResolvedCredentials(config, "fake")

    monkeypatch.setattr("piceli.profiles.resolve", resolve)
    selected: list[str] = []
    app = create_app(
        QueryService([]),
        static_dir=tmp_path,
        active_profile="alpha",
        profile_switch=selected.append,
    )
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert client.get("/api/v1/profiles").status_code == 403
        assert (
            client.get(f"/?token={app.state.security.launch_token}").status_code == 200
        )
        listed = client.get("/api/v1/profiles").json()
        assert listed == {
            "active": "alpha",
            "profiles": [
                {"name": "alpha", "available": True},
                {"name": "beta", "available": True},
            ],
            # Other profiles' read-only scopes (0.14.7), none added yet.
            "scopes": [],
        }
        assert str(config) not in str(listed)
        assert "fake fixture" not in str(listed)
        assert (
            client.post("/api/v1/profiles/switch", json={"name": "beta"}).status_code
            == 403
        )
        csrf = next(
            value
            for name, value in client.cookies.items()
            if name.startswith("piceli_csrf_")
        )
        headers = {"Origin": "http://127.0.0.1:8000", "X-Piceli-CSRF": csrf}
        assert (
            client.post(
                "/api/v1/profiles/switch", json={"name": "beta"}, headers=headers
            ).status_code
            == 202
        )
        assert selected == ["beta"]
        missing = client.post(
            "/api/v1/profiles/switch", json={"name": "missing"}, headers=headers
        )
        assert (
            missing.status_code == 404 and missing.json()["code"] == "profile-not-found"
        )


def test_ui_serve_profile_selects_explicit_target_without_ambient_context(
    tmp_path: Path, monkeypatch
) -> None:
    from piceli.k8s.cli import app as cli
    from piceli.profiles import save_profile

    monkeypatch.setenv("PICELI_PROFILES_DIR", str(tmp_path / "profiles"))
    seen: list[tuple[Path, str, str]] = []

    def run(server) -> None:
        registration = server.config.app.state.service.registrations["my-app"]
        seen.append(
            (
                registration.target.kubeconfig,
                registration.target.context,
                registration.target.namespace,
            )
        )

    monkeypatch.setattr("uvicorn.Server.run", run)
    config = tmp_path / "config"
    config.write_text(
        "apiVersion: v1\nkind: Config\nclusters:\n- name: c\n  cluster: {server: 'https://203.0.113.9'}\n"
        "users:\n- name: u\n  user: {token: fixture}\ncontexts:\n"
        "- name: fake\n  context: {cluster: c, user: u}\n"
    )
    save_profile("alpha", config, "fake")
    result = CliRunner().invoke(
        cli,
        ["ui", "serve", "--profile", "alpha", "--state-dir", str(tmp_path / "state")],
    )
    assert result.exit_code == 0
    assert seen == [(config, "fake", "default")]
