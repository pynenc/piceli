"""Test helpers for code that drives Piceli: a fake Kubernetes API server.

Maturity: **preview**, public and covered by semantic versioning from 0.4.0.
See ``docs/testing.md``.

Everything is re-exported lazily from :mod:`piceli.testing.fake_api`, so
``import piceli.testing`` is cheap and has no side effects (no server starts,
no socket opens, no kubeconfig is read). A server runs only inside
:func:`serve` or :func:`fake_cluster`.

Example::

    from piceli.testing import fake_cluster, manifest

    def test_my_release(tmp_path):
        with fake_cluster() as cluster:
            cluster.api.put(manifest("ConfigMap", "settings"))
            kubeconfig = cluster.kubeconfig(tmp_path / "kubeconfig")
            ...  # run your code against the explicit kubeconfig
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__all__ = [
    "TARGET",
    "TYPES",
    "FakeAPI",
    "FakeCluster",
    "fake_cluster",
    "field_paths",
    "fields_v1",
    "manifest",
    "paths_of",
    "provider_at",
    "serve",
    "value_at",
    "write_kubeconfig",
]

if TYPE_CHECKING:
    from piceli.testing.fake_api import (
        TARGET,
        TYPES,
        FakeAPI,
        FakeCluster,
        fake_cluster,
        field_paths,
        fields_v1,
        manifest,
        paths_of,
        provider_at,
        serve,
        value_at,
        write_kubeconfig,
    )


def __getattr__(name: str) -> Any:
    if name in __all__:
        from piceli.testing import fake_api

        return getattr(fake_api, name)
    raise AttributeError(f"module 'piceli.testing' has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted([*globals(), *__all__])
