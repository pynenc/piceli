"""Explicit node targets and bounded, shell-free process transports for delivery.

A target URL names *where* a node runtime lives and *which* runtime CLI imports
images. Transports only build argument vectors; they never run anything on
import and never interpret a string through a local shell.
"""

from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, BinaryIO, Protocol, TypeVar
from urllib.parse import parse_qsl, urlsplit

from piceli.artifacts.process import ProcessLimits, ToolPin, _run_process

T = TypeVar("T")

RUNTIMES = ("containerd", "k3s-containerd")
_HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
_USER = re.compile(r"[a-z_][a-z0-9_.-]{0,31}")
_CONTAINER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_NAMESPACE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,75}")
# Every remote argument is one of a small fixed vocabulary, an image reference or
# a digest. Nothing a caller supplies can reach a remote shell unquoted.
_REMOTE_ARG = re.compile(r"[A-Za-z0-9_.:/@=+-]{1,512}")
_TRUE = {"1", "true", "yes"}
_FALSE = {"0", "false", "no"}


@dataclass(frozen=True)
class NodeTarget:
    """A parsed ``ssh://`` or ``docker://`` delivery target."""

    transport: str
    address: str
    runtime: str
    namespace: str = "k8s.io"
    user: str | None = field(default=None, repr=False)
    port: int | None = None
    sudo: bool = False

    def __post_init__(self) -> None:
        if self.transport not in {"ssh", "docker"}:
            raise ValueError("unsupported delivery transport")
        pattern = _HOST if self.transport == "ssh" else _CONTAINER
        if not isinstance(self.address, str) or not pattern.fullmatch(self.address):
            raise ValueError("invalid delivery target address")
        if self.runtime not in RUNTIMES:
            raise ValueError("unsupported node runtime")
        if not isinstance(self.namespace, str) or not _NAMESPACE.fullmatch(
            self.namespace
        ):
            raise ValueError("invalid containerd namespace")
        if self.user is not None and (
            self.transport != "ssh" or not _USER.fullmatch(self.user)
        ):
            raise ValueError("invalid ssh user")
        if self.port is not None and (
            self.transport != "ssh"
            or isinstance(self.port, bool)
            or not isinstance(self.port, int)
            or not 0 < self.port < 65536
        ):
            raise ValueError("invalid ssh port")
        if not isinstance(self.sudo, bool):
            raise ValueError("invalid sudo flag")

    @classmethod
    def parse(cls, url: str) -> NodeTarget:
        """Parse ``ssh://[user@]host[:port]?runtime=...`` or ``docker://name?...``.

        Query parameters: ``runtime`` (required: ``containerd`` or
        ``k3s-containerd``), ``namespace`` (default ``k8s.io``) and ``sudo``
        (default ``true`` for ssh, ``false`` for docker).
        """
        if not isinstance(url, str) or len(url) > 1024 or not url.isprintable():
            raise ValueError("invalid delivery target")
        if url.startswith(("registry://", "oci://")):
            raise ValueError(
                "registry delivery is not a node target; use RegistryDelivery"
            )
        parts = urlsplit(url)
        if parts.scheme not in {"ssh", "docker"} or parts.path or parts.fragment:
            raise ValueError("delivery target must be ssh:// or docker://")
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        query = dict(pairs)
        if len(query) != len(pairs) or set(query) - {"runtime", "namespace", "sudo"}:
            raise ValueError("unknown or repeated delivery target option")
        if "runtime" not in query:
            raise ValueError("delivery target requires an explicit runtime")
        sudo_text = query.get("sudo", "true" if parts.scheme == "ssh" else "false")
        if sudo_text not in _TRUE | _FALSE:
            raise ValueError("invalid sudo flag")
        if parts.password is not None:
            raise ValueError("credentials cannot be part of a delivery target")
        try:
            port = parts.port
        except ValueError as error:
            raise ValueError("invalid ssh port") from error
        return cls(
            transport=parts.scheme,
            address=parts.hostname if parts.scheme == "ssh" else parts.netloc,  # type: ignore[arg-type]
            runtime=query["runtime"],
            namespace=query.get("namespace", "k8s.io"),
            user=parts.username,
            port=port,
            sudo=sudo_text in _TRUE,
        )

    def public(self) -> dict[str, object]:
        """Receipt view: host/container, runtime and namespace; never the user."""
        result: dict[str, object] = {
            "transport": self.transport,
            "host" if self.transport == "ssh" else "container": self.address,
            "runtime": self.runtime,
            "namespace": self.namespace,
            "sudo": self.sudo,
        }
        if self.port is not None:
            result["port"] = self.port
        return result

    @property
    def identity(self) -> str:
        """Canonical target string a grant binds to (user excluded)."""
        port = f":{self.port}" if self.port is not None else ""
        sudo = "true" if self.sudo else "false"
        return (
            f"{self.transport}://{self.address}{port}?runtime={self.runtime}"
            f"&namespace={self.namespace}&sudo={sudo}"
        )

    def runtime_argv(self, *arguments: str) -> list[str]:
        """The node-side argv of one runtime CLI call (``ctr`` or ``k3s ctr``)."""
        base = ["ctr"] if self.runtime == "containerd" else ["k3s", "ctr"]
        argv = [*(["sudo", "-n"] if self.sudo else []), *base, "-n", self.namespace]
        argv.extend(arguments)
        for argument in argv:
            if not _REMOTE_ARG.fullmatch(argument):
                raise ValueError("unsafe node runtime argument")
        return argv


