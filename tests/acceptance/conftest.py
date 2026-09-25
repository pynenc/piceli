"""Shared fixtures for independently runnable local acceptance modules."""

import pytest

from tests.acceptance.fake_api import provider_at, serve


@pytest.fixture
def local_api():
    with serve() as (api, url):
        provider = provider_at(url)
        yield api, provider
        provider.client.close()


@pytest.fixture
def shop(tmp_path, monkeypatch):
    """A shop Pipeline on the fake API with a fake build/delivery backend."""
    from tests.acceptance.test_deploy_pipeline import make_shop

    yield from make_shop(tmp_path, monkeypatch)
