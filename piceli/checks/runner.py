"""Run checks: :func:`run_checks` → :class:`CheckReport`."""

from __future__ import annotations

import json
import math
import operator
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from piceli.checks.context import CheckContext, CheckError, CheckFailed, http_get
from piceli.checks.model import (
    Check,
    CheckSpecError,
    ExecCheck,
    HttpCheck,
    MetricCheck,
    PythonCheck,
    as_checks,
)

_DETAIL = 500
_OPERATORS: dict[str, Callable[[float, float], bool]] = {
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
    "==": operator.eq,
    "!=": operator.ne,
}


@dataclass(frozen=True)
class CheckResult:
    """The outcome of one check.

    :param name: The check's name (declared or derived).
    :param passed: Whether it passed.
    :param detail: One short, printable line: what was observed.
    :param duration: Seconds spent, over every attempt.
    :param type: ``http``, ``exec``, ``metric`` or ``python``.
    :param attempts: Attempts made (at most ``retries + 1``).
    :param code: ``None`` when passed, else a fixed error code such as
        ``check-failed`` or ``check-forward-unavailable``.
    """

    name: str
    passed: bool
    detail: str
    duration: float
    type: str = ""
    attempts: int = 1
    code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "passed": self.passed,
            "detail": self.detail,
            "duration": self.duration,
            "attempts": self.attempts,
            "code": self.code,
        }


@dataclass(frozen=True)
class CheckReport:
    """Every check's result; ``passed`` only when all of them passed.

    An empty report (no checks) passes.
    """

    passed: bool
    results: list[CheckResult] = field(default_factory=list)

    @property
    def failed(self) -> list[CheckResult]:
        return [item for item in self.results if not item.passed]

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "failed": [item.name for item in self.failed],
            "results": [item.to_dict() for item in self.results],
        }


