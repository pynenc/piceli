"""Typed post-deploy check declarations, shared by Python and ``release.toml``.

Every check is a frozen, strictly validated model. In Python use the
:class:`Checks` constructors; in ``release.toml`` declare ``[[checks]]``
tables with a ``type`` key. Both produce the same objects, so a check written
in one form means exactly the same in the other.

Importing this module has no side effects: nothing here contacts a cluster,
imports user code or spawns a process.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import re
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

_NAME = re.compile(r"[a-z0-9](?:[-a-z0-9_.]{0,61}[a-z0-9])?")
_OBJECT = re.compile(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?")
_SAFE_PATH = re.compile(r"/[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*")
_ENTRY = re.compile(r"(?P<target>.+):(?P<attribute>[A-Za-z_][A-Za-z0-9_]*)")

#: Workload kinds each check type may target (lowercase, as ``kind/name``).
HTTP_KINDS = ("service", "deployment", "pod")
EXEC_KINDS = ("deployment", "statefulset", "daemonset", "pod")


class CheckSpecError(ValueError):
    """A check declaration is invalid. ``code`` is one fixed word.

    Codes: ``check-invalid`` (a malformed declaration) and
    ``check-callable-invalid`` (a ``python`` check's ``call`` cannot be
    imported or is not callable).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def split_target(value: str, kinds: Sequence[str]) -> tuple[str, str]:
    """``kind/name`` (any case of kind) → ``(kind, name)``, kind in ``kinds``."""
    kind, _, name = value.partition("/")
    kind = kind.lower()
    if kind not in kinds or not _OBJECT.fullmatch(name):
        raise ValueError(
            f"target must be one of {', '.join(k + '/NAME' for k in kinds)}, "
            f"got {value!r}"
        )
    return kind, name


def _handle_target(value: Any) -> tuple[str, int | None] | None:
    """An App handle → (``kind/name``, default port), or ``None``.

    Duck-typed so this module does not import :mod:`piceli.app`: a
    :class:`~piceli.app.model.Service` has ``selector`` and ``ports``; a
    workload (:class:`~piceli.app.model.Workload`) has ``containers`` and its
    ``kind``.
    """
    name = getattr(value, "name", None)
    if not isinstance(name, str) or isinstance(value, str):
        return None
    containers = getattr(value, "containers", None)
    if containers:
        ports = getattr(containers[0], "ports", ()) or ()
        port = ports[0] if ports else None
        port = getattr(port, "port", port)
        # A workload handle's own kind (StatefulSet, DaemonSet, Job …); a kind
        # a check cannot target is refused by the target validator.
        kind = getattr(value, "kind", None)
        kind = kind.lower() if isinstance(kind, str) and kind else "deployment"
        return f"{kind}/{name}", port if isinstance(port, int) else None
    ports = getattr(value, "ports", None)
    if ports is not None and getattr(value, "selector", None) is not None:
        first = ports[0] if ports else None
        port = getattr(first, "port", None)
        return f"service/{name}", port if isinstance(port, int) else None
    return None


def _from_handle(data: Any, *, with_port: bool) -> Any:
    """Before-validator: replace an App handle ``target`` by ``kind/name``.

    ``with_port`` also fills ``port`` from the handle's first port when unset.
    """
    if not isinstance(data, Mapping):
        return data
    resolved = _handle_target(data.get("target"))
    if resolved is None:
        return data
    target, port = resolved
    result = dict(data, target=target)
    if with_port and result.get("port") is None and port is not None:
        result["port"] = port
    return result


class _Check(BaseModel):
    """Fields every check has.

    :param name: Unique name in a check list; derived from the check when
        omitted (for example ``http-service-web-login``).
    :param timeout: Seconds one attempt may take.
    :param retries: Extra attempts after a failed one (``0`` = one attempt).
    :param interval: Seconds to wait between attempts.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str | None = None
    timeout: float = Field(default=10.0, gt=0, le=600)
    retries: int = Field(default=3, ge=0, le=100)
    interval: float = Field(default=2.0, ge=0, le=300)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        if value is not None and not _NAME.fullmatch(value):
            raise ValueError(
                "check name must be lowercase letters, digits, '-', '_' or '.' "
                "(at most 63 characters)"
            )
        return value

    @property
    def label(self) -> str:
        """The check's name: ``name`` or a stable derived one."""
        return self.name or self._default_name()

    def _default_name(self) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def public_dict(self) -> dict[str, Any]:
        """The declaration as JSON (a ``python`` check shows its entry point)."""
        return {"name": self.label, **self.model_dump(mode="json", exclude={"name"})}


