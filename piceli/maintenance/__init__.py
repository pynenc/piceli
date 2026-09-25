"""Runner hygiene: disk used by Piceli (``piceli cache``) and runner checks
(``piceli doctor``).

* :mod:`piceli.maintenance.cache`: what the state directories and Piceli's
  temporary directories hold, and a prune that never removes the active
  release's state, approved plans, the secret store or anything a rollback
  of the last releases needs.
* :mod:`piceli.maintenance.doctor`: free disk and memory against what the
  next build needs, and the external tools a pipeline uses.

Importing this package is side-effect free.
"""

from piceli.maintenance.cache import (
    CacheError,
    StateDir,
    parse_size,
    prune,
    state_dirs,
    status,
)

__all__ = [
    "CacheError",
    "StateDir",
    "parse_size",
    "prune",
    "state_dirs",
    "status",
]
