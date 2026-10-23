"""The train spec: which branches may join, judged by which steps.

A :class:`Train` is declared in Python, in a module of the repository it
tests (usually the app's composition module). Steps are plain data: an argv
and its environment, or a cargo step with an impact selection. That keeps a
spec serialisable, so it is read from the commit under test (a subprocess
loads the module in the merged checkout and prints it as JSON), and a later
runner can ship a step to another host unchanged.

Importing this module is side-effect free.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import KW_ONLY, dataclass, field
from typing import Any, ClassVar, Literal

#: Changed paths (globs, relative to the repository root) that make an
#: :class:`Impact` selection test every package: the workspace manifest, the
#: lock file, the toolchain and cargo's configuration.
DEFAULT_ROOTS: tuple[str, ...] = (
    "Cargo.toml",
    "Cargo.lock",
    "rust-toolchain",
    "rust-toolchain.toml",
    ".cargo/config",
    ".cargo/config.toml",
)

#: Seconds a step may run before it is stopped (its verdict is ``timeout``).
DEFAULT_TIMEOUT = 3600.0

_NAME = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
_STEP = re.compile(r"[a-z0-9][a-z0-9_.-]{0,62}")
_TAG_FIELDS = re.compile(r"\{([^{}]*)\}")
#: Step names the train uses for its own records.
RESERVED_STEPS = frozenset({"merge", "spec"})


class TrainError(ValueError):
    """A train declaration or run was refused; ``code`` is registered in
    :mod:`piceli.errors` (``piceli explain <code>``)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _invalid(message: str) -> TrainError:
    return TrainError("train-invalid", message)


def _strings(value: Any, what: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise _invalid(f"{what} must be a list of strings")
    items = tuple(value)
    if not all(isinstance(item, str) for item in items):
        raise _invalid(f"{what} must be a list of strings")
    return items


def _environment(value: Any, what: str) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if isinstance(value, tuple) and all(
        isinstance(pair, tuple) and len(pair) == 2 for pair in value
    ):
        value = dict(value)
    if not isinstance(value, Mapping):
        raise _invalid(f"{what} env must be a mapping of strings")
    pairs = tuple(sorted(value.items()))
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in pairs):
        raise _invalid(f"{what} env must be a mapping of strings")
    return pairs


@dataclass(frozen=True)
class Impact:
    """Select the cargo packages a change can affect.

    The packages whose files changed between the train's head and the commit
    under test, plus every workspace member that depends on them (normal and
    build dependencies, transitively; dev-dependencies for direct users).
    A changed path matching one of ``roots`` selects every package. Files
    outside every package that match no root select nothing (documentation,
    scripts: other steps check those).

    :param roots: Globs (``fnmatch``, relative to the repository root) whose
        change tests everything. Default :data:`DEFAULT_ROOTS`.
    """

    roots: tuple[str, ...] = DEFAULT_ROOTS

    def __post_init__(self) -> None:
        object.__setattr__(self, "roots", _strings(self.roots, "Impact roots"))

    def to_dict(self) -> dict[str, Any]:
        return {"impact": {"roots": list(self.roots)}}


Selection = Impact | Literal["all"]


def _selection(value: Any) -> Selection:
    if value == "all" or value is None:
        return "all"
    if isinstance(value, Impact):
        return value
    raise _invalid('select must be Impact(...) or "all"')


def _selection_dict(value: Selection) -> Any:
    return "all" if value == "all" else value.to_dict()


def _selection_from(value: Any) -> Selection:
    if value == "all":
        return "all"
    return Impact(roots=tuple(value["impact"]["roots"]))