def _slug(*parts: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", "-".join(parts).lower()).strip("-")
    if len(text) > 63:
        digest = hashlib.sha256(text.encode()).hexdigest()[:8]
        text = text[:54].rstrip("-") + "-" + digest
    return text or "check"


class HttpCheck(_Check):
    """GET ``path`` through a temporary supervised loopback port forward.

    :param target: ``service/NAME``, ``deployment/NAME`` or ``pod/NAME`` (or an
        App ``Service``/``Deployment`` handle).
    :param path: Absolute URL path, may include a query string.
    :param port: Remote port. Defaults to the handle's first port, or for a
        plain target to the first port of the live object.
    :param expect: Accepted status: one code (``200``) or an inclusive
        ``[low, high]`` range.
    :param body_contains: Text the response body must contain.

    Example: ``Checks.http(web, "/login", expect=200)``; in TOML
    ``{type = "http", target = "service/web", path = "/login", expect = 200}``.
    """

    type: Literal["http"] = "http"
    target: str
    path: str = "/"
    port: int | None = Field(default=None, ge=1, le=65535)
    expect: tuple[int, int] = (200, 299)
    body_contains: str | None = Field(default=None, min_length=1, max_length=4096)

    @model_validator(mode="before")
    @classmethod
    def _handle(cls, data: Any) -> Any:
        return _from_handle(data, with_port=True)

    @field_validator("target")
    @classmethod
    def _target(cls, value: str) -> str:
        kind, name = split_target(value, HTTP_KINDS)
        return f"{kind}/{name}"

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        if not _SAFE_PATH.fullmatch(value) or len(value) > 2048:
            raise ValueError("path must be a safe absolute URL path")
        return value

    @field_validator("expect", mode="before")
    @classmethod
    def _expect(cls, value: Any) -> Any:
        if isinstance(value, int) and not isinstance(value, bool):
            value = (value, value)
        if isinstance(value, list | tuple) and len(value) == 2:
            low, high = value
            if (
                isinstance(low, int)
                and isinstance(high, int)
                and 100 <= low <= high <= 599
            ):
                return (low, high)
        raise ValueError("expect must be a status code or [low, high] in 100..599")

    def _default_name(self) -> str:
        return _slug("http", self.target, self.path.split("?", 1)[0])


class ExecCheck(_Check):
    """Run ``command`` in one ready pod of ``target`` through the Kubernetes API.

    :param target: ``deployment/NAME``, ``statefulset/NAME``,
        ``daemonset/NAME`` or ``pod/NAME`` (or an App ``Deployment`` handle).
        For a workload, the newest ready pod its selector matches is used.
    :param command: The argv to execute (no shell unless you run one).
    :param container: Container name; defaults to the pod's first container.
    :param expect_exit: The exit code that passes.
    :param output_contains: Text stdout must contain.

    Uses the explicit kubeconfig of the :class:`~piceli.checks.CheckContext`
    (the ``pods/exec`` permission); never ``kubectl`` or an ambient context.
    """

    type: Literal["exec"] = "exec"
    target: str
    command: tuple[str, ...] = Field(min_length=1, max_length=64)
    container: str | None = None
    expect_exit: int = Field(default=0, ge=0, le=255)
    output_contains: str | None = Field(default=None, min_length=1, max_length=4096)

    @model_validator(mode="before")
    @classmethod
    def _handle(cls, data: Any) -> Any:
        return _from_handle(data, with_port=False)

    @field_validator("target")
    @classmethod
    def _target(cls, value: str) -> str:
        kind, name = split_target(value, EXEC_KINDS)
        return f"{kind}/{name}"

    @field_validator("container")
    @classmethod
    def _container(cls, value: str | None) -> str | None:
        if value is not None and not _NAME.fullmatch(value):
            raise ValueError("invalid container name")
        return value

    def _default_name(self) -> str:
        return _slug("exec", self.target, Path(self.command[0]).name)


Comparison = Literal["<", "<=", ">", ">=", "==", "!="]


class MetricCheck(_Check):
    """Query a Prometheus-compatible endpoint and compare every sample.

    The endpoint is reached through a temporary supervised port forward to
    ``target``; ``GET {path}?query=<query>`` must return the Prometheus HTTP
    API JSON (``vector`` or ``scalar``). The check passes when there is at
    least one sample (unless ``empty = "pass"``) and every sample value
    satisfies ``value <op> threshold``.

    :param target: ``service/NAME``, ``deployment/NAME`` or ``pod/NAME``.
    :param query: The PromQL query.
    :param op: One of ``<``, ``<=``, ``>``, ``>=``, ``==``, ``!=``.
    :param threshold: The number each sample is compared with.
    :param port: Remote port (default as for :class:`HttpCheck`).
    :param path: The query API path.
    :param empty: ``fail`` (default) or ``pass`` when the query has no samples.
    """

    type: Literal["metric"] = "metric"
    target: str
    query: str = Field(min_length=1, max_length=4096)
    op: Comparison = "<="
    threshold: float
    port: int | None = Field(default=None, ge=1, le=65535)
    path: str = "/api/v1/query"
    empty: Literal["fail", "pass"] = "fail"

    @model_validator(mode="before")
    @classmethod
    def _handle(cls, data: Any) -> Any:
        return _from_handle(data, with_port=True)

    @field_validator("target")
    @classmethod
    def _target(cls, value: str) -> str:
        kind, name = split_target(value, HTTP_KINDS)
        return f"{kind}/{name}"

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        if not _SAFE_PATH.fullmatch(value) or "?" in value:
            raise ValueError("path must be a safe absolute URL path without a query")
        return value

    def _default_name(self) -> str:
        return _slug("metric", self.target, self.query[:24])


class PythonCheck(_Check):
    """Call a function with the :class:`~piceli.checks.CheckContext`.

    :param call: A callable, or an entry point ``module:function`` /
        ``path/to/file.py:function`` (a relative file resolves from the
        context's ``base``, the directory of ``release.toml``).

    The function passes by returning ``None`` or ``True``. It fails by
    returning ``False``, by returning a string (the failure detail) or by
    raising :class:`~piceli.checks.CheckFailed`. Any other exception fails the
    check with ``check-raised`` and only the exception type as detail.
    """

    type: Literal["python"] = "python"
    call: str | Callable[..., Any]

    @field_validator("call")
    @classmethod
    def _call(cls, value: Any) -> Any:
        if isinstance(value, str):
            if not _ENTRY.fullmatch(value):
                raise ValueError(
                    "call must be 'module:function' or 'path/file.py:function'"
                )
            return value
        if not callable(value):
            raise ValueError("call must be callable or 'module:function'")
        return value

    @property
    def entry(self) -> str:
        """``module:function`` for display (a callable's qualified name)."""
        if isinstance(self.call, str):
            return self.call
        module = getattr(self.call, "__module__", "?")
        name = getattr(self.call, "__qualname__", type(self.call).__name__)
        return f"{module}:{name}"

    def public_dict(self) -> dict[str, Any]:
        return {
            "name": self.label,
            **self.model_dump(mode="json", exclude={"name", "call"}),
            "call": self.entry,
        }

    def _default_name(self) -> str:
        return _slug("python", self.entry.rpartition(":")[2])

    def resolve(self, base: Path | None = None) -> Callable[..., Any]:
        """Import the entry point (or return the callable)."""
        if not isinstance(self.call, str):
            return self.call
        return load_callable(self.call, base)


Check = Annotated[
    HttpCheck | ExecCheck | MetricCheck | PythonCheck, Field(discriminator="type")
]
_CHECKS: TypeAdapter[tuple[Check, ...]] = TypeAdapter(tuple[Check, ...])


def parse_checks(items: Sequence[Mapping[str, Any]]) -> tuple[Check, ...]:
    """Validate ``[[checks]]`` tables (or dicts) into check models.

    Raises :class:`CheckSpecError` (``check-invalid``) with every problem.
    """
    try:
        checks = _CHECKS.validate_python(tuple(items))
    except ValidationError as error:
        problems = "; ".join(
            f"checks.{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
            for item in error.errors()
        )
        raise CheckSpecError("check-invalid", problems) from None
    unique_names(checks)
    return checks


def as_checks(checks: Check | Iterable[Check] | None) -> tuple[Check, ...]:
    """One check, an iterable of checks or ``None`` → a validated tuple."""
    if checks is None:
        return ()
    if isinstance(checks, _Check):
        result: tuple[Check, ...] = (checks,)  # type: ignore[assignment]
    else:
        result = tuple(checks)
    for item in result:
        if not isinstance(item, _Check):
            raise CheckSpecError("check-invalid", f"not a check: {type(item).__name__}")
    unique_names(result)
    return result


def unique_names(checks: Sequence[Check]) -> None:
    """Refuse two checks with the same (declared or derived) name."""
    seen: set[str] = set()
    for item in checks:
        if item.label in seen:
            raise CheckSpecError(
                "check-invalid",
                f"two checks are named {item.label!r}; give them distinct names",
            )
        seen.add(item.label)


def load_callable(entry: str, base: Path | None = None) -> Callable[..., Any]:
    """Import ``module:function`` or ``path/file.py:function``.

    Raises :class:`CheckSpecError` (``check-callable-invalid``).
    """
    match = _ENTRY.fullmatch(entry)
    if match is None:
        raise CheckSpecError("check-callable-invalid", f"invalid entry {entry!r}")
    target, attribute = match["target"], match["attribute"]
    if target.endswith(".py") or "/" in target:
        path = Path(target).expanduser()
        if not path.is_absolute() and base is not None:
            path = base / path
        if not path.is_file():
            raise CheckSpecError(
                "check-callable-invalid", f"check file not found: {target}"
            )
        digest = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:16]
        module_name = f"_piceli_check_{digest}"
        module = sys.modules.get(module_name)
        if module is None:
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                raise CheckSpecError(
                    "check-callable-invalid", f"cannot import check file {target}"
                )
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
            except BaseException:
                del sys.modules[module_name]
                raise
    else:
        try:
            module = importlib.import_module(target)
        except ImportError as error:
            raise CheckSpecError(
                "check-callable-invalid",
                f"cannot import check module {target!r}: {type(error).__name__}",
            ) from None
    function = getattr(module, attribute, None)
    if not callable(function):
        raise CheckSpecError(
            "check-callable-invalid", f"check entry {entry!r} is not callable"
        )
    return function  # type: ignore[no-any-return]


