"""Plain HTTP reaches the in-cluster registry Service, and nothing else.

The controller's mirror copy and the build Job's push talk to
``Registry.in_cluster`` at ``<name>.<namespace>.svc:<port>`` over plain HTTP
inside the cluster network; every other host stays HTTPS.
"""

from __future__ import annotations

import http.client

import pytest

from piceli.artifacts.registry import (
    RegistryEndpoint,
    RegistryError,
    StreamedOciRegistryClient,
)

SERVICE = "piceli-registry.piceli-system.svc"


def test_plain_http_connects_to_the_in_cluster_registry_service() -> None:
    client = StreamedOciRegistryClient(RegistryEndpoint(SERVICE, 5000))
    connection = client._connection("http", SERVICE, 5000)
    assert type(connection) is http.client.HTTPConnection
    assert (connection.host, connection.port) == (SERVICE, 5000)


def test_plain_http_to_loopback_still_works() -> None:
    client = StreamedOciRegistryClient(RegistryEndpoint("127.0.0.1", 5000))
    assert type(client._connection("http", "127.0.0.1", 5000)) is (
        http.client.HTTPConnection
    )


@pytest.mark.parametrize(
    "host",
    [
        "registry.example.com",  # a public name
        "other.piceli-system.svc",  # another Service (a redirect target)
        "10.0.0.5",  # a cluster IP: not a name the mirror pins
    ],
)
def test_plain_http_to_any_other_host_is_refused(host: str) -> None:
    client = StreamedOciRegistryClient(RegistryEndpoint(SERVICE, 5000))
    with pytest.raises(RegistryError, match="plain-http-refused"):
        client._connection("http", host, 5000)