@dataclass(frozen=True)
class Transport:
    """Build local argv that runs a node-side argv through ssh or ``docker exec``."""

    target: NodeTarget
    docker: ToolPin | None = None
    docker_socket: Path | None = field(default=None, repr=False)
    ssh: ToolPin | None = None

    def __post_init__(self) -> None:
        if self.target.transport == "docker":
            if self.docker is None or self.docker_socket is None:
                raise ValueError("docker transport requires a pinned docker tool")
            if not self.docker_socket.is_absolute():
                raise ValueError("explicit absolute Docker socket required")
        elif self.ssh is None:
            raise ValueError("ssh transport requires a pinned ssh tool")

    def tools(self) -> tuple[ToolPin, ...]:
        tool = self.docker if self.target.transport == "docker" else self.ssh
        assert tool is not None
        return (tool,)

    def argv(self, node_argv: Sequence[str], *, stdin: bool = False) -> list[str]:
        if self.target.transport == "docker":
            assert self.docker is not None and self.docker_socket is not None
            return [
                str(self.docker.path),
                "--host",
                f"unix://{self.docker_socket}",
                "exec",
                *(["-i"] if stdin else []),
                self.target.address,
                *node_argv,
            ]
        assert self.ssh is not None
        options = ["-o", "BatchMode=yes", "-T"]
        if self.target.port is not None:
            options += ["-p", str(self.target.port)]
        if self.target.user is not None:
            options += ["-l", self.target.user]
        # ssh hands the command to the remote login shell; each argument is
        # validated against a fixed character set and then quoted.
        return [
            str(self.ssh.path),
            *options,
            "--",
            self.target.address,
            " ".join(shlex.quote(argument) for argument in node_argv),
        ]


@dataclass(frozen=True)
class RunResult:
    state: str
    exit_code: int | None
    stdout: bytes = field(repr=False)
    seconds: float


class Runner(Protocol):
    """Process seam: the subprocess implementation, or a fake in tests."""

    def capture(
        self, argv: list[str], limits: ProcessLimits, env: Mapping[str, str]
    ) -> RunResult: ...

    def feed(
        self,
        argv: list[str],
        writer: Callable[[BinaryIO], None],
        limits: ProcessLimits,
        env: Mapping[str, str],
        cancel: threading.Event | None = None,
    ) -> RunResult: ...

    def read(
        self,
        argv: list[str],
        reader: Callable[[BinaryIO], T],
        limits: ProcessLimits,
        env: Mapping[str, str],
        cancel: threading.Event | None = None,
    ) -> tuple[RunResult, T]: ...