class Checks:
    """Constructors for post-deploy checks.

    Targets are App handles (``app.deployment(...)``/``app.service(...)``
    results) or plain ``kind/name`` strings. Common keyword arguments:
    ``name``, ``timeout`` (seconds per attempt), ``retries`` and ``interval``.

    Example::

        from piceli.checks import Checks

        checks = [
            Checks.http(web, "/login", expect=200),
            Checks.exec("deployment/api", ["api", "self-test"]),
            Checks.metric("service/prometheus", "sum(rate(errors[5m]))",
                          op="<", threshold=0.05),
            Checks.python("checks.py:smoke"),
        ]
    """

    @staticmethod
    def http(
        target: Any,
        path: str = "/",
        *,
        expect: int | tuple[int, int] | None = None,
        port: int | None = None,
        body_contains: str | None = None,
        **common: Any,
    ) -> HttpCheck:
        """An :class:`HttpCheck` (``expect`` defaults to ``[200, 299]``)."""
        return HttpCheck.model_validate(
            {
                "target": target,
                "path": path,
                **({"expect": expect} if expect is not None else {}),
                "port": port,
                "body_contains": body_contains,
                **common,
            }
        )

    @staticmethod
    def exec(
        target: Any,
        command: Sequence[str],
        *,
        container: str | None = None,
        expect_exit: int = 0,
        output_contains: str | None = None,
        **common: Any,
    ) -> ExecCheck:
        """An :class:`ExecCheck`."""
        if isinstance(command, str):
            raise CheckSpecError(
                "check-invalid", "command is an argv list, not a shell string"
            )
        return ExecCheck.model_validate(
            {
                "target": target,
                "command": tuple(command),
                "container": container,
                "expect_exit": expect_exit,
                "output_contains": output_contains,
                **common,
            }
        )

    @staticmethod
    def metric(
        target: Any,
        query: str,
        *,
        op: Comparison = "<=",
        threshold: float,
        port: int | None = None,
        path: str = "/api/v1/query",
        empty: Literal["fail", "pass"] = "fail",
        **common: Any,
    ) -> MetricCheck:
        """A :class:`MetricCheck`."""
        return MetricCheck.model_validate(
            {
                "target": target,
                "query": query,
                "op": op,
                "threshold": threshold,
                "port": port,
                "path": path,
                "empty": empty,
                **common,
            }
        )

    @staticmethod
    def python(call: str | Callable[..., Any], **common: Any) -> PythonCheck:
        """A :class:`PythonCheck`."""
        return PythonCheck.model_validate({"call": call, **common})
