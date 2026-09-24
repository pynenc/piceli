"""Fixed rejection codes and tool discovery for ``artifacts deliver``.

A delivery input problem is reported as a :class:`DeliveryInputError` whose
``code`` is one fixed, secret-free word (``docker-tool-required``,
``node-registry-required`` ...). The message may say more, but only the code
is ever printed by the CLI: never a path, a credential or tool output.

Tools are found the way ``build-spec`` finds docker: an explicit path wins,
otherwise ``PATH`` is searched. Either way the path is resolved to the real
file (no symbolic link) and pinned by SHA-256; the receipt records the pin.
Importing this module runs nothing.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from pathlib import Path

from piceli.artifacts.process import ProcessLimits, ToolPin


class DeliveryInputError(ValueError):
    """An invalid or unavailable delivery input, identified by a fixed ``code``."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


def discover_tool(
    name: str, path: Path | None = None, sha256: str | None = None
) -> ToolPin:
    """Pin ``name``: the explicit ``path`` or the first match on ``PATH``.

    The pin is taken on the resolved real file. An explicit ``sha256`` must
    match it; otherwise the digest is computed now and recorded.
    """
    code = f"{name}-tool-required"
    if path is None:
        found = shutil.which(name)
        if found is None:
            raise DeliveryInputError(code, f"{name} not found on PATH")
        path = Path(found)
    try:
        real = path.absolute().resolve(strict=True)
        captured = ToolPin.capture(real)
    except (OSError, ValueError) as error:
        raise DeliveryInputError(code, f"{name} is not a usable executable") from error
    if sha256 is not None and sha256 != captured.sha256:
        raise DeliveryInputError("tool-pin-mismatch", f"{name} differs from its pin")
    return captured


_DOCKER_ENV = ("HOME", "DOCKER_CONFIG", "DOCKER_CONTEXT")


def _unix_socket(value: str) -> Path:
    if not value.startswith("unix://"):
        raise DeliveryInputError(
            "docker-socket-required", "only a unix:// Docker endpoint is supported"
        )
    socket = Path(value[len("unix://") :])
    if not socket.is_absolute():
        raise DeliveryInputError("docker-socket-required", "relative Docker socket")
    return socket


def discover_docker_socket(docker: ToolPin, explicit: Path | None = None) -> Path:
    """The Docker daemon's unix socket: explicit, ``DOCKER_HOST``, or the context.

    ``docker context inspect`` runs through the pinned tool with only the
    Docker locator variables in its environment. TCP/SSH endpoints are
    refused: delivery always talks to a local unix socket.
    """
    if explicit is not None:
        if not explicit.is_absolute():
            raise DeliveryInputError(
                "docker-socket-required", "explicit absolute Docker socket required"
            )
        return explicit
    host = os.environ.get("DOCKER_HOST")
    if host:
        return _unix_socket(host)
    from piceli.artifacts.node_transport import SubprocessRunner

    env = {key: os.environ[key] for key in _DOCKER_ENV if key in os.environ}
    result = SubprocessRunner().capture(
        [
            str(docker.path),
            "context",
            "inspect",
            "--format",
            "{{.Endpoints.docker.Host}}",
        ],
        ProcessLimits(20, 65536),
        env,
    )
    value = result.stdout.decode("utf-8", "replace").strip()
    if result.state != "succeeded" or not value:
        raise DeliveryInputError(
            "docker-socket-required", "the Docker context has no endpoint"
        )
    return _unix_socket(value)


def verify_tools(tools: Mapping[str, ToolPin]) -> None:
    """Re-check every pin; a changed binary is ``tool-pin-mismatch``."""
    for name, tool in tools.items():
        try:
            tool.verify()
        except (OSError, ValueError) as error:
            raise DeliveryInputError(
                "tool-pin-mismatch", f"{name} differs from its pin"
            ) from error
