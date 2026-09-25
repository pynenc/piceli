"""A pytest plugin with a ready-made fake-cluster fixture.

Enable it in a ``conftest.py``::

    pytest_plugins = ["piceli.testing.pytest_plugin"]

    def test_settings(piceli_fake_cluster):
        piceli_fake_cluster.api.put(...)

Importing this module imports ``pytest``, so only load it from tests.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from piceli.testing.fake_api import FakeCluster, fake_cluster


@pytest.fixture
def piceli_fake_cluster() -> Iterator[FakeCluster]:
    """A running fake API server with a provider, stopped after the test."""
    with fake_cluster() as cluster:
        yield cluster