@dataclass(frozen=True)
class Step:
    """One step a candidate must pass, run in the merged checkout.

    Build steps with :meth:`command` and :meth:`publish`, or use
    :class:`CargoCheck` and :class:`CargoTests`. Every step gets the
    variables ``PICELI_TRAIN`` (the train's name), ``PICELI_TRAIN_BASE`` (the
    train's head the candidates were merged onto), ``PICELI_TRAIN_HEAD`` (the
    commit under test) and ``PICELI_TRAIN_BRANCHES`` (the candidate branches
    in this run, space separated) on top of the caller's environment and its
    own ``env``.

    :param name: Unique in the train; names the step in results and logs.
    :param env: Extra environment variables.
    :param timeout: Seconds before the step is stopped (verdict ``timeout``).
    """

    kind: ClassVar[str] = "step"
    #: ``"test"`` steps judge candidates; ``"publish"`` steps run on promote.
    phase: ClassVar[str] = "test"

    name: str
    _: KW_ONLY
    env: Mapping[str, str] | tuple[tuple[str, str], ...] = ()
    timeout: float = DEFAULT_TIMEOUT

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _STEP.fullmatch(self.name):
            raise _invalid(
                f"step name {self.name!r}: lowercase letters, digits, '-', '_' and '.'"
            )
        if self.name in RESERVED_STEPS:
            raise _invalid(f"step name {self.name!r} is reserved")
        object.__setattr__(self, "env", _environment(self.env, f"step {self.name}"))
        if not isinstance(self.timeout, int | float) or self.timeout <= 0:
            raise _invalid(f"step {self.name}: timeout must be a positive number")
        object.__setattr__(self, "timeout", float(self.timeout))

    @staticmethod
    def command(
        name: str,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> CommandStep:
        """Run ``argv`` in the merged checkout; exit status 0 passes.

        A relative program path (``scripts/check.sh``) is the merged tree's,
        so the script and the files it reads are the candidate's.
        """
        return CommandStep(name, argv=tuple(argv), env=env or (), timeout=timeout)

    @staticmethod
    def publish(
        name: str,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> PublishStep:
        """Run ``argv`` in a checkout of the promoted head, only on
        ``--promote`` (after the tag and the branches moved). It never judges
        a candidate. It also gets ``PICELI_TRAIN_TAG``."""
        return PublishStep(name, argv=tuple(argv), env=env or (), timeout=timeout)

    def _fields(self) -> dict[str, Any]:
        return {}

    def to_dict(self) -> dict[str, Any]:
        """The step as JSON-ready data (:func:`step_from_dict` reads it back)."""
        return {
            "kind": self.kind,
            "name": self.name,
            "env": dict(self.env),  # type: ignore[arg-type]
            "timeout": self.timeout,
            **self._fields(),
        }


@dataclass(frozen=True)
class CommandStep(Step):
    """:meth:`Step.command`: a command run in the merged checkout."""

    kind: ClassVar[str] = "command"

    argv: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        super().__post_init__()
        argv = _strings(self.argv, f"step {self.name} argv")
        if not argv or not argv[0]:
            raise _invalid(f"step {self.name}: argv is empty")
        object.__setattr__(self, "argv", argv)

    def _fields(self) -> dict[str, Any]:
        return {"argv": list(self.argv)}


@dataclass(frozen=True)
class PublishStep(CommandStep):
    """:meth:`Step.publish`: a command run only on promote."""

    kind: ClassVar[str] = "publish"
    phase: ClassVar[str] = "publish"


@dataclass(frozen=True)
class CargoCheck(Step):
    """``cargo check`` on the selected packages.

    :param select: :class:`Impact` (default) or ``"all"`` (``--workspace``).
    :param args: Extra cargo arguments (default ``--all-targets``).
    """

    kind: ClassVar[str] = "cargo-check"

    name: str = "cargo-check"
    _: KW_ONLY
    select: Selection = field(default_factory=Impact)
    args: tuple[str, ...] = ("--all-targets",)

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "select", _selection(self.select))
        object.__setattr__(self, "args", _strings(self.args, f"step {self.name} args"))

    def _fields(self) -> dict[str, Any]:
        return {"select": _selection_dict(self.select), "args": list(self.args)}


@dataclass(frozen=True)
class CargoTests(Step):
    """``cargo test --no-fail-fast`` on the selected packages.

    Every test binary runs. When tests fail, only the failed tests run again
    (``retry`` times), each by its exact name in its own target: a test that
    passes on a retry does not eject the candidate, and is recorded as flaky
    with its first failure's output.

    :param select: :class:`Impact` (default) or ``"all"`` (``--workspace``).
    :param args: Extra arguments; those after ``--`` go to the test binaries.
    :param retry: How many times failed tests run again (0: never).
    """

    kind: ClassVar[str] = "cargo-test"

    name: str = "cargo-test"
    _: KW_ONLY
    select: Selection = field(default_factory=Impact)
    args: tuple[str, ...] = ()
    retry: int = 1

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "select", _selection(self.select))
        object.__setattr__(self, "args", _strings(self.args, f"step {self.name} args"))
        if type(self.retry) is not int or self.retry < 0:
            raise _invalid(f"step {self.name}: retry must be a non-negative integer")

    def _fields(self) -> dict[str, Any]:
        return {
            "select": _selection_dict(self.select),
            "args": list(self.args),
            "retry": self.retry,
        }


