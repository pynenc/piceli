"""The error-code registry covers every fixed code the source can emit."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import piceli
from piceli.errors import AREAS, ERRORS, ErrorCode, lookup

SOURCE = Path(piceli.__file__).resolve().parent
CODE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")

# Constructors whose first positional argument is a fixed error code.
FIRST_ARGUMENT = {
    "DeliveryInputError",
    "BuildSpecError",
    "BuildContextError",
    "ProviderError",
    "RegistryError",
    "_input",
    "SecretError",
    "ImageHandoffError",
    "CheckError",
    "CheckSpecError",
    "reject",
    "fail",
    "rejection",
    "_result_error",
}
# ``_Failure(result, reason)`` in the delivery modules; ``reject_error(error, default)``;
# ``DryRunUnavailable(resource, reason)`` in ``k8s/ops/dry_run.py``.
SECOND_ARGUMENT = {"_Failure", "reject_error", "DryRunUnavailable"}
# ``rejecting((ExceptionTypes, "default-code"), ...)`` in the CLI modules.
RULES = {"rejecting"}

# Codes built at runtime, which a literal scan cannot see. Keep this list next
# to the code that builds them.
DYNAMIC = {
    # delivery_inputs.discover_tool: f"{name}-tool-required"
    "docker-tool-required",
    "ssh-tool-required",
    "kubectl-tool-required",
    # delivery.NodeDelivery: "import-" + RunResult.state
    "import-failed",
    "import-timed-out",
    "import-cancelled",
    "import-output-limit",
    # registry_delivery._read_source(failure=...)
    "invalid-archive",
    "invalid-image-stream",
    "source-changed",
    # kubernetes_provider: HTTP status → category
    "rbac-denied",
    "not-found",
    "conflict",
    "invalid-request",
    "api-unavailable",
    # build_spec._Execution: step state → code
    "build-failed",
    "build-timed-out",
    "docker-unavailable",
    # build_spec: smoke check outcome ("smoke-failed" if finished else ...)
    "smoke-failed",
    "smoke-timed-out",
    # artifacts/cli.py fallback for unclassified input errors
    "invalid-or-unavailable-artifact-input",
    # artifacts/cli.py: "command-" + RunResult.state for execute-command
    "command-failed",
    "command-timed-out",
    "command-cancelled",
    "command-output-limit",
    # k8s/cli/release.py: fallbacks chosen by exception type
    "release-refused",
    "release-state-unavailable",
    # k8s/release_runner.resolve_ownership: several blocking codes at once
    "plan-blocked",
    # k8s/release_runner._declared_match: f"{what}-entry-not-declared"
    "adopt-entry-not-declared",
    "replace-entry-not-declared",
    # k8s/cli/release.py: an execution that failed without a failure category
    "execution-not-ready",
    # k8s/cli/inputs.py: phase defaults passed to _reject(error, default)
    "invalid-inputs-spec",
    "inputs-lock-invalid",
    "source-capture-failed",
    "inputs-io-error",
}
# Registered for the CLI contract itself (piceli/cli_contract.py users).
CONTRACT = {"unknown-error-code"}


def _name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _literal(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def emitted_codes() -> dict[str, set[str]]:
    """Every literal error code in the source, with where it appears."""
    found: dict[str, set[str]] = {}
    for path in sorted(SOURCE.rglob("*.py")):
        if path.name == "errors.py" and path.parent == SOURCE:
            continue
        where = str(path.relative_to(SOURCE))
        for node in ast.walk(ast.parse(path.read_text())):
            codes: list[str | None] = []
            if isinstance(node, ast.Call):
                name = _name(node.func)
                if name in FIRST_ARGUMENT and node.args:
                    codes.append(_literal(node.args[0]))
                if name in SECOND_ARGUMENT and len(node.args) > 1:
                    codes.append(_literal(node.args[1]))
                if name in RULES:
                    codes += [
                        _literal(rule.elts[1])
                        for rule in node.args
                        if isinstance(rule, ast.Tuple) and len(rule.elts) == 2
                    ]
                # ``SomeError(message, code="...")``: a keyword code.
                codes += [
                    _literal(keyword.value)
                    for keyword in node.keywords
                    if keyword.arg == "code"
                ]
            elif isinstance(node, ast.ClassDef):
                # Error classes with a fixed class-level ``code = "..."``.
                codes += [
                    _literal(item.value)
                    for item in node.body
                    if isinstance(item, ast.Assign)
                    and any(
                        isinstance(target, ast.Name) and target.id == "code"
                        for target in item.targets
                    )
                ]
            elif isinstance(node, ast.Dict):
                codes += [
                    _literal(value)
                    for key, value in zip(node.keys, node.values, strict=True)
                    if key is not None and _literal(key) in {"reason", "code"}
                ]
            for code in codes:
                if code is not None:
                    found.setdefault(code, set()).add(where)
    # Discovery failure kinds are raised as ProviderError(kind.value).
    from piceli.k8s.ops.discovery import DiscoveryFailureKind

    for kind in DiscoveryFailureKind:
        found.setdefault(kind.value, set()).add("k8s/ops/discovery.py")
    return found


def test_scan_finds_codes() -> None:
    found = emitted_codes()
    assert {"tool-pin-mismatch", "digest-mismatch", "invalid-spec"} <= set(found)
    assert len(found) > 80


def test_every_emitted_code_is_registered() -> None:
    missing = {
        code: sorted(where)
        for code, where in emitted_codes().items()
        if code not in ERRORS
    }
    assert not missing, f"add these codes to piceli/errors.py: {missing}"


def test_dynamic_codes_are_registered() -> None:
    assert not (DYNAMIC | CONTRACT) - set(ERRORS)


def test_every_registered_code_is_emitted() -> None:
    """No stale entries: each code is emitted by the source or the contract."""
    stale = set(ERRORS) - set(emitted_codes()) - DYNAMIC - CONTRACT
    assert not stale, f"no longer emitted, remove from piceli/errors.py: {stale}"


@pytest.mark.parametrize("entry", list(ERRORS.values()), ids=list(ERRORS))
def test_entry_is_complete(entry: ErrorCode) -> None:
    assert CODE.fullmatch(entry.code)
    assert entry.area in AREAS
    assert isinstance(entry.retry_safe, bool)
    for text in (entry.title, entry.cause, entry.fix):
        assert text.strip() and text == text.strip()
    assert not entry.title.endswith(".")
    assert entry.cause.endswith((".", ")"))
    assert entry.fix.endswith((".", ")", "`"))
    assert set(entry.to_dict()) == {
        "code",
        "title",
        "cause",
        "fix",
        "retry_safe",
        "area",
    }


def test_lookup() -> None:
    assert lookup("unknown-error-code") is ERRORS["unknown-error-code"]
    assert lookup("no-such-code") is None
