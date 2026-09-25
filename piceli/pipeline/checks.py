"""The checks stage's interface: a tiny protocol, so any check runner can plug in.

``piceli deploy`` runs a pipeline's ``checks`` after a successful apply
through a :class:`CheckRunner`: ``runner(checks, context)`` returns an object
with ``passed`` (bool) and ``results`` (a sequence). The default runner is
``piceli.checks.run_checks`` over a :class:`piceli.checks.CheckContext` built
from this context (the target's kubeconfig, context, transport and exec
policy), imported only when a pipeline declares checks; when that module is
not installed the stage is refused with ``pipeline-checks-unavailable``.

The ``context`` handed to the runner is a :class:`CheckContext`: the target,
namespace, release and the delivered image references. Checks read it; they
must not change the cluster.

Importing this module is side-effect free.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from piceli.pipeline.errors import PipelineError

if TYPE_CHECKING:
    from piceli.app import App
    from piceli.pipeline.model import Target


@runtime_checkable
class CheckReportLike(Protocol):
    """What a check runner returns."""

    @property
    def passed(self) -> bool: ...

    @property
    def results(self) -> Sequence[Any]: ...


class CheckRunner(Protocol):
    """Runs a pipeline's checks against a deployed release."""

    def __call__(self, checks: Sequence[Any], context: Any, /) -> CheckReportLike: ...


@dataclass(frozen=True)
class CheckContext:
    """What a check may read about the release it verifies.

    :param target: The pipeline's :class:`~piceli.pipeline.Target`.
    :param kubeconfig: Absolute kubeconfig path (explicit, never ambient).
    :param context: Kube context name.
    :param namespace: Namespace of the release.
    :param release: Release name that was applied.
    :param images: Image name → deployed reference.
    :param app: The typed app.
    :param state_dir: The pipeline's local state directory.
    :param base: Directory that ``python`` checks resolve relative files from.
    """

    target: Target
    kubeconfig: Path
    context: str
    namespace: str
    release: str
    images: Mapping[str, str] = field(default_factory=dict)
    app: App | None = None
    state_dir: Path | None = None
    base: Path | None = None


def default_runner() -> CheckRunner:
    """``piceli.checks.run_checks``, or a refusal when it is not installed."""
    import importlib

    try:
        module: Any = importlib.import_module("piceli.checks")
    except ImportError:
        module = None
    run = getattr(module, "run_checks", None) if module is not None else None
    if run is None:
        raise PipelineError(
            "pipeline-checks-unavailable",
            "the pipeline declares checks but piceli.checks is not available in "
            "this installation",
        )

    def runner(checks: Sequence[Any], context: Any) -> Any:
        if not isinstance(context, CheckContext):
            return run(checks, context)
        with check_context(context) as adapted:
            return run(checks, adapted)

    return runner


def check_context(context: CheckContext) -> Any:
    """The :class:`piceli.checks.CheckContext` the default runner hands to checks."""
    from piceli.checks import CheckContext as Context

    target = context.target
    return Context(
        context.kubeconfig,
        context.context,
        context.namespace,
        context.release,
        dict(context.images),
        base=context.base,
        transport=getattr(target, "transport", "https"),
        request_seconds=getattr(target, "request_seconds", 10.0),
        exec_policy=target.exec_policy() if target is not None else None,
    )


def describe_check(check: Any) -> Any:
    """A JSON-safe description of one check (for the plan and its hash)."""
    for method in ("describe", "to_dict"):
        function = getattr(check, method, None)
        if callable(function):
            return _jsonable(function())
    if dataclasses.is_dataclass(check) and not isinstance(check, type):
        return _jsonable(dataclasses.asdict(check))
    dump = getattr(check, "model_dump", None)
    if callable(dump):
        return _jsonable(dump(mode="json"))
    return {"type": type(check).__name__, "repr": repr(check)}


def describe_result(result: Any) -> Any:
    """A JSON-safe view of one check result."""
    return describe_check(result)


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_jsonable(item) for item in value]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _jsonable(dataclasses.asdict(value))
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return _jsonable(dump(mode="json"))
    return str(value)
