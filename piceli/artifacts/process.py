"""Pinned tools, explicit code-execution authority and bounded process groups."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import selectors
import signal
import subprocess
import threading
import time
from typing import Any

from piceli.artifacts.plan import SourcePin, canonical, digest, validate_digest
from piceli.k8s.ops.bounds import positive, seconds, text


@dataclass(frozen=True)
class ToolPin:
    path: Path = field(repr=False)
    sha256: str

    def __post_init__(self) -> None:
        if not self.path.is_absolute():
            raise ValueError("tool path must be explicit and absolute")
        validate_digest(self.sha256)

    @classmethod
    def capture(cls, path: Path) -> ToolPin:
        resolved = path.resolve(strict=True)
        return cls(resolved, cls._digest(resolved))

    @staticmethod
    def _digest(path: Path) -> str:
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size > 256 * 1024 * 1024
        ):
            raise ValueError("tool must be a bounded regular executable")
        with path.open("rb") as stream:
            return "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()

    def verify(self) -> None:
        if self._digest(self.path) != self.sha256:
            raise ValueError("tool pin changed")


@dataclass(frozen=True)
class ProcessLimits:
    max_seconds: float = 60
    max_output_bytes: int = 1_048_576

    def __post_init__(self) -> None:
        seconds(self.max_seconds, "process", 3600)
        positive(self.max_output_bytes, "process output", 16 * 1024 * 1024)


@dataclass(frozen=True)
class ExecutionGrant:
    plan_hash: str
    expires_at: float
    allow_code_execution: bool = False
    allow_network: bool = False

    def __post_init__(self) -> None:
        validate_digest(self.plan_hash)
        seconds(self.expires_at, "grant expiry", 10_000_000_000)
        if not isinstance(self.allow_code_execution, bool) or not isinstance(
            self.allow_network, bool
        ):
            raise ValueError("invalid execution grant")


@dataclass(frozen=True)
class BuildCommand:
    """An explicitly trusted build step, not an OS sandbox.

    Arbitrary tools can access the network. This adapter therefore requires a
    network-capable execution grant even for a tool with an offline flag. Use
    OciBuilder for guaranteed offline, non-executing file assembly.
    """

    tool: ToolPin
    arguments: tuple[str, ...] = field(repr=False)
    inputs: tuple[SourcePin, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.tool, ToolPin)
            or not isinstance(self.arguments, tuple)
            or len(self.arguments) > 256
            or not isinstance(self.inputs, tuple)
            or len(self.inputs) > 4096
            or not all(isinstance(pin, SourcePin) for pin in self.inputs)
        ):
            raise ValueError("invalid command inputs")
        for arg in self.arguments:
            text(arg, "public command argument", empty=True)

    @property
    def plan_hash(self) -> str:
        return digest(
            canonical(
                {
                    "tool": self.tool.sha256,
                    "arguments": self.arguments,
                    "inputs": [pin.__dict__ for pin in self.inputs],
                }
            )
        )

    def preview(self) -> dict[str, Any]:
        return {
            "plan_hash": self.plan_hash,
            "tool_sha256": self.tool.sha256,
            "source_count": len(self.inputs),
            "requires_code_execution": True,
            "requires_network_capable_grant": True,
            "push": False,
        }

    def execute(
        self,
        root: Path,
        grant: ExecutionGrant,
        *,
        limits: ProcessLimits | None = None,
        cancel: threading.Event | None = None,
    ) -> dict[str, Any]:
        if (
            grant.plan_hash != self.plan_hash
            or not grant.allow_code_execution
            or not grant.allow_network
            or grant.expires_at <= time.time()
        ):
            raise ValueError(
                "exact unexpired code-execution/network-capable grant required"
            )
        for pin in self.inputs:
            pin.read(root)
        self.tool.verify()
        receipt = run_process(
            [str(self.tool.path), *self.arguments],
            root,
            limits or ProcessLimits(),
            cancel=cancel,
            expires_at=grant.expires_at,
        )
        self.tool.verify()
        for pin in self.inputs:
            pin.read(root)
        return {"plan_hash": self.plan_hash, "tool_sha256": self.tool.sha256, **receipt}


def run_process(
    argv: list[str],
    cwd: Path,
    limits: ProcessLimits,
    *,
    cancel: threading.Event | None = None,
    expires_at: float | None = None,
    environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    """No shell, inherited credentials, output persistence or unbounded pipe drain.

    Processes escaping their session cannot be fenced here; use an external
    sandbox for untrusted code. Only explicitly authorized tools may call this.
    """
    receipt, _, _ = _run_process(
        argv,
        cwd,
        limits,
        cancel=cancel,
        expires_at=expires_at,
        environment=environment,
    )
    return receipt


def _run_process(
    argv: list[str],
    cwd: Path,
    limits: ProcessLimits,
    *,
    cancel: threading.Event | None = None,
    expires_at: float | None = None,
    environment: dict[str, str] | None = None,
) -> tuple[dict[str, Any], bytes, bytes]:
    """Run with bounded private output for trusted adapters; callers redact it."""
    if os.name != "posix":
        raise ValueError("bounded process-group execution currently requires POSIX")
    started = time.monotonic()
    if cancel is not None and cancel.is_set():
        return ({"state": "cancelled", "exit_code": None, "seconds": 0.0}, b"", b"")
    deadline = started + limits.max_seconds
    if expires_at is not None:
        deadline = min(deadline, started + max(0, expires_at - time.time()))
    if time.monotonic() >= deadline:
        return ({"state": "timed-out", "exit_code": None, "seconds": 0.0}, b"", b"")
    env = {"PATH": os.defpath, "LANG": "C", **(environment or {})}
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    state = "succeeded"
    consumed = 0
    output = {"stdout": bytearray(), "stderr": bytearray()}
    selector = selectors.DefaultSelector()
    try:
        for pipe in (proc.stdout, proc.stderr):
            assert pipe is not None
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ)
        while selector.get_map() or proc.poll() is None:
            if cancel is not None and cancel.is_set():
                state = "cancelled"
                break
            if time.monotonic() >= deadline:
                state = "timed-out"
                break
            for key, _ in selector.select(
                min(0.02, max(0, deadline - time.monotonic()))
            ):
                block = os.read(key.fd, 65536)
                if not block:
                    selector.unregister(key.fileobj)
                else:
                    consumed += len(block)
                    stream = "stdout" if key.fileobj is proc.stdout else "stderr"
                    output[stream].extend(block)
                    if consumed > limits.max_output_bytes:
                        state = "output-limit"
                        break
            if state != "succeeded":
                break
        if state != "succeeded" or proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        code = proc.wait(timeout=1)
        if state == "succeeded" and code != 0:
            state = "failed"
        return (
            {
                "state": state,
                "exit_code": code,
                "seconds": time.monotonic() - started,
            },
            bytes(output["stdout"]),
            bytes(output["stderr"]),
        )
    finally:
        # A successful parent must not leave forked children running either.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=1)
        selector.close()
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()
