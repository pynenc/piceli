"""The repository's temporary-file and process-group guards, for the eval harness.

The mock runs build sandboxes (``piceli-eval-*``) and run ``piceli`` commands in
them: every temporary entry must be gone when a test ends, as in ``tests/``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.conftest import (  # noqa: E402,F401
    _no_foreign_process_groups,
    _no_leftover_temporary_files,
    _no_leftover_temporary_files_per_test,
)
