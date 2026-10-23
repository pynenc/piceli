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
from typing import TYPE_CHECKING, Any, ClassVar, Literal

if TYPE_CHECKING:
    from piceli.infra import Otlp
    from piceli.train.notify import Notify

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
#: The host name of this machine in ``hosts=`` (not a build host).
LOCAL = "local"
#: How parallel halves run: ``"auto"`` (when more than one host is free).
PARALLEL = ("auto", "on", "off")
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
    :param dev_targets: Narrow the members that reach a selected package
        only through a dev-dependency: instead of every test of such a
        member, only its integration test targets (``tests/*.rs``) whose
        source names the selected library. When other code of the member
        names it (its unit tests, a module its tests share), the whole
        member is still tested. ``cargo check`` checks the whole member.
    """

    roots: tuple[str, ...] = DEFAULT_ROOTS
    dev_targets: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "roots", _strings(self.roots, "Impact roots"))
        if not isinstance(self.dev_targets, bool):
            raise _invalid("Impact dev_targets is True or False")

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"roots": list(self.roots)}
        if self.dev_targets:
            body["dev_targets"] = True
        return {"impact": body}


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
    return Impact(
        roots=tuple(value["impact"]["roots"]),
        dev_targets=bool(value["impact"].get("dev_targets", False)),
    )


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
    :param hosts: Where the step may run, in order of preference: ``"local"``
        (this machine) and the names of the train's build hosts
        (``Train(builds=...)``). Empty: the train's ``hosts``.
    :param inputs: Globs of the files (in the tree) the step reads. With
        inputs, a step whose key (their content, the step's own definition
        and the toolchain files) passed before is not run again: it is
        ``reused``, and its ``outputs`` come back from the content store.
    :param outputs: Paths (files or directories) the step produces, relative
        to the tree (``target/...``: the cargo target directory). Kept in the
        content store when the step passes with ``inputs``.
    """

    kind: ClassVar[str] = "step"
    #: ``"test"`` steps judge candidates; ``"publish"`` steps run on promote.
    phase: ClassVar[str] = "test"
    #: Whether the step runs cargo (it gets the shared target directory).
    uses_cargo: ClassVar[bool] = False

    name: str
    _: KW_ONLY
    env: Mapping[str, str] | tuple[tuple[str, str], ...] = ()
    timeout: float = DEFAULT_TIMEOUT
    hosts: tuple[str, ...] = ()
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()

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
        hosts = _strings(self.hosts, f"step {self.name} hosts")
        if not all(host == LOCAL or _NAME.fullmatch(host) for host in hosts):
            raise _invalid(f"step {self.name}: hosts are 'local' or build host names")
        object.__setattr__(self, "hosts", hosts)
        inputs = _strings(self.inputs, f"step {self.name} inputs")
        outputs = _strings(self.outputs, f"step {self.name} outputs")
        for path in (*inputs, *outputs):
            if not path or path.startswith("/") or ".." in path.split("/"):
                raise _invalid(
                    f"step {self.name}: inputs and outputs are paths inside the tree"
                )
        if outputs and not inputs:
            raise _invalid(
                f"step {self.name}: outputs are kept by the inputs that made them: "
                "declare inputs too"
            )
        object.__setattr__(self, "inputs", inputs)
        object.__setattr__(self, "outputs", outputs)

    @property
    def cargo(self) -> bool:
        """Whether this step runs cargo (shared target directory, sccache)."""
        return self.uses_cargo

    @staticmethod
    def command(
        name: str,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        hosts: Sequence[str] = (),
        inputs: Sequence[str] = (),
        outputs: Sequence[str] = (),
        cargo: bool = False,
    ) -> CommandStep:
        """Run ``argv`` in the merged checkout; exit status 0 passes.

        A relative program path (``scripts/check.sh``) is the merged tree's,
        so the script and the files it reads are the candidate's. With
        ``cargo=True`` the command gets the train's shared cargo target
        directory (``CARGO_TARGET_DIR``) and compiler cache, as the cargo
        steps do.
        """
        return CommandStep(
            name,
            argv=tuple(argv),
            env=env or (),
            timeout=timeout,
            hosts=tuple(hosts),
            inputs=tuple(inputs),
            outputs=tuple(outputs),
            runs_cargo=cargo,
        )

    @staticmethod
    def publish(
        name: str,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        hosts: Sequence[str] = (),
        inputs: Sequence[str] = (),
        outputs: Sequence[str] = (),
        cargo: bool = False,
    ) -> PublishStep:
        """Run ``argv`` in a checkout of the promoted head, only on
        ``--promote`` (after the tag and the branches moved). It never judges
        a candidate. It also gets ``PICELI_TRAIN_TAG``. The outputs of the
        test steps that declared them are restored into its checkout first."""
        return PublishStep(
            name,
            argv=tuple(argv),
            env=env or (),
            timeout=timeout,
            hosts=tuple(hosts),
            inputs=tuple(inputs),
            outputs=tuple(outputs),
            runs_cargo=cargo,
        )

    @staticmethod
    def images(
        composition: str,
        *,
        name: str = "images",
        components: Sequence[str] = (),
        platforms: Sequence[str] = (),
        repository: str = "",
        branch: str = "main",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> ImagesStep:
        """On ``--promote``, build the images the GitOps controller of
        ``composition`` will want for the promoted head, from that commit,
        push them to the cluster's registry under the controller's own change
        key, and offer them to the controller (see :mod:`piceli.train.images`).

        :param composition: The composition module (``infra.py``, relative to
            the root of the repository holding it; by default this one, read
            from the promoted head).
        :param components: The components to build (default: every component
            built from a source of this repository).
        :param platforms: The platforms (default: the controller's).
        :param repository: The composition's own repository when it is not
            this one: the URL ``piceli gitops enable`` recorded (the
            controller names that repository by it). The train reads it at
            the tip of ``branch``, the commit the controller follows, with
            this machine's Git credentials.
        :param branch: The branch of ``repository`` the controller follows.
        """
        return ImagesStep(
            name,
            composition=composition,
            components=tuple(components),
            platforms=tuple(platforms),
            repository=repository,
            branch=branch,
            timeout=timeout,
        )

    def _fields(self) -> dict[str, Any]:
        return {}

    def to_dict(self) -> dict[str, Any]:
        """The step as JSON-ready data (:func:`step_from_dict` reads it back)."""
        optional = {
            key: list(getattr(self, key))
            for key in ("hosts", "inputs", "outputs")
            if getattr(self, key)
        }
        return {
            "kind": self.kind,
            "name": self.name,
            "env": dict(self.env),  # type: ignore[arg-type]
            "timeout": self.timeout,
            **optional,
            **self._fields(),
        }


@dataclass(frozen=True)
class CommandStep(Step):
    """:meth:`Step.command`: a command run in the merged checkout."""

    kind: ClassVar[str] = "command"

    argv: tuple[str, ...] = ()
    runs_cargo: bool = False

    def __post_init__(self) -> None:
        super().__post_init__()
        argv = _strings(self.argv, f"step {self.name} argv")
        if not argv or not argv[0]:
            raise _invalid(f"step {self.name}: argv is empty")
        object.__setattr__(self, "argv", argv)
        if not isinstance(self.runs_cargo, bool):
            raise _invalid(f"step {self.name}: cargo is True or False")

    @property
    def cargo(self) -> bool:
        return self.runs_cargo

    def _fields(self) -> dict[str, Any]:
        return {"argv": list(self.argv), **({"cargo": True} if self.runs_cargo else {})}


@dataclass(frozen=True)
class PublishStep(CommandStep):
    """:meth:`Step.publish`: a command run only on promote."""

    kind: ClassVar[str] = "publish"
    phase: ClassVar[str] = "publish"


@dataclass(frozen=True)
class ImagesStep(Step):
    """:meth:`Step.images`: the controller's images, built on promote."""

    kind: ClassVar[str] = "images"
    phase: ClassVar[str] = "publish"

    composition: str = ""
    components: tuple[str, ...] = ()
    platforms: tuple[str, ...] = ()
    repository: str = ""
    branch: str = "main"

    def __post_init__(self) -> None:
        super().__post_init__()
        if (
            not isinstance(self.composition, str)
            or not self.composition.endswith(".py")
            or self.composition.startswith("/")
            or ".." in self.composition.split("/")
        ):
            raise _invalid(
                f"step {self.name}: composition is the composition module's path "
                "in the repository (infra.py)"
            )
        object.__setattr__(
            self,
            "components",
            _strings(self.components, f"step {self.name} components"),
        )
        platforms = _strings(self.platforms, f"step {self.name} platforms")
        if not all(item in ("linux/amd64", "linux/arm64") for item in platforms):
            raise _invalid(f"step {self.name}: platforms are linux/amd64, linux/arm64")
        object.__setattr__(self, "platforms", platforms)
        if not isinstance(self.repository, str):
            raise _invalid(f"step {self.name}: repository is a Git URL")
        if self.repository:
            from piceli.infra import CompositionError, Source

            try:
                Source(self.repository)
            except CompositionError:
                raise _invalid(
                    f"step {self.name}: repository is a Git URL without credentials"
                ) from None
        from piceli.envs.model import Branch, EnvError

        try:
            Branch(self.branch)
        except (EnvError, TypeError):
            raise _invalid(
                f"step {self.name}: branch is one exact branch name"
            ) from None
        if self.inputs or self.outputs:
            raise _invalid(
                f"step {self.name}: an images step is keyed by the controller's "
                "change keys, not inputs"
            )

    def _fields(self) -> dict[str, Any]:
        return {
            "composition": self.composition,
            "components": list(self.components),
            "platforms": list(self.platforms),
            **(
                {"repository": self.repository, "branch": self.branch}
                if self.repository
                else {}
            ),
        }


@dataclass(frozen=True)
class CargoCheck(Step):
    """``cargo check`` on the selected packages.

    :param select: :class:`Impact` (default) or ``"all"`` (``--workspace``).
    :param args: Extra cargo arguments (default ``--all-targets``).
    """

    kind: ClassVar[str] = "cargo-check"
    uses_cargo: ClassVar[bool] = True

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
    uses_cargo: ClassVar[bool] = True

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
    "images": ImagesStep,
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
        "hosts": tuple(data.get("hosts") or ()),
        "inputs": tuple(data.get("inputs") or ()),
        "outputs": tuple(data.get("outputs") or ()),
    }
    if issubclass(kind, CommandStep):
        return kind(
            data["name"],
            argv=tuple(data["argv"]),
            runs_cargo=bool(data.get("cargo", False)),
            **common,
        )
    if kind is ImagesStep:
        return ImagesStep(
            data["name"],
            composition=str(data["composition"]),
            components=tuple(data.get("components") or ()),
            platforms=tuple(data.get("platforms") or ()),
            repository=str(data.get("repository") or ""),
            branch=str(data.get("branch") or "main"),
            env=common["env"],
            timeout=common["timeout"],
            hosts=common["hosts"],
        )
    extra: dict[str, Any] = {
        "select": _selection_from(data["select"]),
        "args": tuple(data.get("args") or ()),
    }
    if kind is CargoTests:
        extra["retry"] = data.get("retry", 1)
    return kind(data["name"], **common, **extra)


_SIZE = re.compile(r"\d+(\.\d+)?\s*([KMGT]i?B?|B)?", re.IGNORECASE)


@dataclass(frozen=True)
class Retention:
    """How much a train keeps on this machine (pruned at the end of each
    run, and by ``piceli train-prune``).

    :param store_size: The content store's bytes (shared by every train of
        the machine), least recently used first beyond it.
    :param store_days: A content-store record unused this long goes.
    :param targets_size: The cargo target directories of this train and
        repository, least recently used first beyond it.
    :param target_days: A target directory unused this long goes.
    :param log_days: A run's record and logs older than this go...
    :param keep_runs: ...except the newest ``keep_runs`` runs.
    """

    store_size: str = "20GiB"
    store_days: int = 30
    targets_size: str = "100GiB"
    target_days: int = 14
    log_days: int = 30
    keep_runs: int = 20

    def __post_init__(self) -> None:
        for name in ("store_size", "targets_size"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _SIZE.fullmatch(value.strip()):
                raise _invalid(f"retention {name} is a size like 20GiB")
        for name in ("store_days", "target_days", "log_days", "keep_runs"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise _invalid(f"retention {name} is a positive integer")

    def to_dict(self) -> dict[str, Any]:
        return {
            "store_size": self.store_size,
            "store_days": self.store_days,
            "targets_size": self.targets_size,
            "target_days": self.target_days,
            "log_days": self.log_days,
            "keep_runs": self.keep_runs,
        }


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
    :param builds: The build hosts steps may run on: a
        :class:`~piceli.dev.model.DevBuilds`, or ``MODULE:ATTR`` of one or of
        a ``Cluster`` declaring ``dev=DevBuilds(...)`` (its docker and native
        hosts; read with the spec). ``None``: every step runs here.
    :param hosts: Where steps run by default, in order of preference:
        ``"local"`` and build host names (a step's own ``hosts`` wins).
    :param local_slots: Commands this machine runs at once, for every train
        on it (it stays usable).
    :param fallback: ``"local"``: a step whose hosts are all down or
        quarantined runs here; ``None``: it fails (``train-host-unavailable``).
    :param target_dir: The cargo target directory of local cargo steps,
        shared by every trial (warm builds): ``"auto"`` (a pool of
        ``local_slots`` directories under Piceli's cache, one per concurrent
        command; ``CARGO_TARGET_DIR`` in the environment wins), a path, or
        ``None`` (each trial's own ``target/``, cold).
    :param sccache: ``"auto"``: local cargo steps use ``sccache`` when it is
        on ``PATH`` and ``RUSTC_WRAPPER`` is unset; ``True``: always;
        ``False``: never.
    :param parallel: ``"auto"``: after a failed joint run, the two halves run
        at once on two hosts when two are free (the right half speculatively
        on top of the left one); ``"on"``: whenever there are two hosts;
        ``"off"``: never.
    :param cache: Where the shared target directories, the content store and
        the slot locks live (default ``$XDG_CACHE_HOME/piceli/train``, else
        ``~/.cache/piceli/train``).
    :param retention: How much of it, and of the run history, is kept
        (:class:`Retention`; pruned at the end of each run).
    :param telemetry: Export each run as one OpenTelemetry trace (spans
        ``piceli.dev.train``, ``.trial``, ``.step``) with logs, over OTLP/HTTP:
        ``Otlp("http://localhost:4318", protocol="http/protobuf")`` (sent as
        protobuf: needs the ``piceli[telemetry]`` extra; a collector behind a
        private CA: ``Otlp(..., ca_file=PATH)``). Without it, the standard
        ``OTEL_EXPORTER_OTLP_*`` variables apply, as for ``piceli deploy``. A
        collector that is down never slows or fails a run.
    :param notify: Notifications sent when a run ends
        (:class:`~piceli.train.notify.Notify`): a command, a webhook, the desktop.

    The train's identity (``name``, ``repo``, ``base``, ``tested``, ``tag``,
    ``remote``), where it runs (``builds``, ``hosts``, ``local_slots``,
    ``fallback``, ``target_dir``, ``sccache``, ``parallel``, ``cache``) and
    how it is observed (``telemetry``, ``notify``) come from the spec you run;
    the steps come from the commit under test.
    """

    name: str
    _: KW_ONLY
    steps: Sequence[Step]
    repo: str = "."
    base: str = "main"
    tested: str = "tested"
    tag: str = "tested/{date}.{n}"
    remote: str = "origin"
    builds: Any = None
    hosts: tuple[str, ...] = (LOCAL,)
    local_slots: int = 1
    fallback: str | None = LOCAL
    target_dir: str | None = "auto"
    sccache: bool | str = "auto"
    parallel: str = "auto"
    cache: str | None = None
    retention: Retention = field(default_factory=Retention)
    telemetry: Otlp | None = None
    notify: Sequence[Notify] = ()

    def __post_init__(self) -> None:
        self._check_observers()
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
        self._check_hosts()

    def _check_hosts(self) -> None:
        builds = self.builds
        if builds is not None and not isinstance(builds, str):
            from piceli.dev.model import DevBuilds

            if not isinstance(builds, DevBuilds):
                raise _invalid("builds is a DevBuilds or MODULE:ATTR naming one")
        elif isinstance(builds, str) and ":" not in builds:
            raise _invalid("builds is a DevBuilds or MODULE:ATTR naming one")
        hosts = _strings(self.hosts, "hosts")
        if not hosts:
            raise _invalid("hosts names at least one host ('local')")
        if not all(host == LOCAL or _NAME.fullmatch(host) for host in hosts):
            raise _invalid("hosts are 'local' or build host names")
        object.__setattr__(self, "hosts", hosts)
        known = self.host_names()
        if known is not None:
            unknown = [host for host in hosts if host not in known]
            if unknown:
                raise _invalid(f"hosts: no build host {', '.join(unknown)}")
        if (
            type(self.local_slots) is not int
            or self.local_slots < 1
            or self.local_slots > 64
        ):
            raise _invalid("local_slots is an integer from 1 to 64")
        if self.fallback not in (LOCAL, None):
            raise _invalid("fallback is 'local' or None")
        if self.target_dir is not None and (
            not isinstance(self.target_dir, str) or not self.target_dir
        ):
            raise _invalid('target_dir is "auto", a path or None')
        if self.sccache not in ("auto", True, False):
            raise _invalid('sccache is "auto", True or False')
        if self.parallel not in PARALLEL:
            raise _invalid('parallel is "auto", "on" or "off"')
        if not isinstance(self.retention, Retention):
            raise _invalid("retention is a Retention(...)")
        if self.cache is not None and (
            not isinstance(self.cache, str) or not self.cache
        ):
            raise _invalid("cache is a directory path or None")

    def host_names(self) -> set[str] | None:
        """``local`` and the build hosts steps may name (``None``: not known
        yet, ``builds`` is a ``MODULE:ATTR`` read with the spec)."""
        if isinstance(self.builds, str):
            return None
        names = {LOCAL}
        if self.builds is not None:
            names |= {host.name for host in self.builds.hosts}
        return names

    def _check_observers(self) -> None:
        from piceli.infra import Otlp
        from piceli.train.notify import Notify

        if self.telemetry is not None:
            if not isinstance(self.telemetry, Otlp):
                raise _invalid("telemetry must be Otlp(...) or None")
            if self.telemetry.protocol != "http/protobuf":
                raise _invalid(
                    'a train exports over OTLP/HTTP: Otlp(..., protocol="http/protobuf")'
                )
            if self.telemetry.headers_secret:
                raise _invalid(
                    "a train runs outside the cluster: Otlp(headers_secret=) is a "
                    "cluster Secret; set OTEL_EXPORTER_OTLP_HEADERS instead"
                )
            if self.telemetry.ca_secret:
                raise _invalid(
                    "a train runs outside the cluster: Otlp(ca_secret=) is a "
                    "cluster Secret; give the CA file with Otlp(ca_file=PATH) or "
                    "OTEL_EXPORTER_OTLP_CERTIFICATE instead"
                )
        if isinstance(self.notify, Notify) or not isinstance(self.notify, Sequence):
            raise _invalid("notify must be a list of Notify objects")
        notify = tuple(self.notify)
        if not all(isinstance(item, Notify) for item in notify):
            raise _invalid("notify must be a list of Notify objects")
        object.__setattr__(self, "notify", notify)

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
            **self._run_fields(),
            "telemetry": self.telemetry.to_dict() if self.telemetry else None,
            "notify": [item.to_dict() for item in self.notify],
        }

    def _run_fields(self) -> dict[str, Any]:
        """Where it runs, only what differs from the defaults."""
        body: dict[str, Any] = {}
        if isinstance(self.builds, str):
            body["builds"] = {"ref": self.builds}
        elif self.builds is not None:
            body["builds"] = {"dev": self.builds.describe()}
        defaults = {
            "hosts": (LOCAL,),
            "local_slots": 1,
            "fallback": LOCAL,
            "target_dir": "auto",
            "sccache": "auto",
            "parallel": "auto",
            "cache": None,
        }
        for key, default in defaults.items():
            value = getattr(self, key)
            if value != default:
                body[key] = list(value) if isinstance(value, tuple) else value
        if self.retention != Retention():
            body["retention"] = self.retention.to_dict()
        return body

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Train:
        from piceli.infra import Otlp
        from piceli.train.notify import notify_from_dict

        telemetry = data.get("telemetry")
        return cls(
            data["name"],
            steps=[step_from_dict(item) for item in data["steps"]],
            repo=data.get("repo", "."),
            base=data.get("base", "main"),
            tested=data.get("tested", "tested"),
            tag=data.get("tag", "tested/{date}.{n}"),
            remote=data.get("remote", "origin"),
            builds=_builds_from(data.get("builds")),
            hosts=tuple(data.get("hosts") or (LOCAL,)),
            local_slots=data.get("local_slots", 1),
            fallback=data.get("fallback", LOCAL),
            target_dir=data.get("target_dir", "auto"),
            sccache=data.get("sccache", "auto"),
            parallel=data.get("parallel", "auto"),
            cache=data.get("cache"),
            retention=Retention(**(data.get("retention") or {})),
            telemetry=Otlp.from_dict(telemetry) if telemetry else None,
            notify=[notify_from_dict(item) for item in data.get("notify") or ()],
        )


def _builds_from(value: Any) -> Any:
    if not value:
        return None
    if "ref" in value:
        return str(value["ref"])
    from piceli.dev.model import DevBuilds

    return DevBuilds.from_dict(value["dev"])
