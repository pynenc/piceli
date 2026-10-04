"""The GitOps controller's heartbeat (0.16.0).

``controller.last_poll`` in the status is written when a poll starts, so it
stands still while one step runs: an 18-minute build read as a stopped
controller. A :class:`Heartbeat` thread calls the controller's ``beat()``
every :data:`HEARTBEAT_SECONDS` while the process lives, also during builds
and deploys. ``beat()`` publishes only ``controller.heartbeat_at`` (a
read-modify-write of the status document: no history, no state), under the
lock the controller's own publish holds, so the two never interleave.
:mod:`piceli.gitops.liveness` reads ``heartbeat_at`` for ``stale`` when the
status has it, else ``last_poll``.

Importing this module is side-effect free (the thread starts in
:meth:`Heartbeat.start`).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from types import TracebackType

#: Seconds between two heartbeats.
HEARTBEAT_SECONDS = 30


class Heartbeat:
    """Call ``beat`` every ``seconds`` on a daemon thread until :meth:`stop`.

    A failing ``beat`` is reported to ``log`` by its type and never ends the
    thread.
    """

    def __init__(
        self,
        beat: Callable[[], None],
        *,
        seconds: float = HEARTBEAT_SECONDS,
        log: Callable[[str], None] = lambda _: None,
    ) -> None:
        self.beat = beat
        self.seconds = seconds
        self.log = log
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> Heartbeat:
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._loop, name="piceli-gitops-heartbeat", daemon=True
            )
            self._thread.start()
        return self

    def _loop(self) -> None:
        while not self._stopping.wait(self.seconds):
            try:
                self.beat()
            except Exception as error:  # never end the heartbeat on one failure
                self.log(f"heartbeat not published ({type(error).__name__})")

    def stop(self) -> None:
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.seconds))
            self._thread = None

    def __enter__(self) -> Heartbeat:
        return self.start()

    def __exit__(
        self,
        kind: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()
