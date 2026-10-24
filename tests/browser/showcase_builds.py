"""Builds for the showcase UI's Builds page (fake data).

:func:`seed_builds` adds to the showcase's controller status a NixRelease
publish waiting for its approval (started by hand from the CLI) and an image
build a build host is running, with its progress. :func:`live_builds` keeps
that build's log tail growing while the context is open, as a build host's
``build-progress`` reports do. Nothing here reads a cluster or a host.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

BUILD = "e" * 20
DIGEST = "sha256:" + "7" * 64


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def seed_builds(status: dict[str, Any]) -> None:
    commit = "9" * 40
    status["nix_releases"]["web-2"] = {
        "state": "approval-required", "tag": "web-v1.3.0", "commit": commit,
        "trigger": "cli web-v1.3.0", "trigger_kind": "cli", "requested_by": "ana",
        "builder": "mini", "requested_at": _now(), "plan_digest": DIGEST,
        "approve_command": f"piceli gitops approve web-2 {DIGEST}",
        "plan": {
            "schema": "piceli.nix-release-plan.v1", "release": "web-2",
            "attribute": ".#nixosConfigurations.web-2.config.system.build.toplevel",
            "name": "web-2", "commit": commit, "tag": "web-v1.3.0", "source": "hosts",
            "builder": "mini", "manifest": f"piceli/web-2/{commit}.json",
            "pointers": ["piceli/web-2/refs/tags/web-v1.3.0.json"],
        },
    }  # fmt: skip
    main = status["envs"]["main"]["revision"]
    status["controller"]["builds_wanted"] = [
        {
            "id": BUILD, "kind": "components", "owner": "main", "images": ["api", "web"],
            "commits": dict(main), "requested_at": _now(),
            "claimed": {"host": "laptop", "at": _now()},
            "progress": {"stage": "build", "at": _now(),
                         "log_tail": "   Compiling api v0.4.0 (/src/api)"},
        }
    ]  # fmt: skip


@contextmanager
def live_builds(status: dict[str, Any]) -> Iterator[None]:
    """Append a line to the running build's log tail every half second."""
    stop = threading.Event()
    progress = status["controller"]["builds_wanted"][0]["progress"]

    def grow() -> None:
        n = 0
        while not stop.wait(0.5):
            n += 1
            lines = progress["log_tail"].splitlines()[-30:]
            lines.append(f"   Compiling dependency-{n} v1.{n}.0")
            progress["log_tail"] = "\n".join(lines)

    thread = threading.Thread(target=grow, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=5)
