"""Shared strict wire validation and bounded calls for untrusted providers."""

from __future__ import annotations

import json
import math
import re
import threading
import time
from datetime import datetime, timezone
from queue import Empty, Queue
from typing import Any, Callable, TypeVar

T = TypeVar("T")
_CALL_SLOTS = threading.BoundedSemaphore(4)


def text(value: Any, name: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ValueError(f"{name} must be a string")
    if len(value) > 4096 or any(ord(c) < 32 for c in value):
        raise ValueError(f"invalid {name}")
    return value


def positive(value: Any, name: str, maximum: int) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 0 < value <= maximum
    ):
        raise ValueError(f"invalid {name} budget")
    return value


def seconds(value: Any, name: str, maximum: float = 3600) -> float:
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or not 0 < value <= maximum
    ):
        raise ValueError(f"invalid {name} deadline")
    return float(value)


def timestamp(value: Any, *, allow_future: bool = False) -> datetime:
    text(value, "timestamp")
    if not re.fullmatch(
        r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)", value
    ):
        raise ValueError("timestamp must be RFC3339 with timezone")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not allow_future and result > datetime.now(timezone.utc):
        raise ValueError("timestamp cannot be in the future")
    return result


def object_keys(
    value: Any, required: set[str], optional: frozenset[str] = frozenset()
) -> None:
    if (
        not isinstance(value, dict)
        or not required <= value.keys()
        or value.keys() - required - optional
    ):
        raise ValueError("invalid object fields")


def strict_json(value: str, max_bytes: int = 10_000_000) -> Any:
    if not isinstance(value, str) or len(value.encode()) > max_bytes:
        raise ValueError("JSON exceeds byte budget")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, child in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = child
        return result

    def invalid(_: str) -> Any:
        raise ValueError("non-finite JSON number")

    try:
        return json.loads(value, object_pairs_hook=pairs, parse_constant=invalid)
    except (RecursionError, json.JSONDecodeError) as error:
        raise ValueError("invalid JSON") from error


def bounded_call(
    call: Callable[[], T],
    deadline: float,
    *,
    slots: threading.BoundedSemaphore = _CALL_SLOTS,
) -> T:
    """At most four outstanding calls, including timed-out uncooperative calls.

    A timed-out mutation is ambiguous: callers must journal before invoking it
    and observe before any retry. Threads cannot forcibly interrupt third-party IO.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not slots.acquire(blocking=False):
        raise TimeoutError("provider deadline or capacity exhausted")
    result: Queue[tuple[bool, Any]] = Queue(maxsize=1)

    def run() -> None:
        try:
            result.put((True, call()))
        except BaseException as error:
            result.put((False, error))
        finally:
            slots.release()

    threading.Thread(target=run, daemon=True).start()
    try:
        ok, value = result.get(timeout=max(0, deadline - time.monotonic()))
    except Empty:
        raise TimeoutError("provider deadline exceeded") from None
    if not ok:
        raise value
    return value
