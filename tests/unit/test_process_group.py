import os
import signal
import subprocess
import sys
from unittest.mock import MagicMock

from piceli.process_group import signal_group


def test_refuses_anything_but_a_child_group() -> None:
    # A MagicMock pid converts to 1: it must never reach os.killpg.
    assert signal_group(MagicMock().pid, signal.SIGTERM) is False
    assert signal_group(1, signal.SIGTERM) is False
    assert signal_group(0, signal.SIGTERM) is False
    assert signal_group(-5, signal.SIGTERM) is False
    assert signal_group(True, signal.SIGTERM) is False
    assert signal_group(os.getpgrp(), signal.SIGTERM) is False


def test_signals_a_child_session() -> None:
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    try:
        assert signal_group(child.pid, signal.SIGTERM) is True
        assert child.wait(timeout=10) == -signal.SIGTERM
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
