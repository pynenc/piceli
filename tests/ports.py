"""Loopback ports for tests that bind them later, or in another process.

A free port picked with ``bind(("127.0.0.1", 0))`` is an *ephemeral* port:
once that socket closes, the kernel can hand it out again as the source port
of any connection (a probe of it can even connect to itself), and a parallel
test can be given the same free port before either binds it. On a loaded
runner the gap between picking a port and a fake ``kubectl`` binding it lasts
seconds, and the test then talks to another process.

:func:`worker_port` hands out ports below the ephemeral range, where only an
explicit bind takes a port, from a slice that belongs to this pytest-xdist
worker alone, never the same port twice in a row. A listener that does not
need a known port up front binds port 0 itself and reports the port it got
(:data:`LISTENER`).
"""

from __future__ import annotations

import itertools
import os
import random
import socket
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path

SLICE = 64  # ports per pytest-xdist worker

# Another program's listener: it binds a port the kernel picks, prints it,
# and holds it (run it with ``python -c LISTENER``).
LISTENER = """import socket
import time

with socket.socket() as server:
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    print(server.getsockname()[1], flush=True)
    time.sleep(120)
"""


def ephemeral_ports() -> tuple[int, int]:
    """The range the kernel picks source ports and ``bind(0)`` ports from."""
    try:
        low, high = Path("/proc/sys/net/ipv4/ip_local_port_range").read_text().split()
        return int(low), int(high)
    except (OSError, ValueError):
        return 49152, 65535  # IANA's range: macOS and the BSDs


def _slice() -> range:
    low, _high = ephemeral_ports()
    worker = os.environ.get("PYTEST_XDIST_WORKER", "gw0").removeprefix("gw")
    slices = (low - low // 2) // SLICE
    start = low // 2 + (int(worker) if worker.isdigit() else 0) % slices * SLICE
    return range(start, start + SLICE)


def _rotation() -> Iterator[int]:
    ports = _slice()
    first = random.randrange(len(ports))  # a fresh process starts anywhere
    return itertools.cycle([*ports[first:], *ports[:first]])


_ports = _rotation()


def worker_port() -> int:
    """A free loopback port of this worker's slice, below the ephemeral range.

    Successive calls go round the slice, so the ports one test asks for are
    distinct, and a port is offered again only after the whole slice.
    """
    for _ in range(SLICE):
        port = next(_ports)
        with socket.socket() as probe, suppress(OSError):
            probe.bind(("127.0.0.1", port))
            return port
    raise AssertionError("no free port in this worker's slice")
