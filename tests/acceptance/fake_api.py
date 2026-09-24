"""Compatibility shim: the fake API server now ships as :mod:`piceli.testing`.

Kept so existing tests keep importing ``tests.acceptance.fake_api``; new tests
import from ``piceli.testing`` instead.
"""

from piceli.testing.fake_api import *  # noqa: F403
from piceli.testing.fake_api import (  # noqa: F401
    _MISSING,
    FakeAPI,
    Path,
    _merge_patch,
    _prune,
    _remove_path,
    _segments,
    _ssa_merge,
)
