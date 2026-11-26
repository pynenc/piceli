"""One run of a host build plan: sync the stage, run the tools, assemble images.

See :mod:`piceli.artifacts.host_build`. The stage of a build lives at the
same path every run (``<cache>/stage/<name>``) and is *synchronised*, not
recreated: a file whose content is unchanged keeps its mtime, a changed or
new file gets the current time, and a file no longer declared is removed. The
compiler's freshness checks then see exactly the edited files, and the shared
target directory keeps everything else.

``build.shared_commands`` (0.23) run first, into ``<cache>/target/shared``,
unless ``run_shared`` is false (a later platform of a multi-platform build).

With ``build.shared_commands_alongside`` (0.78.0) they run in a thread of
their own, started with the platform's commands; the two sides use different
target directories, so cargo's build-directory lock never makes one wait for
the other. An image is assembled once the platform's commands and every
shared command naming it (every one, for a plain argv entry) have finished;
the platform's ``[[output.files]]`` need only its commands. The first
failure on either side stops the other: its running command's process group
gets ``SIGTERM``, then ``SIGKILL`` after a grace period (a runner that takes
``cancel``, as the default one does), its step is recorded ``cancelled``,
and the build fails with the failing command's error, its step last.

Each step records ``start_seconds`` (0.78.0): when it started, in seconds
from the start of the whole build, so a reader sees which steps overlapped.

Importing this module runs nothing.
"""

from __future__ import annotations

import collections
import hashlib
import inspect
import os
import re
import shutil
import stat
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from piceli.artifacts.build_context import (
    BuildContextError,
    ContextManifest,
    _open_relative,
    verify_staged,
)
from piceli.artifacts.build_spec import (
    PROCESS_OUTPUT_BUDGET,
    BuildSpecError,
    Runner,
    _BuildLog,
)
from piceli.artifacts.host_build import (
    _PASSTHROUGH,
    SHARED_TARGET_ENV,
    HostBuildGrant,
    HostBuildPlan,
    HostBuildSpec,
    HostImage,
    lock_file,
)
from piceli.artifacts.image_manifest import scan_image_stream
from piceli.artifacts.oci_layers import (
    BaseImage,
    BlobStore,
    Descriptor,
    ImageSettings,
    LayerEntry,
    LayerError,
    assemble,
    collect,
    pull_base,
    write_archive,
    write_layer,
)
from piceli.artifacts.process import ProcessLimits

MAX_ARCHIVE_BYTES = 16 * 1024**3
#: What a failed command keeps of its output (:attr:`BuildSpecError.output_tail`).
OUTPUT_TAIL_LINES = 200
OUTPUT_TAIL_BYTES = 16 * 1024
#: The first error's lines kept apart (0.19.0): printed last in the Job's log.
FIRST_ERROR_LINES = 20
_TAIL_LINE_CHARS = 500
_SECRET_NAME = re.compile(
    r"(?i)pass(?:word|wd)?|secret|token|api[_-]?key|authorization|credential"
)


