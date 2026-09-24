"""Post-deploy checks: prove a release works, not only that its pods are ready.

Maturity: **preview** (the API may change before 1.0).

Four check types, each usable from Python (:class:`Checks`) and from
``release.toml`` (``[[checks]]``):

* ``http`` — GET a path through a temporary supervised loopback port forward;
* ``exec`` — run a command in a ready pod through the Kubernetes API;
* ``metric`` — query a Prometheus-compatible endpoint and compare the samples;
* ``python`` — call a function with the :class:`CheckContext`.

Example::

    from pathlib import Path
    from piceli.checks import CheckContext, Checks, run_checks

    checks = [Checks.http("service/web", "/login", expect=200)]
    with CheckContext(Path("kubeconfig"), "kind-shop", "shop", "shop-1") as ctx:
        report = run_checks(checks, ctx)
    assert report.passed, report.to_dict()

``piceli release apply`` runs the spec's checks after readiness; see
``docs/checks.md``. Importing this package has no side effects.
"""

from piceli.checks.context import (
    CheckContext,
    CheckError,
    CheckFailed,
    ExecResult,
    HttpResponse,
)
from piceli.checks.model import (
    Check,
    Checks,
    CheckSpecError,
    ExecCheck,
    HttpCheck,
    MetricCheck,
    PythonCheck,
    parse_checks,
)
from piceli.checks.runner import CheckReport, CheckResult, run_check, run_checks

__all__ = [
    "Check",
    "CheckContext",
    "CheckError",
    "CheckFailed",
    "CheckReport",
    "CheckResult",
    "CheckSpecError",
    "Checks",
    "ExecCheck",
    "ExecResult",
    "HttpCheck",
    "HttpResponse",
    "MetricCheck",
    "PythonCheck",
    "parse_checks",
    "run_check",
    "run_checks",
]
