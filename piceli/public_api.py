"""The modules Piceli documents as its Python API.

Code written against Piceli imports from these. Each is a package facade
that re-exports names defined in internal modules, so those may move. The
API reference (``docs/conf.py``) documents them, the eval harness checks
their exports name by name, and ``tests/unit/test_public_api.py`` fails when
a guide, an example or the agent skill imports a module missing here. Other
``piceli.*`` modules are internal and may change in any release.

Importing this module is side-effect free.
"""

from __future__ import annotations

PUBLIC_MODULES: tuple[str, ...] = (
    "piceli",
    "piceli.app",
    "piceli.artifacts",
    "piceli.checks",
    "piceli.envs",
    "piceli.infra",
    "piceli.k8s",
    "piceli.k8s.ops",
    "piceli.k8s.release_spec",
    "piceli.k8s.templates",
    "piceli.pipeline",
    "piceli.server.cluster_install",
    "piceli.testing",
    "piceli.train",
)
