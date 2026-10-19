"""Lazy re-exports for the public package facades.

A facade (``piceli.k8s``, ``piceli.artifacts``, ...) lists names defined in
internal modules. Importing those modules eagerly would make ``import
piceli.k8s`` load the Kubernetes client or start import cycles, so a facade
maps each such name to its module and loads it on first access.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Mapping
from typing import Any


def lazy_exports(
    package: str, names: Mapping[str, str], namespace: dict[str, Any]
) -> tuple[Callable[[str], Any], Callable[[], list[str]]]:
    """``__getattr__`` and ``__dir__`` for ``package`` loading ``names``
    (``{name: module}``) on first use and caching them in ``namespace``."""

    def __getattr__(name: str) -> Any:
        module = names.get(name)
        if module is None:
            raise AttributeError(f"module {package!r} has no attribute {name!r}")
        value = getattr(importlib.import_module(module), name)
        namespace[name] = value
        return value

    def __dir__() -> list[str]:
        return sorted(set(namespace) | set(names))

    return __getattr__, __dir__
