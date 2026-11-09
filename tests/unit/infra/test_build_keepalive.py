"""0.70.0: a build host renews its claim on a timer while it builds, also
when the build prints nothing (a claim lapsed after 10 silent minutes)."""

from __future__ import annotations

import threading
import time
from typing import Any

from piceli.infra import builds
from piceli.infra.remote_builds import live_claim, progress

BUILD = "0123456789abcdef0123"


class Channel:
    def __init__(self) -> None:
        self.requests: list[tuple[Any, ...]] = []
        self.lock = threading.Lock()

    def add_request(self, *request: Any) -> None:
        with self.lock:
            self.requests.append(request)


def test_a_silent_build_still_reports_while_it_runs() -> None:
    channel = Channel()
    reporter = builds.Progress(channel, BUILD, "builder-1", clock=time.time)
    reporter.current = "build"
    with reporter.alive(every=0.02):
        time.sleep(0.15)  # the build prints nothing
    sent = len(channel.requests)
    assert sent >= 3
    time.sleep(0.06)
    assert len(channel.requests) == sent  # stops with the build


def test_the_keepalive_stops_when_the_build_fails() -> None:
    channel = Channel()
    reporter = builds.Progress(channel, BUILD, "builder-1", clock=time.time)
    try:
        with reporter.alive(every=0.02):
            raise RuntimeError("the build failed")
    except RuntimeError:
        pass
    sent = len(channel.requests)
    time.sleep(0.06)
    assert len(channel.requests) == sent
    assert not any(t.name == "build-keepalive" for t in threading.enumerate())


def test_each_report_keeps_the_claim_alive() -> None:
    state: dict[str, Any] = {
        "host_builds": {
            "b1": {"claimed": {"host": "builder-1", "epoch": 0.0}, "images": ["api"]}
        }
    }
    entry = state["host_builds"]["b1"]
    assert live_claim(entry, 700.0) is None  # 10 min without a report
    body = {"id": "b1", "host": "builder-1", "stage": "build", "log_tail": ""}
    for at in (120.0, 240.0, 360.0, 480.0, 600.0):  # every 2 min, no new lines
        progress(state, body, at)
    assert live_claim(entry, 700.0) is not None
