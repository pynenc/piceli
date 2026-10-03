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
