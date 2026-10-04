"""Run OpenTofu: located explicitly, version checked, secrets only in its environment.

The binary is ``--tofu PATH``, else ``$PICELI_TOFU``, else ``tofu`` on
``PATH``; ``tofu version -json`` must report at least
:data:`~piceli.infra.machines.render.MIN_TOFU`. Every run gets a fresh
environment: ``PATH``, ``HOME``, ``TMPDIR``, locale and proxy settings,
OpenTofu's plugin cache and CLI configuration, plus what the command adds
(provider tokens, ``TF_ENCRYPTION``). Nothing secret is ever an argument,
stdin is closed, and output is redacted of every secret value before it is
shown or kept.

Importing this module is side-effect free.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from piceli.infra.machines.model import InfraError
from piceli.infra.machines.render import MIN_TOFU

__all__ = ["Tofu", "TofuResult", "locate", "redact"]

TOFU_ENV = "PICELI_TOFU"
#: Variables passed through from the caller's environment.
PASSTHROUGH = (
    "PATH",
    "HOME",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NIX_SSL_CERT_FILE",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "https_proxy",
    "http_proxy",
    "no_proxy",
    "TF_PLUGIN_CACHE_DIR",
    "TF_CLI_CONFIG_FILE",
)
REDACTED = "<redacted>"
_VERSION = re.compile(r"OpenTofu v(\d+)\.(\d+)\.(\d+)")


def redact(text: str, secrets: Sequence[str]) -> str:
    """``text`` with every secret value replaced (longest first)."""
    for value in sorted({s for s in secrets if s}, key=len, reverse=True):
        text = text.replace(value, REDACTED)
    return text


@dataclass(frozen=True)
class TofuResult:
    code: int
    stdout: str
    stderr: str

    def diagnostics(self) -> list[str]:
        """Error summaries from ``-json`` output (already redacted)."""
        found: list[str] = []
        for line in self.stdout.splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict) and item.get("type") == "diagnostic":
                diagnostic = item.get("diagnostic") or {}
                if diagnostic.get("severity") == "error":
                    found.append(
                        " ".join(
                            str(diagnostic.get(k) or "") for k in ("summary", "detail")
                        ).strip()
                    )
        if not found and self.code != 0:
            tail = [line for line in self.stderr.splitlines() if line.strip()][-8:]
            found.extend(tail)
        return found


@dataclass
class Tofu:
    """A located, version-checked ``tofu`` binary."""

    path: Path
    version: tuple[int, int, int]
    secrets: list[str] = field(default_factory=list)

    @property
    def version_text(self) -> str:
        return ".".join(map(str, self.version))

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float = 1800,
    ) -> TofuResult:
        """Run ``tofu ARGS`` in ``cwd`` (stdin closed; output redacted)."""
        child = {k: os.environ[k] for k in PASSTHROUGH if os.environ.get(k)}
        child.update(
            {"TF_IN_AUTOMATION": "1", "TF_INPUT": "0", "CHECKPOINT_DISABLE": "1"}
        )
        child.update(env)
        secrets = [*self.secrets, *(v for k, v in env.items() if _secret_name(k))]
        process = subprocess.Popen(
            [str(self.path), *args],
            cwd=cwd,
            env=child,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            umask=0o077,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except OSError:
                pass
            try:
                stdout, stderr = process.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass
                stdout, stderr = process.communicate()
            raise InfraError(
                "infra-tofu-failed", "OpenTofu did not finish in time"
            ) from None
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except OSError:
                pass
            process.wait()
            raise
        return TofuResult(
            process.returncode, redact(stdout, secrets), redact(stderr, secrets)
        )


def _secret_name(key: str) -> bool:
    return key == "TF_ENCRYPTION" or key.endswith(("_TOKEN", "_PASSWORD", "_SECRET"))


def locate(explicit: str | None = None) -> Tofu:
    """The ``tofu`` to run: ``explicit``, else ``$PICELI_TOFU``, else ``PATH``."""
    candidate = explicit or os.environ.get(TOFU_ENV) or shutil.which("tofu")
    if not candidate:
        raise InfraError(
            "infra-tofu-missing",
            "OpenTofu (tofu) was not found; install it or pass --tofu PATH",
        )
    path = Path(candidate).expanduser()
    if not path.is_absolute():
        found = shutil.which(str(path))
        if found is None:
            raise InfraError(
                "infra-tofu-missing", "the given tofu is not an executable"
            )
        path = Path(found)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise InfraError("infra-tofu-missing", "the given tofu is not an executable")
    try:
        done = subprocess.run(
            [str(path), "version"],
            env={k: os.environ[k] for k in PASSTHROUGH if os.environ.get(k)}
            | {"CHECKPOINT_DISABLE": "1"},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise InfraError(
            "infra-tofu-unsupported", "`tofu version` did not answer"
        ) from None
    # Only OpenTofu encrypts its state (TF_ENCRYPTION); another binary that
    # accepts the same commands would write it in the clear.
    match = _VERSION.match(done.stdout.strip())
    if match is None:
        raise InfraError("infra-tofu-unsupported", "this binary is not OpenTofu")
    text = f"{match[1]}.{match[2]}.{match[3]}"
    version = (int(match[1]), int(match[2]), int(match[3]))
    if version < MIN_TOFU:
        raise InfraError(
            "infra-tofu-unsupported",
            f"OpenTofu {text} is too old; Piceli needs {'.'.join(map(str, MIN_TOFU))} or later",
        )
    return Tofu(path, version)
