"""Where model-written code runs: a scratch directory with no cluster and no network.

Every command runs with the current interpreter in a fresh temporary directory:

- ``HOME`` and ``KUBECONFIG`` point at empty directories inside it, so neither
  ``~/.kube/config`` nor an inherited kubeconfig or context can be reached;
- the environment is built from scratch (no provider keys, tokens, cloud
  credentials or ``PICELI_*`` settings are inherited) and ``PATH`` holds only
  the interpreter's directory and ``/usr/bin:/bin`` (no ``kubectl``, ``helm``
  or cloud CLIs from ``/usr/local`` or package managers);
- a ``sitecustomize`` module refuses every socket connection and name lookup
  except loopback, and proxies point at a closed loopback port, so the only
  "cluster" reachable is the in-process fake API (``piceli.testing``) the
  harness starts for a task.

This keeps honest mistakes (and the naive mock answers) away from real clusters
and the network. It is not a security boundary against hostile code: run evals
of real models in a disposable container or VM.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any

COMMAND_TIMEOUT_SECONDS = 120
OUTPUT_LIMIT = 4000
HASH = re.compile(r"^[0-9a-f]{64}$")

NETWORK_GUARD = '''\
"""Eval sandbox: only loopback connections are allowed."""
import socket

_LOOPBACK = ("127.", "::1", "localhost")


def _allowed(host):
    return host is None or str(host).startswith(_LOOPBACK) or str(host) == ""


def _check(address):
    if isinstance(address, tuple) and address and not _allowed(address[0]):
        raise PermissionError("network access is disabled in the eval sandbox")


_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex
_getaddrinfo = socket.getaddrinfo


def connect(self, address):
    _check(address)
    return _connect(self, address)


def connect_ex(self, address):
    _check(address)
    return _connect_ex(self, address)


def getaddrinfo(host, *args, **kwargs):
    if not _allowed(host if not isinstance(host, bytes) else host.decode()):
        raise socket.gaierror("network access is disabled in the eval sandbox")
    return _getaddrinfo(host, *args, **kwargs)


socket.socket.connect = connect
socket.socket.connect_ex = connect_ex
socket.getaddrinfo = getaddrinfo
'''


@dataclass
class CommandResult:
    """One command run in the sandbox."""

    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    approved: str | None = None  # the hash the simulated owner approved

    def as_record(self, sandbox: Sandbox) -> dict[str, Any]:
        """A JSON-ready, anonymized record."""
        record: dict[str, Any] = {
            "command": " ".join(["piceli", *self.argv]),
            "exit_code": self.returncode,
            "stdout": sandbox.anonymize(self.stdout[-OUTPUT_LIMIT:]),
            "stderr": sandbox.anonymize(self.stderr[-OUTPUT_LIMIT:]),
        }
        if self.approved:
            record["owner_approved"] = self.approved
        return record


@dataclass
class Sandbox:
    """A scratch workspace (``ws``) with an empty home and no kube configuration."""

    root: Path = field(init=False)
    _tmp: tempfile.TemporaryDirectory[str] = field(init=False, repr=False)
    extra_anonymize: dict[str, str] = field(default_factory=dict)

    def __enter__(self) -> Sandbox:
        self._tmp = tempfile.TemporaryDirectory(prefix="piceli-eval-")
        self.root = Path(self._tmp.name).resolve()
        for name in ("ws", "home", "kube", "guard", "tmp"):
            (self.root / name).mkdir()
        (self.root / "guard" / "sitecustomize.py").write_text(NETWORK_GUARD)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._tmp.cleanup()

    @property
    def ws(self) -> Path:
        """The working directory commands run in."""
        return self.root / "ws"

    def env(self) -> dict[str, str]:
        """The whole environment of a sandboxed command (nothing inherited)."""
        interpreter_dir = str(Path(sys.executable).parent)
        dead_proxy = "http://127.0.0.1:9"
        return {
            "PATH": f"{interpreter_dir}:/usr/bin:/bin",
            "HOME": str(self.root / "home"),
            "KUBECONFIG": str(self.root / "kube"),
            "TMPDIR": str(self.root / "tmp"),
            "XDG_CONFIG_HOME": str(self.root / "home" / ".config"),
            "XDG_CACHE_HOME": str(self.root / "home" / ".cache"),
            "PYTHONPATH": str(self.root / "guard"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUTF8": "1",
            "LANG": "C.UTF-8",
            "NO_COLOR": "1",
            "COLUMNS": "200",
            "HTTP_PROXY": dead_proxy,
            "HTTPS_PROXY": dead_proxy,
            "http_proxy": dead_proxy,
            "https_proxy": dead_proxy,
            "NO_PROXY": "127.0.0.1,localhost,::1",
            "no_proxy": "127.0.0.1,localhost,::1",
        }

    def run(
        self, argv: list[str], timeout: float = COMMAND_TIMEOUT_SECONDS
    ) -> CommandResult:
        """Run ``argv`` in the workspace."""
        try:
            completed = subprocess.run(
                argv,
                cwd=self.ws,
                env=self.env(),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return CommandResult(argv, 124, "", f"timed out after {timeout:.0f}s")
        return CommandResult(
            argv, completed.returncode, completed.stdout, completed.stderr
        )

    def piceli(self, *args: str) -> CommandResult:
        """Run ``piceli ARGS`` (as ``python -m piceli``) in the workspace."""
        result = self.run([sys.executable, "-m", "piceli", *args])
        result.argv = list(args)
        return result

    def python(self, *args: str) -> CommandResult:
        """Run ``python ARGS`` in the workspace."""
        return self.run([sys.executable, *args])

    def anonymize(self, text: str) -> str:
        """Replace local paths and ports in saved output."""
        import piceli

        package = Path(piceli.__file__).resolve().parent
        replacements = {
            str(self.root): "<sandbox>",
            self._tmp.name: "<sandbox>",
            str(package): "<piceli>",
            sys.prefix: "<python>",
            sys.base_prefix: "<python>",
            str(package.parent): "<checkout>",
            str(Path.home()): "~",
            **self.extra_anonymize,
        }
        for path, label in replacements.items():
            text = text.replace(path, label)
        return re.sub(r"127\.0\.0\.1:\d+", "127.0.0.1:<port>", text)


def approval_hash(result: CommandResult) -> str | None:
    """The hash a command asked the owner to approve, if any."""
    text = result.stderr + "\n" + result.stdout
    shown = re.findall(r"--approve[ =]([0-9a-f]{64})", text)
    if shown:
        return str(shown[-1])
    for line in reversed(result.stdout.strip().splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("state") != "rejected":
            for key in ("combined_hash", "plan_hash"):
                if isinstance(data.get(key), str) and HASH.match(data[key]):
                    return str(data[key])
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return None
    if isinstance(data, dict) and data.get("state") == "approval-required":
        value = data.get("plan_hash")
        return str(value) if isinstance(value, str) else None
    return None


def with_owner_approval(
    args: list[str], shown: str | None
) -> tuple[list[str], str | None]:
    """Fill the ``--approve`` value with the hash the owner was shown.

    The simulated owner approves exactly what the last plan printed. A literal
    hash the model made up is left alone (Piceli refuses it), and nothing is
    approved before a plan was shown.
    """
    out = list(args)
    for index, token in enumerate(out):
        if token == "--approve" and index + 1 < len(out):
            value = out[index + 1]
            if shown and not HASH.match(value) and not value.startswith("-"):
                out[index + 1] = shown
                return out, shown
        elif token.startswith("--approve="):
            value = token.split("=", 1)[1]
            if shown and not HASH.match(value):
                out[index] = f"--approve={shown}"
                return out, shown
    return out, None


def copy_tree(source: Path, target: Path) -> None:
    """Copy a fixture directory into the workspace."""
    shutil.copytree(source, target, dirs_exist_ok=True)
