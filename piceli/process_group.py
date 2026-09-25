"""Signal a child's process group without ever reaching anything else.

Piceli starts helper processes (``kubectl port-forward``, build tools) in a new
session, so each leads its own process group and ``os.killpg(pid, …)`` stops it
together with anything it forked. A bad ``pid`` would turn that into a signal
for an unrelated group: ``1`` (every process in init's group), ``0`` (our own
group) or our own group id. ``signal_group`` refuses those.
"""

from __future__ import annotations

import os


def signal_group(pid: object, signum: int) -> bool:
    """Send ``signum`` to the process group ``pid`` leads; ``False`` if refused.

    Refuses anything that is not a real child's group: a non-integer (a test
    double's ``pid``), ``pid <= 1``, and the caller's own process group.
    ``OSError`` from ``os.killpg`` (for example ``ProcessLookupError``)
    propagates as before.
    """
    if type(pid) is not int or pid <= 1 or pid == os.getpgrp():
        return False
    os.killpg(pid, signum)
    return True
