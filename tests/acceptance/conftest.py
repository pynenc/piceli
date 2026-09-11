"""Shared fixtures for independently runnable local acceptance modules."""

import pytest

from tests.acceptance.fake_api import provider_at, serve


@pytest.fixture
def local_api():
    with serve() as (api, url):
        provider = provider_at(url)
        yield api, provider
        provider.client.close()