def _kill(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _drain(stream: IO[bytes], keep: bytearray, maximum: int) -> None:
    while block := stream.read(65536):
        if len(keep) < maximum:
            keep.extend(block[: maximum - len(keep)])


class SubprocessRunner:
    """POSIX process groups, no shell, explicit environment, bounded output."""

    def capture(
        self, argv: list[str], limits: ProcessLimits, env: Mapping[str, str]
    ) -> RunResult:
        receipt, stdout, _ = _run_process(
            argv, Path("/"), limits, environment=dict(env)
        )
        return RunResult(
            receipt["state"], receipt["exit_code"], stdout, receipt["seconds"]
        )

    def _start(
        self,
        argv: list[str],
        env: Mapping[str, str],
        *,
        stdin: bool,
    ) -> subprocess.Popen[bytes]:
        if os.name != "posix":
            raise ValueError("bounded process-group execution currently requires POSIX")
        return subprocess.Popen(
            argv,
            cwd="/",
            env={"PATH": os.defpath, "LANG": "C", **env},
            stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

    def _supervise(
        self,
        proc: subprocess.Popen[bytes],
        limits: ProcessLimits,
        cancel: threading.Event | None,
        streams: Sequence[IO[bytes]],
    ) -> tuple[threading.Event, list[bytearray], list[threading.Thread]]:
        finished = threading.Event()
        deadline = time.monotonic() + limits.max_seconds

        def watchdog() -> None:
            while not finished.wait(0.05):
                if time.monotonic() >= deadline or (
                    cancel is not None and cancel.is_set()
                ):
                    _kill(proc)
                    return

        buffers = [bytearray() for _ in streams]
        threads = [
            threading.Thread(
                target=_drain,
                args=(stream, buffer, limits.max_output_bytes),
                daemon=True,
            )
            for stream, buffer in zip(streams, buffers, strict=True)
        ]
        threads.append(threading.Thread(target=watchdog, daemon=True))
        for thread in threads:
            thread.start()
        return finished, buffers, threads

    @staticmethod
    def _finish(
        proc: subprocess.Popen[bytes],
        started: float,
        limits: ProcessLimits,
        cancel: threading.Event | None,
        finished: threading.Event,
        threads: list[threading.Thread],
        failed: bool,
    ) -> tuple[str, int | None, float]:
        try:
            code = proc.wait(timeout=max(0.1, limits.max_seconds))
        except subprocess.TimeoutExpired:
            _kill(proc)
            code = proc.wait(timeout=5)
        finished.set()
        _kill(proc)
        for thread in threads:
            thread.join(timeout=5)
        elapsed = time.monotonic() - started
        if cancel is not None and cancel.is_set():
            state = "cancelled"
        elif elapsed >= limits.max_seconds:
            state = "timed-out"
        elif failed or code != 0:
            state = "failed"
        else:
            state = "succeeded"
        return state, code, elapsed

    def feed(
        self,
        argv: list[str],
        writer: Callable[[BinaryIO], None],
        limits: ProcessLimits,
        env: Mapping[str, str],
        cancel: threading.Event | None = None,
    ) -> RunResult:
        """Stream ``writer`` output into ``argv``'s stdin.

        If ``writer`` raises, stdin is closed without further bytes (so a
        partially received tar has no index) and the exception propagates.
        """
        started = time.monotonic()
        proc = self._start(argv, env, stdin=True)
        assert proc.stdin is not None and proc.stdout is not None
        assert proc.stderr is not None
        finished, buffers, threads = self._supervise(
            proc, limits, cancel, (proc.stdout, proc.stderr)
        )
        failure: BaseException | None = None
        try:
            writer(proc.stdin)  # type: ignore[arg-type]
        except BrokenPipeError:
            pass
        except BaseException as error:  # re-raised after the process is reaped
            failure = error
        finally:
            try:
                proc.stdin.close()
            except BrokenPipeError:
                pass
        if failure is not None:
            # Give the receiver a moment to fail on the truncated stream on its
            # own, then fence the process group.
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        state, code, elapsed = self._finish(
            proc, started, limits, cancel, finished, threads, failure is not None
        )
        if failure is not None:
            raise failure
        return RunResult(state, code, bytes(buffers[0]), elapsed)

    def read(
        self,
        argv: list[str],
        reader: Callable[[BinaryIO], T],
        limits: ProcessLimits,
        env: Mapping[str, str],
        cancel: threading.Event | None = None,
    ) -> tuple[RunResult, T]:
        """Hand ``argv``'s stdout to ``reader``; the process is always reaped."""
        started = time.monotonic()
        proc = self._start(argv, env, stdin=False)
        assert proc.stdout is not None and proc.stderr is not None
        finished, _, threads = self._supervise(proc, limits, cancel, (proc.stderr,))
        try:
            value = reader(proc.stdout)  # type: ignore[arg-type]
        except BaseException:
            _kill(proc)
            self._finish(proc, started, limits, cancel, finished, threads, True)
            proc.stdout.close()
            raise
        # Discard trailing padding so the producer can exit instead of blocking.
        while proc.stdout.read(65536):
            pass
        state, code, elapsed = self._finish(
            proc, started, limits, cancel, finished, threads, False
        )
        proc.stdout.close()
        return RunResult(state, code, b"", elapsed), value