_KINDS: Mapping[str, type[Step]] = {
    "command": CommandStep,
    "publish": PublishStep,
    "cargo-check": CargoCheck,
    "cargo-test": CargoTests,
}


def step_from_dict(data: Mapping[str, Any]) -> Step:
    """The step :meth:`Step.to_dict` wrote."""
    kind = _KINDS.get(str(data.get("kind")))
    if kind is None:
        raise _invalid(f"unknown step kind {data.get('kind')!r}")
    common: dict[str, Any] = {
        "env": dict(data.get("env") or {}),
        "timeout": data.get("timeout", DEFAULT_TIMEOUT),
    }
    if issubclass(kind, CommandStep):
        return kind(data["name"], argv=tuple(data["argv"]), **common)
    extra: dict[str, Any] = {
        "select": _selection_from(data["select"]),
        "args": tuple(data.get("args") or ()),
    }
    if kind is CargoTests:
        extra["retry"] = data.get("retry", 1)
    return kind(data["name"], **common, **extra)


@dataclass(frozen=True)
class Train:
    """A merge train: candidates join ``base`` one tested merge at a time.

    :param name: A DNS label; names the run history and the merge commits.
    :param repo: The Git repository, relative to the spec module's directory
        (default: the repository holding the module).
    :param base: The branch candidates join (moved by fast-forward only).
    :param tested: The branch marking the last tested head (fast-forward only).
    :param steps: The steps each candidate set must pass, in order, then the
        publish steps run on promote.
    :param tag: The tag of a promoted head; ``{date}`` (UTC, ``YYYY-MM-DD``),
        ``{n}`` (1 + the highest number used that day; required) and ``{name}``.
    :param remote: Where ``--push`` pushes the tag, ``tested`` and ``base``.

    The train's identity (``name``, ``repo``, ``base``, ``tested``, ``tag``,
    ``remote``) comes from the spec you run; the steps come from the commit
    under test.
    """

    name: str
    _: KW_ONLY
    steps: Sequence[Step]
    repo: str = "."
    base: str = "main"
    tested: str = "tested"
    tag: str = "tested/{date}.{n}"
    remote: str = "origin"

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _NAME.fullmatch(self.name):
            raise _invalid(f"train name {self.name!r} is not a DNS label")
        for label, value in (
            ("repo", self.repo),
            ("base", self.base),
            ("tested", self.tested),
            ("remote", self.remote),
        ):
            if not isinstance(value, str) or not value:
                raise _invalid(f"{label} must be a non-empty string")
        if self.base == self.tested:
            raise _invalid("base and tested must be different branches")
        fields = set(_TAG_FIELDS.findall(self.tag))
        if "n" not in fields or fields - {"date", "n", "name"}:
            raise _invalid("tag must use {n} and only {date}, {n} and {name}")
        if isinstance(self.steps, str | Step) or not isinstance(self.steps, Sequence):
            raise _invalid("steps must be a list of Step objects")
        steps = tuple(self.steps)
        if not all(isinstance(step, Step) for step in steps):
            raise _invalid("steps must be a list of Step objects")
        names = [step.name for step in steps]
        if len(set(names)) != len(names):
            raise _invalid("two steps share a name")
        if not any(step.phase == "test" for step in steps):
            raise _invalid(
                "a train needs at least one test step (a branch never joins untested)"
            )
        object.__setattr__(self, "steps", steps)

    @property
    def test_steps(self) -> tuple[Step, ...]:
        return tuple(step for step in self.steps if step.phase == "test")

    @property
    def publish_steps(self) -> tuple[Step, ...]:
        return tuple(step for step in self.steps if step.phase == "publish")

    def tag_name(self, date: str, n: int) -> str:
        return self.tag.format(date=date, n=n, name=self.name)

    def to_dict(self) -> dict[str, Any]:
        """The train as JSON-ready data (:meth:`from_dict` reads it back)."""
        return {
            "name": self.name,
            "repo": self.repo,
            "base": self.base,
            "tested": self.tested,
            "tag": self.tag,
            "remote": self.remote,
            "steps": [step.to_dict() for step in self.steps],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Train:
        return cls(
            data["name"],
            steps=[step_from_dict(item) for item in data["steps"]],
            repo=data.get("repo", "."),
            base=data.get("base", "main"),
            tested=data.get("tested", "tested"),
            tag=data.get("tag", "tested/{date}.{n}"),
            remote=data.get("remote", "origin"),
        )