class OutputTail:
    """The last bytes of one command's output, redacted on the way out.

    Redaction follows the pre-rollout log rules
    (:func:`piceli.pipeline.prerollout.scrub`): known secret values first,
    then ``key=value`` pairs naming a password, token or credential, then
    any long run of token characters.
    """

    def __init__(self) -> None:
        from piceli.dev.pod import FirstError

        self.data = bytearray()
        #: 0.19: the first ``error`` line and what follows (the cause), and
        #: the recent lines a corrupted cache shows in (piceli.dev.pod).
        self.first = FirstError(FIRST_ERROR_LINES)
        self.recent: collections.deque[str] = collections.deque(maxlen=2000)
        self._partial = b""

    def feed(self, block: bytes) -> None:
        self.data += block
        if len(self.data) > 4 * OUTPUT_TAIL_BYTES:
            del self.data[: len(self.data) - 4 * OUTPUT_TAIL_BYTES]
        *lines, self._partial = (self._partial + block).split(b"\n")
        for raw in lines:
            line = raw.decode("utf-8", "replace").rstrip("\r")
            self.first.feed(line)
            self.recent.append(line)

    def first_error(self, secrets: Iterable[str] = ()) -> str:
        return self._redact(self.first.lines, secrets)

    def text(self, secrets: Iterable[str] = ()) -> str:
        lines = bytes(self.data).decode("utf-8", "replace").replace("\r", "")
        return self._redact(lines.splitlines()[-OUTPUT_TAIL_LINES:], secrets)

    @staticmethod
    def _redact(lines: Iterable[str], secrets: Iterable[str]) -> str:
        from piceli.pipeline.prerollout import _SCHEME, _SENSITIVE, _TOKEN

        known = sorted({v for v in secrets if len(v) >= 4}, key=len, reverse=True)
        kept: list[str] = []
        for line in lines:
            for value in known:
                line = line.replace(value, "<redacted>")
            line = _SCHEME.sub(lambda match: match[1] + " <redacted>", line)
            line = _SENSITIVE.sub(lambda match: match[1] + "<redacted>", line)
            line = _TOKEN.sub("<redacted>", line)
            kept.append(line[:_TAIL_LINE_CHARS])
        while kept and len("\n".join(kept).encode()) > OUTPUT_TAIL_BYTES:
            kept.pop(0)
        return "\n".join(kept)


def sync_context(root: Path, manifest: ContextManifest, destination: Path) -> int:
    """Make ``destination`` hold exactly the manifest's files; returns how many changed.

    Unchanged files (same size and sha256) are left alone, so they keep their
    mtime; others are copied (re-hashed against the manifest while copied)
    and get the current time. Anything else below ``destination`` is removed.
    """
    destination.mkdir(parents=True, exist_ok=True)
    wanted = {item.path: item for item in manifest.files}
    changed = 0
    for item in manifest.files:
        target = destination.joinpath(*PurePosixPath(item.path).parts)
        if _same(target, item.sha256, item.size):
            _chmod(target, item.executable)
            continue
        for parent in reversed(target.parents):
            if parent == destination or destination not in parent.parents:
                continue
            if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                parent.unlink()
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or target.is_dir():
            _remove(target)
        temporary = target.with_name(f".piceli-{os.urandom(6).hex()}")
        fd = _open_relative(root, item.path)
        hasher, size = hashlib.sha256(), 0
        try:
            with os.fdopen(os.dup(fd), "rb") as source, open(temporary, "xb") as sink:
                while block := source.read(1024 * 1024):
                    size += len(block)
                    if size > item.size:
                        break
                    hasher.update(block)
                    sink.write(block)
            if size != item.size or "sha256:" + hasher.hexdigest() != item.sha256:
                raise BuildContextError(
                    "context-changed", "context file changed while staging"
                )
            _chmod(temporary, item.executable)
            os.replace(temporary, target)
        finally:
            os.close(fd)
            if temporary.exists():
                temporary.unlink()
        changed += 1
    keep = {
        str(parent)
        for path in wanted
        for parent in PurePosixPath(path).parents
        if str(parent) != "."
    } | set(wanted)
    for current, directories, files in os.walk(destination, topdown=False):
        here = Path(current)
        for name in [*files, *directories]:
            path = here / name
            relative = path.relative_to(destination).as_posix()
            if relative not in keep:
                _remove(path)
    return changed


def _same(path: Path, sha256: str, size: int) -> bool:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size != size:
            return False
        with path.open("rb") as stream:
            return (
                "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest() == sha256
            )
    except OSError:
        return False


def _chmod(path: Path, executable: bool) -> None:
    mode = 0o755 if executable else 0o644
    if stat.S_IMODE(path.lstat().st_mode) != mode:
        os.chmod(path, mode)


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _takes_cancel(runner: Any) -> bool:
    """Whether ``runner`` takes ``cancel=`` (0.78.0: the default one does)."""
    try:
        parameters = inspect.signature(runner).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        item.name == "cancel" or item.kind is inspect.Parameter.VAR_KEYWORD
        for item in parameters
    )


