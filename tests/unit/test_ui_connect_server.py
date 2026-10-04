"""``piceli ui connect --server``: https, or plain http only on loopback."""

from __future__ import annotations

import pytest

from piceli.k8s.cli.ui_remote import RemoteClientError, _server_url


@pytest.mark.parametrize(
    "server",
    [
        "http://127.0.0.1:8790/",
        "http://127.0.0.1:8790",
        "http://localhost:8790/",
        "http://[::1]:8790/",
        "https://ui.example/piceli",
    ],
)
def test_https_or_loopback_http_is_accepted(server: str) -> None:
    assert _server_url(server, allow_insecure_loopback_test=False).endswith("/")


@pytest.mark.parametrize(
    "server",
    [
        "http://192.0.2.10:8790/",
        "http://ui.example/",
        "http://localhost.example/",
        "ftp://127.0.0.1/",
        "http://user:pw@127.0.0.1:8790/",
    ],
)
def test_plain_http_elsewhere_is_refused(server: str) -> None:
    with pytest.raises(RemoteClientError) as refused:
        _server_url(server, allow_insecure_loopback_test=False)
    assert refused.value.code == "ui-invalid-request"


def test_connect_without_the_ui_extra_names_it(monkeypatch, tmp_path) -> None:
    """Without the ui extra (no HTTP client) the refusal says what to install."""
    import builtins

    from typer.testing import CliRunner

    from piceli.k8s.cli import app

    real_import = builtins.__import__

    def no_httpx2(name, *args, **kwargs):  # type: ignore[no-untyped-def]
        if name == "httpx2":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("apiVersion: v1\nkind: Config\n")
    monkeypatch.setattr(builtins, "__import__", no_httpx2)
    result = CliRunner().invoke(
        app,
        ["ui", "connect", "--server", "http://127.0.0.1:8790", "--ticket", "a" * 32,
         "--kubeconfig", str(kubeconfig), "--context", "c", "--local-port", "18081"],
        input="secret\n",
    )  # fmt: skip
    assert result.exit_code != 0
    assert "ui-assets-unavailable" in result.output
    assert "piceli[ui]" in result.stderr