def _short(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _DETAIL else text[: _DETAIL - 1] + "…"


def _http(check: HttpCheck, context: CheckContext) -> str:
    response = context.http_get(
        check.target, check.path, port=check.port, timeout=check.timeout
    )
    low, high = check.expect
    if not low <= response.status <= high:
        wanted = str(low) if low == high else f"{low}-{high}"
        raise CheckError(
            "check-failed",
            f"GET {check.path} returned {response.status}, expected {wanted}",
        )
    if check.body_contains is not None and check.body_contains not in response.text:
        raise CheckError(
            "check-failed",
            f"GET {check.path} returned {response.status} without the expected text",
        )
    return f"GET {check.path} returned {response.status}"


def _exec(check: ExecCheck, context: CheckContext) -> str:
    result = context.exec(
        check.target, check.command, container=check.container, timeout=check.timeout
    )
    if result.exit_code != check.expect_exit:
        raise CheckError(
            "check-failed",
            f"{check.command[0]} exited {result.exit_code}, expected "
            f"{check.expect_exit}",
        )
    if check.output_contains is not None and check.output_contains not in result.stdout:
        raise CheckError(
            "check-failed", f"{check.command[0]} output lacks the expected text"
        )
    return f"{check.command[0]} exited {result.exit_code}"


def _samples(document: Any) -> list[float]:
    if not isinstance(document, dict) or document.get("status") != "success":
        raise CheckError(
            "check-metric-invalid", "response is not a successful Prometheus query"
        )
    data = document.get("data")
    if not isinstance(data, dict):
        raise CheckError("check-metric-invalid", "response has no data")
    kind, result = data.get("resultType"), data.get("result")
    try:
        if kind == "scalar" and isinstance(result, list):
            return [float(result[1])]
        if kind == "vector" and isinstance(result, list):
            return [float(item["value"][1]) for item in result]
    except (KeyError, IndexError, TypeError, ValueError):
        pass
    raise CheckError(
        "check-metric-invalid", f"unsupported result type {kind!r} (vector or scalar)"
    )


def _metric(check: MetricCheck, context: CheckContext) -> str:
    with context.forward(check.target, check.port) as url:
        response = http_get(
            url,
            f"{check.path}?query={quote(check.query, safe='')}",
            timeout=check.timeout,
        )
    if response.status != 200:
        raise CheckError(
            "check-metric-invalid", f"query returned HTTP {response.status}"
        )
    try:
        document = json.loads(response.body)
    except ValueError:
        raise CheckError("check-metric-invalid", "response is not JSON") from None
    values = _samples(document)
    if not values:
        if check.empty == "pass":
            return "no samples (empty = pass)"
        raise CheckError("check-failed", "the query returned no samples")
    compare = _OPERATORS[check.op]
    bad = [
        value
        for value in values
        if math.isnan(value) or not compare(value, check.threshold)
    ]
    if bad:
        raise CheckError(
            "check-failed",
            f"{len(bad)} of {len(values)} samples fail {check.op} "
            f"{check.threshold:g} (e.g. {bad[0]:g})",
        )
    return f"{len(values)} samples {check.op} {check.threshold:g}"


def _python(check: PythonCheck, context: CheckContext) -> str:
    function = check.resolve(context.base)
    try:
        outcome = function(context)
    except (CheckFailed, CheckError):
        raise
    except Exception as error:
        raise CheckError(
            "check-raised", f"{check.entry} raised {type(error).__name__}"
        ) from None
    if outcome is None or outcome is True:
        return f"{check.entry} passed"
    if outcome is False:
        raise CheckError("check-failed", f"{check.entry} returned False")
    if isinstance(outcome, str):
        raise CheckError("check-failed", outcome)
    raise CheckError(
        "check-raised",
        f"{check.entry} returned {type(outcome).__name__}; return None, a bool "
        "or a failure string",
    )


def _attempt(check: Check, context: CheckContext) -> str:
    if isinstance(check, HttpCheck):
        return _http(check, context)
    if isinstance(check, ExecCheck):
        return _exec(check, context)
    if isinstance(check, MetricCheck):
        return _metric(check, context)
    return _python(check, context)


def run_check(
    check: Check,
    context: CheckContext,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> CheckResult:
    """Run one check with its retries; never raises for a failing check."""
    started = time.monotonic()
    attempts = 0
    code: str | None = None
    detail = ""
    for attempt in range(check.retries + 1):
        attempts = attempt + 1
        try:
            detail = _attempt(check, context)
            code = None
            break
        except CheckFailed as failure:
            code, detail = "check-failed", failure.detail
        except CheckError as error:
            code, detail = error.code, error.detail
        except CheckSpecError as error:
            code, detail = error.code, str(error).split(": ", 1)[-1]
            break  # a declaration error cannot improve by retrying
        except Exception as error:  # never let one check abort the report
            code, detail = "check-raised", f"{type(error).__name__} while checking"
        if attempt < check.retries:
            sleep(check.interval)
    return CheckResult(
        check.label,
        code is None,
        _short(detail),
        round(time.monotonic() - started, 3),
        check.type,
        attempts,
        code,
    )


def run_checks(
    checks: Check | Iterable[Check] | None,
    context: CheckContext,
    *,
    fail_fast: bool = False,
    sleep: Callable[[float], None] = time.sleep,
) -> CheckReport:
    """Run ``checks`` in order against ``context`` and report every result.

    :param checks: One check, several, or ``None`` (an empty, passing report).
    :param context: Where the checks run; the caller owns (and closes) it.
    :param fail_fast: Stop after the first failing check.
    :param sleep: Replaces :func:`time.sleep` between attempts (tests).

    A failing check never raises: it is a :class:`CheckResult` with
    ``passed=False`` and a fixed ``code``. Duplicate names raise
    :class:`~piceli.checks.CheckSpecError` before anything runs.
    """
    items = as_checks(checks)
    results: list[CheckResult] = []
    for check in items:
        result = run_check(check, context, sleep=sleep)
        results.append(result)
        if fail_fast and not result.passed:
            break
    return CheckReport(all(item.passed for item in results), results)
