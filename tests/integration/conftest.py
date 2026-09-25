"""Fixtures for the opt-in kind tests (see ``kind_support``)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from kind_support import kubectl


@pytest.fixture
def kind_namespace() -> Iterator[str]:
    name = "m2-" + uuid.uuid4().hex[:8]
    kubectl("create", "namespace", name)
    try:
        yield name
    finally:
        kubectl("delete", "namespace", name, "--wait=false", check=False)
