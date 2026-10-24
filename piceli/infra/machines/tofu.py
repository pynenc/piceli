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
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
    """``text`` with every secret value replaced (longest first).

    Also as a JSON string spells it (``-json`` output escapes new lines and
    quotes), so a multi-line secret is replaced there too.
    """
    forms = {s for s in secrets if s} | {json.dumps(s)[1:-1] for s in secrets if s}
    for value in sorted(forms, key=len, reverse=True):
        text = text.replace(value, REDACTED)
    return text


def _json_lines(text: str) -> Iterator[dict[str, Any]]:
    for line in text.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            yield item


@dataclass(frozen=True)
class TofuResult:
    code: int
    stdout: str
    stderr: str
    #: The arguments it ran with (for the failure log).
    args: tuple[str, ...] = ()

    def errors(self) -> list[dict[str, Any]]:
        """Error diagnostics of ``-json`` output (already redacted): ``summary``,
        ``detail`` and, when OpenTofu gives them, the ``address`` of the
        resource or data source it failed on and the configuration ``file``
        and ``line``."""
        found: list[dict[str, Any]] = []
        for item in _json_lines(self.stdout):
            diagnostic = item.get("diagnostic")
            if item.get("type") != "diagnostic" or not isinstance(diagnostic, dict):
                continue
            if diagnostic.get("severity") != "error":
                continue
            entry: dict[str, Any] = {
                "summary": str(diagnostic.get("summary") or ""),
                "detail": str(diagnostic.get("detail") or ""),
            }
            if isinstance(diagnostic.get("address"), str):
                entry["address"] = diagnostic["address"]
            where = diagnostic.get("range")
            if isinstance(where, dict) and isinstance(where.get("filename"), str):
                entry["file"] = where["filename"]
                line = (where.get("start") or {}).get("line")
                if isinstance(line, int):
                    entry["line"] = line
            found.append(entry)
        return found

    def diagnostics(self) -> list[str]:
        """Error summaries from ``-json`` output (already redacted), each
        prefixed with the address it failed on; else the tail of stderr."""
        found: list[str] = []
        for error in self.errors():
            text = f"{error['summary']} {error['detail']}".strip()
            address = error.get("address")
            if address and not text.startswith(address):
                text = f"{address}: {text}"
            found.append(text)
        if not found and self.code != 0:
            tail = [line for line in self.stderr.splitlines() if line.strip()][-8:]
            found.extend(tail)
        return found

    def log_text(self) -> str:
        """The whole run as text for a failure log (already redacted):
        each ``-json`` line's message, each error in full, then stderr."""
        lines = [f"$ tofu {' '.join(self.args)}".rstrip(), f"exit {self.code}"]
        for item in _json_lines(self.stdout):
            message = item.get("@message")
            if isinstance(message, str):
                lines.append(f"[{item.get('@level') or 'info'}] {message}")
            diagnostic = item.get("diagnostic")
            if item.get("type") == "diagnostic" and isinstance(diagnostic, dict):
                if diagnostic.get("address"):
                    lines.append(f"  address: {diagnostic['address']}")
                where = diagnostic.get("range")
                if isinstance(where, dict) and where.get("filename"):
                    start = (where.get("start") or {}).get("line")
                    lines.append(f"  at: {where['filename']}:{start}")
                for row in str(diagnostic.get("detail") or "").splitlines():
                    lines.append(f"  {row}")
        plain = [line for line in self.stdout.splitlines() if not line.startswith("{")]
        if plain:
            lines += ["--- stdout", *plain]
        if self.stderr.strip():
            lines += ["--- stderr", *self.stderr.splitlines()]
        return "\n".join(lines) + "\n"


@dataclass
class Tofu:
    """A located, version-checked ``tofu`` binary."""

    path: Path
    version: tuple[int, int, int]
    secrets: list[str] = field(default_factory=list)

    @property
    def version_text(self) -> str:
        return ".".join(map(str, self.version))

    def redaction(self, env: Mapping[str, str]) -> list[str]:
        """The values a run's output is redacted of: the known secrets and
        the sensitive variables of ``env``."""
        return [*self.secrets, *(v for k, v in env.items() if _secret_name(k))]

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: float = 1800,
        redacted: bool = True,
    ) -> TofuResult:
        """Run ``tofu ARGS`` in ``cwd`` (stdin closed; output redacted).

        ``redacted=False`` only for reading the S3 keys out of the state
        (:mod:`piceli.infra.machines.keys`), whose output is parsed and
        dropped, never shown or kept."""
        child = {k: os.environ[k] for k in PASSTHROUGH if os.environ.get(k)}
        child.update(
            {"TF_IN_AUTOMATION": "1", "TF_INPUT": "0", "CHECKPOINT_DISABLE": "1"}
        )
        child.update(env)
        secrets = self.redaction(env)
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
        ran = tuple(args)
        if not redacted:
            return TofuResult(process.returncode, stdout, redact(stderr, secrets), ran)
        return TofuResult(
            process.returncode, redact(stdout, secrets), redact(stderr, secrets), ran
        )


def _secret_name(key: str) -> bool:
    # Piceli passes only sensitive variables (user data) as TF_VAR_*.
    return (
        key == "TF_ENCRYPTION"
        or key.startswith("TF_VAR_")
        or key.endswith(("_TOKEN", "_PASSWORD", "_SECRET", "_API_KEY"))
    )


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