class _Stopped(Exception):
    """A side of an overlapped build stopped because the other one failed."""


class _SideLog:
    """One side's view of a shared `_BuildLog` (0.78.0): whole lines only,
    under one lock, so two commands' output never mixes inside a line."""

    def __init__(self, log: _BuildLog, lock: threading.Lock) -> None:
        self.log = log
        self.lock = lock
        self.pending: dict[str, bytes] = {}

    def line(self, text: str) -> None:
        with self.lock:
            self.log.line(text)

    def say(self, text: str) -> None:
        with self.lock:
            self.log.say(text)

    def feed(self, stream: str, block: bytes) -> None:
        with self.lock:
            if self.log.raw is not None:
                self.log.raw(block)
            data = self.pending.pop(stream, b"") + block
            cut = data.rfind(b"\n") + 1
            self.log._write(data[:cut])
            if data[cut:]:
                self.pending[stream] = data[cut:]

    def end_step(self) -> None:
        with self.lock:
            for stream in sorted(self.pending):
                self.log._write(self.pending.pop(stream) + b"\n")


class _SharedSide:
    """The shared commands of an overlapped build, in a thread (0.78.0)."""

    def __init__(
        self, execution: HostExecution, cwd: Path, env: Mapping[str, str]
    ) -> None:
        self.execution = execution
        self.cwd = cwd
        self.env = env
        self.done = 0  # how many shared commands have succeeded
        self.finished = False
        self.error: BaseException | None = None
        self.condition = threading.Condition()
        self.thread = threading.Thread(
            target=self._run, name="piceli-shared-commands", daemon=True
        )

    def start(self) -> None:
        self.execution.shared_target.mkdir(parents=True, exist_ok=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            commands = self.execution.plan.shared_commands
            for index, command in enumerate(commands, start=1):
                self.execution._command(index, command, self.cwd, self.env, shared=True)
                with self.condition:
                    self.done = index
                    self.condition.notify_all()
        except BaseException as error:
            with self.condition:
                self.error = error
            self.execution._stop(None)
        finally:
            with self.condition:
                self.finished = True
                self.condition.notify_all()

    def wait(self, count: int) -> None:
        """Until the first ``count`` shared commands succeeded; raises the
        shared side's error when it failed first."""
        with self.condition:
            self.condition.wait_for(
                lambda: self.done >= count or self.finished or self.error is not None
            )
            error = self.error
        if error is not None:
            raise error

    def join(self) -> None:
        self.thread.join()


class HostExecution:
    """Runs a `HostBuildPlan`; see the module docstring."""

    def __init__(
        self,
        plan: HostBuildPlan,
        grant: HostBuildGrant,
        cache: Path,
        runner: Runner,
        log: _BuildLog,
        cancel: threading.Event | None,
        client_factory: Callable[[str], Any] | None = None,
        run_shared: bool = True,
        build_start: float | None = None,
    ) -> None:
        self.plan = plan
        self.spec: HostBuildSpec = plan.spec
        self.grant = grant
        self.cache = cache
        self.runner = runner
        self.log: Any = log  # 0.78.0: a _SideLog when overlapped
        self.cancel = cancel
        self.client_factory = client_factory
        self.steps: list[dict[str, Any]] = []
        # 0.22.0: a cached blob whose bytes no longer match its digest is
        # discarded and made again; the line marks the build host's health
        # (piceli.dev.pod.HEALTH_SIGNATURES, kind cache-corrupt).
        self.store = BlobStore(
            cache,
            on_discard=self._discarded,
        )
        self.stage = cache / "stage" / self.spec.name
        # One target directory per architecture and page size: a cache shared
        # by nodes with other page sizes would rebuild jemalloc and everything
        # above it on each switch (B28).
        facts = plan.facts
        self.target = cache / "target" / f"{facts.architecture}-{facts.page_size}"
        self.total = len(plan.commands)
        #: 0.23: the shared commands' target, the same for every platform.
        self.shared_target = cache / "target" / "shared"
        self.run_shared = run_shared
        #: 0.22: what provisioning did per pinned tool (the receipt's ``toolchains``).
        self.toolchains: dict[str, dict[str, Any]] = {}
        #: 0.78.0: the whole build's start (``time.time()``), for ``start_seconds``.
        self.build_start = time.time() if build_start is None else build_start
        #: 0.78.0: overlapped shared commands: set to stop the other side.
        self.stop = threading.Event()
        self.failing: str | None = None  # the first failing command's label
        self.overlap = bool(
            self.spec.shared_commands_alongside and run_shared and plan.shared_commands
        )
        self.logs: dict[bool, Any] = {False: log, True: log}
        if self.overlap:
            lock = threading.Lock()
            self.logs = {False: _SideLog(log, lock), True: _SideLog(log, lock)}
            # Every line of this side, not only its commands', under the lock.
            self.log = self.logs[False]
        self._lock = threading.Lock()
        self._runner_stops = _takes_cancel(runner)

    def _offset(self) -> float:
        """0.78.0: seconds since the build started (a step's ``start_seconds``)."""
        return round(max(0.0, time.time() - self.build_start), 3)

    def _stop(self, label: str | None) -> None:
        """Stop the other side of an overlapped build; the first label wins."""
        with self._lock:
            if self.failing is None and label is not None:
                self.failing = label
        self.stop.set()

    def _discarded(self, value: str, reason: str) -> None:
        text = (
            f"[piceli] cache-corrupt: blob {value} discarded ({reason}); "
            "fetching or writing it again"
        )
        self.log.line(text)
        self.log.say(text)

    # ---------------------------------------------------------------- run
    def execute(self, output_dir: Path) -> dict[str, Any]:
        from piceli.artifacts.cache_budget import mark_used

        # 0.35.0: what this build uses is the last evicted over the budget.
        mark_used(
            self.cache,
            [self.stage, self.target, self.shared_target, self.cache / "blobs"],
        )
        guard = lock_file(self.cache / "stage" / f"{self.spec.name}.lock")
        try:
            changed = self._stage()
            self.log.say(f"[piceli] stage: {changed} file(s) changed since last build")
            self.target.mkdir(parents=True, exist_ok=True)
            environment = self._environment()
            shared = self._environment(shared=True)
            before = dict(environment)
            self._toolchains(environment)
            # The shared commands get the same toolchain selection.
            shared.update(
                (key, value)
                for key, value in environment.items()
                if before.get(key) != value
            )
            cwd = self.stage.joinpath(*PurePosixPath(self.spec.workdir).parts)
            if self.overlap:
                files, details, images = self._overlapped(
                    cwd, environment, shared, output_dir
                )
            else:
                self._shared_commands(cwd, shared)
                for index, command in enumerate(self.plan.commands, start=1):
                    self._command(index, command, cwd, environment)
                files, details, images = self._outputs(output_dir, None)
        except LayerError as error:
            raise BuildSpecError(
                error.code, str(error), steps=tuple(self.steps)
            ) from None
        finally:
            guard.close()
        if not details:
            return {"files": files, "images": images}
        return {"files": files, "images": images, "file_details": details}

    def _outputs(
        self, output_dir: Path, side: _SharedSide | None
    ) -> tuple[dict[str, str], dict[str, dict[str, Any]], dict[str, Any]]:
        """Files and images, after the commands; ``side``: each image first
        waits for the shared commands it needs (0.78.0)."""
        self._verify_consumed()
        files, details = self._files(output_dir)
        images_dir = output_dir / "images"
        if images_dir.exists():
            shutil.rmtree(images_dir)
        images_dir.mkdir(parents=True)
        bases: dict[str, BaseImage] = {}
        images = {}
        for image in self.plan.images:
            if side is not None:
                needed = self._shared_needed(image.name)
                if needed > side.done:
                    self.log.say(
                        f"[piceli] image {image.name}: waiting for shared "
                        f"command(s) 1-{needed}"
                    )
                side.wait(needed)
            start = self._offset()
            began = time.monotonic()
            images[image.name] = self._image(image, images_dir, bases)
            # 0.17.0: each image's assembly time (base pull, layers, archive).
            self.steps.append(
                {
                    "platform": self.plan.platform,
                    "kind": f"image-{image.name}",
                    "state": "succeeded",
                    "seconds": round(time.monotonic() - began, 3),
                    "start_seconds": start,
                }
            )
        return files, details, images

    def _shared_needed(self, image: str) -> int:
        """How many shared commands (from the first) must have finished
        before ``image`` is assembled: up to the last one naming it (any
        plain argv entry names every image)."""
        tags = self.plan.shared_images or tuple(() for _ in self.plan.shared_commands)
        needed = 0
        for index, images in enumerate(tags, start=1):
            if not images or image in images:
                needed = index
        return needed

    def _overlapped(
        self,
        cwd: Path,
        environment: Mapping[str, str],
        shared: Mapping[str, str],
        output_dir: Path,
    ) -> tuple[dict[str, str], dict[str, dict[str, Any]], dict[str, Any]]:
        """0.78.0: the shared commands in a thread, beside the platform's."""
        side = _SharedSide(self, cwd, shared)
        text = (
            f"[piceli] {self.plan.platform}: {len(self.plan.shared_commands)} "
            "shared command(s) run alongside the platform's commands"
        )
        self.log.line(text)
        self.log.say(text)
        side.start()
        try:
            for index, command in enumerate(self.plan.commands, start=1):
                self._command(index, command, cwd, environment)
            result = self._outputs(output_dir, side)
            side.wait(len(self.plan.shared_commands))
        except BaseException as error:
            self._stop(None)
            side.join()
            if isinstance(error, _Stopped) and side.error is not None:
                raise self._last(side.error) from None
            raise self._last(error) from None
        side.join()
        return result

    def _last(self, error: BaseException) -> BaseException:
        """``error`` with every step recorded so far, its failing step last."""
        if not isinstance(error, BuildSpecError):
            return error
        failing = error.steps[-1] if error.steps else None
        steps = [step for step in self.steps if step is not failing]
        if failing is not None:
            steps.append(failing)
        return BuildSpecError(
            error.code,
            str(error),
            steps=tuple(steps),
            output_tail=error.output_tail,
            first_error=error.first_error,
        )

    def _files(
        self, output_dir: Path
    ) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
        """0.39: copy ``[[output.files]]`` to ``files/<name>/<file name>``.

        Returns ``outputs.files`` (``{path: sha256}``, the shape a Docker
        build's receipt has) and ``outputs.file_details`` (per path: the
        output's name, the file name, platform, sha256 and size).
        """
        files: dict[str, str] = {}
        details: dict[str, dict[str, Any]] = {}
        files_dir = output_dir / "files"
        if files_dir.exists():
            shutil.rmtree(files_dir)
        for item in self.plan.files:
            start = self._offset()
            began = time.monotonic()
            directory = files_dir / item.name
            directory.mkdir(parents=True)
            for src, name in item.target_files:
                source = self._source(self.target, src)
                if source.is_symlink() or not source.is_file():
                    raise BuildSpecError(
                        "host-output-missing",
                        f"{src!r} is not a regular file (output.files {item.name!r})",
                        steps=tuple(self.steps),
                    )
                target = directory / name
                shutil.copyfile(source, target, follow_symlinks=False)
                os.chmod(target, 0o755 if os.access(source, os.X_OK) else 0o644)
                hasher = hashlib.sha256()
                size = 0
                with target.open("rb") as stream:
                    while block := stream.read(1024 * 1024):
                        hasher.update(block)
                        size += len(block)
                sha = "sha256:" + hasher.hexdigest()
                path = f"files/{item.name}/{name}"
                files[path] = sha
                details[path] = {
                    "output": item.name,
                    "file": name,
                    "platform": self.plan.platform,
                    "sha256": sha,
                    "size": size,
                }
            self.steps.append(
                {
                    "platform": self.plan.platform,
                    "kind": f"files-{item.name}",
                    "state": "succeeded",
                    "seconds": round(time.monotonic() - began, 3),
                    "start_seconds": start,
                }
            )
        return files, details

    def _stage(self) -> int:
        self.stage.mkdir(parents=True, exist_ok=True)
        wanted = {item.target or item.name for item in self.spec.contexts}
        for entry in list(self.stage.iterdir()):
            if entry.name not in wanted:
                _remove(entry)
        changed = 0
        for context in self.spec.contexts:
            try:
                changed += sync_context(
                    self.plan.context_roots[context.name],
                    self.plan.contexts[context.name],
                    self.stage.joinpath(
                        *PurePosixPath(context.target or context.name).parts
                    ),
                )
            except BuildContextError as error:
                raise BuildSpecError(error.code, str(error)) from None
        return changed

    def _shared_commands(self, cwd: Path, env: Mapping[str, str]) -> None:
        if not self.plan.shared_commands:
            return
        if not self.run_shared:
            text = (
                f"[piceli] {self.plan.platform}: {len(self.plan.shared_commands)} "
                "shared command(s) already ran for this build; skipped"
            )
            self.log.line(text)
            self.log.say(text)
            return
        self.shared_target.mkdir(parents=True, exist_ok=True)
        for index, command in enumerate(self.plan.shared_commands, start=1):
            self._command(index, command, cwd, env, shared=True)

    def _environment(self, shared: bool = False) -> dict[str, str]:
        environment = {
            key: os.environ[key] for key in _PASSTHROUGH if key in os.environ
        }
        directories: list[str] = []
        for path, _ in self.plan.tools.values():
            if str(path.parent) not in directories:
                directories.append(str(path.parent))
        environment["PATH"] = os.pathsep.join([*directories, os.defpath])
        target = self.shared_target if shared else self.target
        environment["CARGO_TARGET_DIR"] = str(target)
        environment["SOURCE_DATE_EPOCH"] = str(self.spec.source_date_epoch)
        # 0.68.0: declared, even when this build runs none of them (they are
        # for other images): its commands may read their earlier outputs.
        if self.spec.shared_commands:
            environment[SHARED_TARGET_ENV] = str(self.shared_target)
        environment.update(self.plan.shared_env if shared else self.plan.env)
        return environment

    def _toolchains(self, environment: dict[str, str]) -> None:
        """Install ``build.toolchains`` once into the cache and select them."""
        if not self.plan.toolchains:
            return
        from piceli.artifacts.host_toolchains import provision

        start = self._offset()
        began = time.monotonic()
        try:
            self.toolchains = provision(
                self.cache,
                self.plan.toolchains,
                environment,
                self.log.say,
            )
        except BuildSpecError as error:
            raise BuildSpecError(
                error.code, str(error), steps=tuple(self.steps)
            ) from None
        self.steps.append(
            {
                "platform": self.plan.platform,
                "kind": "toolchains",
                "state": "succeeded",
                "seconds": round(time.monotonic() - began, 3),
                "start_seconds": start,
            }
        )

    def _command(
        self,
        index: int,
        command: tuple[str, ...],
        cwd: Path,
        env: Mapping[str, str],
        repaired: bool = False,
        shared: bool = False,
    ) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise BuildSpecError(
                "cancelled", "build cancelled", steps=tuple(self.steps)
            )
        if self.grant.expires_at <= time.time():
            raise BuildSpecError("grant-expired", "build grant expired")
        if self.overlap and self.stop.is_set():
            raise _Stopped()
        path, pin = self.plan.tools[command[0]]
        self._verify_tools()
        if shared:
            # 0.23: once for every platform of the build.
            step = {"platform": "any", "kind": f"shared-{index}", "tool": command[0]}
            total = len(self.plan.shared_commands)
            label = f"shared command {index}/{total} ({command[0]})"
        else:
            step = {
                "platform": self.plan.platform,
                "kind": f"command-{index}",
                "tool": command[0],
            }
            label = f"{self.plan.platform} command {index}/{self.total} ({command[0]})"
        target = self.shared_target if shared else self.target
        log = self.logs[shared]
        log.line(f"### {label} started")
        log.say(f"[piceli] {label}: running")
        tail = OutputTail()

        def output(stream: str, block: bytes) -> None:
            log.feed(stream, block)
            tail.feed(block)

        # 0.78.0: overlapped, the other side's failure stops this command.
        stop: dict[str, Any] = (
            {"cancel": self.stop} if self.overlap and self._runner_stops else {}
        )
        start = self._offset()
        receipt, stdout, stderr = self.runner(
            [str(path), *command[1:]],
            cwd,
            ProcessLimits(self.spec.timeout_seconds, PROCESS_OUTPUT_BUDGET),
            dict(env),
            self.grant.expires_at,
            on_output=output,
            **stop,
        )
        if not tail.data:  # a runner that only returns the output
            tail.feed(bytes(stdout or b"") + bytes(stderr or b""))
        self._verify_tools()
        state = receipt["state"]
        seconds = round(float(receipt["seconds"]), 3)
        self.steps.append(
            {
                **step,
                "state": state,
                "exit_code": receipt["exit_code"],
                "seconds": seconds,
                "start_seconds": start,
            }
        )
        log.end_step()
        if self.overlap and self.stop.is_set() and state == "cancelled":
            text = f"[piceli] {label}: stopped after {seconds}s" + (
                f" because {self.failing} failed" if self.failing else ""
            )
            log.line(f"### {text}")
            log.say(text)
            raise _Stopped()
        log.line(f"### {label} {state} {seconds}s")
        log.say(f"[piceli] {label}: {state} in {seconds}s")
        if state != "succeeded":
            from piceli.dev.pod import clear_crates, clear_incremental, corrupt_crates

            crates, damage = (
                corrupt_crates(list(tail.recent))
                if state != "timed-out" and not repaired
                else ([], None)
            )
            if crates or damage == "incremental":
                # 0.19: a damaged cached artifact (a loader or link failure),
                # not the code: clear those crates and run the step once more.
                # 0.20: an ICE in rustc's incremental state: drop incremental/.
                removed = (
                    clear_incremental(target)
                    if damage == "incremental"
                    else clear_crates(target, crates)
                )
                log.say(
                    f"[piceli] {label}: corrupted cache ({damage}): cleared "
                    f"{', '.join(crates)} ({removed} entries); running once more"
                )
                self.steps[-1]["repaired"] = {"kind": damage, "crates": crates}
                self._command(index, command, cwd, env, repaired=True, shared=shared)
                return
            code = "build-timed-out" if state == "timed-out" else "build-failed"
            secrets = [value for key, value in env.items() if _SECRET_NAME.search(key)]
            if self.overlap:
                self._stop(label)  # 0.78.0: stop the other side now
            raise BuildSpecError(
                code,
                f"host build step failed: {label}"
                if self.overlap
                else "host build step failed",
                steps=tuple(self.steps),
                output_tail=tail.text(secrets),
                first_error=tail.first_error(secrets),
            )

    def _verify_tools(self) -> None:
        for _, pin in self.plan.tools.values():
            try:
                pin.verify()
            except (OSError, ValueError):
                raise BuildSpecError(
                    "tool-pin-mismatch", "a host tool changed", steps=tuple(self.steps)
                ) from None

    def _verify_consumed(self) -> None:
        for context in self.spec.contexts:
            try:
                verify_staged(
                    self.plan.context_roots[context.name],
                    self.plan.contexts[context.name],
                )
            except BuildContextError as error:
                raise BuildSpecError(error.code, str(error)) from None
        origin = self.spec.origin
        if origin is not None:
            try:
                same = (
                    HostBuildSpec.from_toml(origin).spec_sha256 == self.spec.spec_sha256
                )
            except BuildSpecError:
                same = False
            if not same:
                raise BuildSpecError("spec-changed", "build spec changed")

    # -------------------------------------------------------------- images
    def _source(self, root: Path, path: str) -> Path:
        source = root.joinpath(*PurePosixPath(path).parts)
        try:
            resolved = source.resolve(strict=True)
        except OSError:
            raise BuildSpecError(
                "host-output-missing",
                f"the build produced no {path!r} for its outputs",
                steps=tuple(self.steps),
            ) from None
        if not resolved.is_relative_to(root.resolve()):
            raise BuildSpecError(
                "host-output-missing", "an image file leaves its directory"
            )
        return source

    def _image(
        self, image: HostImage, images_dir: Path, bases: dict[str, BaseImage]
    ) -> dict[str, Any]:
        platform, epoch = self.plan.platform, self.spec.source_date_epoch
        base = None
        if image.base is not None:
            key = image.base.ref
            if key not in bases:
                self.log.say(f"[piceli] base {image.base.image}: pulling by digest")
                bases[key] = pull_base(
                    key,
                    platform,
                    self.store,
                    client_factory=self.client_factory,
                    cancel=self.cancel,
                )
            base = bases[key]
        layers: list[tuple[Descriptor, str]] = []
        files: list[tuple[str, str, int]] = []
        #: 0.83.0: the layer of each file mapping, by its container path.
        mappings: list[dict[str, str]] = []
        if image.dirs:
            entries = [
                LayerEntry(str(parent), "dir", 0o755)
                for item in image.dirs
                for parent in reversed(PurePosixPath(item.path).parents)
                if str(parent) != "."
            ] + [
                LayerEntry(item.path, "dir", item.mode, uid=item.uid, gid=item.gid)
                for item in image.dirs
            ]
            written = write_layer(self.store, entries, mtime=epoch, cancel=self.cancel)
            layers.append((written.descriptor, "directories"))
        sources = (
            [(self.stage, src, dest) for src, dest in image.files]
            + [(self.target, src, dest) for src, dest in image.target_files]
            + [(self.shared_target, src, dest) for src, dest in image.shared_files]
        )
        for root, src, dest in sorted(sources, key=lambda item: item[2]):
            entries = collect(self._source(root, src), dest)
            written = write_layer(self.store, entries, mtime=epoch, cancel=self.cancel)
            layers.append((written.descriptor, f"copy {dest}"))
            mappings.append({"mapping": dest, "digest": written.descriptor.digest})
            files.extend(written.files)
        built = assemble(
            self.store,
            base,
            layers,
            ImageSettings(
                entrypoint=image.entrypoint,
                cmd=image.cmd,
                user=image.user,
                workdir=image.workdir,
                env=image.env,
                labels=image.labels,
            ),
            platform,
            epoch=epoch,
        )
        archive = write_archive(self.store, built, images_dir / f"{image.name}.oci.tar")
        with archive.open("rb") as stream:
            push = scan_image_stream(stream, max_bytes=MAX_ARCHIVE_BYTES)
        if (
            push.config_digest != built.config.digest
            or push.manifest_digest != built.manifest.digest
            or "/".join(push.platform.split("/")[:2]) != platform
        ):
            raise BuildSpecError(
                "host-build-invalid", "assembled image is inconsistent"
            )
        own = {item.digest for item, _ in layers}
        self.log.say(
            f"[piceli] image {image.name}: {built.manifest.digest[7:19]} "
            f"({len(built.layers)} layers, {len(own)} built here)"
        )
        return {
            "image_id": built.config.digest,
            "digest": built.manifest.digest,
            "manifest_size": built.manifest.size,
            "platform": platform,
            "ref": f"{image.repository}@{built.manifest.digest}",
            "archive": f"images/{image.name}.oci.tar",
            "size_bytes": sum(item.size for item in built.layers) + built.config.size,
            "base": (
                {"reference": base.reference, "manifest": base.manifest_digest}
                if base is not None
                else None
            ),
            "layers": [
                {
                    "digest": item.digest,
                    "diff_id": item.diff_id,
                    "size": item.size,
                    "origin": "build" if item.digest in own else "base",
                }
                for item in built.layers
            ],
            # 0.83.0: one layer per declared file mapping (the run's
            # summary and the deploy history list them).
            "mappings": mappings,
            "files": [
                {"path": "/" + path, "sha256": sha, "size": size}
                for path, sha, size in sorted(files)
            ],
        }
