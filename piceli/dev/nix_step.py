"""The short Nix steps on a build host: the input fetch and the publisher (0.26.1).

Before and after a Nix build, the client runs a standard-library script on
the build host (:mod:`piceli.dev.nix_fetch`, :mod:`piceli.dev.nix_publish`),
with its request on stdin: a native host's own Python, or on a docker host
a short ``docker run --rm -i`` with the host's ``/nix`` (its daemon)
mounted, as the build run mounts it.

**The image** (:func:`step_image`) is the one the build run uses: the
host's, else the profile's, else the ``DevBuilds`` builder image. A docker
host with none is refused before anything runs (``dev-nix-invalid``).

**The user.** The build run is user ``65532`` with a ``HOME`` it creates
and owns. The builder image has no ``passwd`` entry for that user, and two
programs of these steps need one: Nix ignores a ``HOME`` its user does not
own and then looks the user up (``cannot determine user's home
directory``), and ``ssh`` (a deploy key) refuses to start without one
(``No user exists for uid 65532``). So the container starts as root with
only ``SETUID`` and ``SETGID``, and :data:`BOOT` adds the entry when the
image lacks it, drops to ``65532`` (losing every capability), makes a home
that user owns and only then runs the step, which reads its request
(credentials included) as ``65532``. The container is removed when it
ends; nothing on the host changes.

Importing this module is side-effect free.
"""

from __future__ import annotations

import sys
from typing import Any

from piceli.dev.model import DevError

#: The build run's user and group in a docker host's container.
UID = 65532
GID = 65532

#: Runs first in the step's ``python3 -I -c``; the step's source is
#: ``sys.argv[1]`` and runs as ``__main__`` once the user is ``UID``.
BOOT = f"""\
import os, sys, tempfile
uid, gid, name = {UID}, {GID}, "piceli"
if os.geteuid() == 0:
    import grp, pwd
    try:
        pwd.getpwuid(uid)
    except KeyError:
        with open("/etc/passwd", "a") as handle:
            handle.write(f"{{name}}:x:{{uid}}:{{gid}}:{{name}}:/tmp/{{name}}-home:/bin/sh\\n")
    try:
        grp.getgrgid(gid)
    except KeyError:
        with open("/etc/group", "a") as handle:
            handle.write(f"{{name}}:x:{{gid}}:\\n")
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
try:
    owned = os.stat(os.environ.get("HOME") or "/").st_uid == os.geteuid()
except OSError:
    owned = False
if not owned:
    os.environ["HOME"] = tempfile.mkdtemp(prefix="piceli-home-")
os.environ.setdefault("USER", name)
source = sys.argv.pop(1)
exec(compile(source, "<piceli-nix-step>", "exec"), {{"__name__": "__main__"}})
"""


def step_image(host: Any, profile: Any) -> str | None:
    """The image of ``host``'s Nix steps: the one its build run uses (the
    host's, else ``profile``'s; a profile from ``DevBuilds.profile()``
    carries the builder image when it names none)."""
    return getattr(host, "image", None) or getattr(profile, "image", None) or None


def check_image(host: Any, image: str | None) -> str:
    """The image a docker ``host`` runs its Nix steps in.

    :raises DevError: ``dev-nix-invalid`` when there is none."""
    found = getattr(host, "image", None) or image
    if not found or str(found) == "None":
        raise DevError(
            "dev-nix-invalid",
            f"build host {host.name} has no image for the Nix steps (the input "
            "fetch, the publisher): give BuildHost.docker(image=...), "
            "DevProfile(image=...) or DevBuilds(image=...), the image the build "
            "run uses",
        )
    return str(found)


def step_argv(host: Any, image: str | None, source: str) -> list[str]:
    """How ``source`` runs on ``host``: a native host's Python, or a short
    container of the run image on a docker host (:data:`BOOT` first)."""
    if host.kind == "native":
        return [sys.executable, "-I", "-c", source]
    if host.kind == "docker":
        from piceli.dev.hosts import NIX_MOUNT, ContainerCli

        run_image = check_image(host, image)
        return ContainerCli(host).argv(
            "run", "--rm", "-i", "--user", "0:0", "--cap-drop", "ALL",
            "--cap-add", "SETUID", "--cap-add", "SETGID",
            "--security-opt", "no-new-privileges", *NIX_MOUNT,
            "--entrypoint", "python3", run_image, "-I", "-c", BOOT, source,
        )  # fmt: skip
    raise DevError(
        "dev-nix-host-unsupported",
        f"build host {host.name} is a cluster node; Nix builds run on docker "
        "or native hosts",
    )


def failure_tail(stderr: bytes | str | None, secrets: list[str]) -> str:
    """The line that says why a step failed, from the last lines it printed
    (masked): the docker or Nix error; ``""`` when it printed nothing."""
    from piceli.dev.nix_fetch import mask, reason

    text = stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else stderr
    lines = [mask(line, secrets) for line in (text or "").splitlines() if line.strip()]
    return reason(lines[-20:]) if lines else ""
