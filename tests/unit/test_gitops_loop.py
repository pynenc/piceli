"""The controller's pod loop survives a failed poll and retries with backoff."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from piceli.k8s.cli.gitops import _forever


class FlakyController:
    """Polls that time out ``failures`` times, then succeed."""

    def __init__(self, failures: int) -> None:
        self.config = SimpleNamespace(poll_seconds=60)
        self.failures = failures
        self.polls = 0

    def poll_once(self) -> dict[str, Any]:
        self.polls += 1
        if self.polls <= self.failures:
            raise TimeoutError("the API did not answer")
        return {}


def _run(controller: FlakyController, polls: int) -> list[float]:
    clock = {"now": 0.0}
    waits: list[float] = []

    def sleep(seconds: float) -> None:
        waits.append(seconds)
        clock["now"] += seconds

    _forever(
        controller,
        lambda: None,
        stop=lambda: controller.polls >= polls,
        sleep=sleep,
        clock=lambda: clock["now"],
        handle_signals=False,
    )
    return waits


def test_a_timed_out_poll_does_not_end_the_loop() -> None:
    controller = FlakyController(failures=3)
    _run(controller, polls=5)
    assert controller.polls == 5  # it kept polling after three failures


def test_failed_polls_back_off_and_success_resets_the_cadence() -> None:
    waits = _run(FlakyController(failures=3), polls=5)
    # 5 s, 10 s, 20 s after the failures; the poll interval after a success.
    assert waits[:3] == [5.0, 10.0, 20.0]
    assert waits[3] == 60.0


def test_backoff_is_capped_at_five_minutes() -> None:
    controller = FlakyController(failures=20)
    waits = _run(controller, polls=12)
    assert max(waits) == 300.0
