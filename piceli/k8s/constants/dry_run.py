from enum import Enum
from typing import Optional


class DryRun(Enum):
    """dry_run accepted options"""

    OFF: Optional[str] = None
    ON: Optional[str] = "All"
