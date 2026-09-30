"""Opt-in for web UI paths that have not passed their release gate.

In-cluster manual delivery (``piceli ui cluster-serve`` with deploy grants),
remote local-client access (``--authorized-access-sub`` and ``piceli ui
connect``) and install manifests that enable either are experimental and
unsupported. They are refused with ``ui-experimental-disabled`` unless the
caller passes ``--experimental`` or sets ``PICELI_UI_EXPERIMENTAL=1``.
"""

from __future__ import annotations

import os

EXPERIMENTAL_ENV = "PICELI_UI_EXPERIMENTAL"


def experimental_enabled(flag: bool = False) -> bool:
    """Whether the caller opted in, by ``flag`` or ``PICELI_UI_EXPERIMENTAL=1``."""
    return flag or os.environ.get(EXPERIMENTAL_ENV) == "1"
