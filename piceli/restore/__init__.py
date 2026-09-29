"""Restore points of retained data: archive the claims a release touches.

Maturity: **preview** (the API may change before 1.0).

``Pipeline(..., restore_points=RestorePoints())`` gives ``piceli deploy`` a
``backup`` stage; ``app.quiesce(workload, Quiesce...)`` declares hooks that
run before the writers stop; ``piceli restore-points`` lists and verifies
them and ``piceli restore`` puts one back. See ``docs/restore_points.md``.

Importing this package reads no file and contacts nothing.
"""

from piceli.restore.model import Quiesce, RestorePointError, RestorePoints

__all__ = ["Quiesce", "RestorePointError", "RestorePoints"]
