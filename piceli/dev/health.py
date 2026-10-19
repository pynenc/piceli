"""Build host health (0.22.0): health events and quarantine.

A machine with faulty memory or a failing CPU does not stop: its builds
fail in odd ways (an internal compiler error, an undefined symbol at link,
a segfault in ``rustc`` or the linker) or, worse, pass with a corrupt
artifact. Piceli records a **health event** for the build host whenever a
build or development run on it shows one of the signatures in
:data:`piceli.dev.pod.HEALTH_SIGNATURES`, or a cached artifact's bytes no
longer match their recorded digest.

After ``quarantine_after`` events within ``quarantine_window`` (default 2
in 24 hours; :class:`~piceli.dev.model.BuildHost`) the host is
**quarantined**: ``piceli dev run`` skips it as if it were down (with the
reason), ``piceli builder serve`` on it stops claiming builds and says why.
``piceli dev release-host NAME`` clears its events, once the machine is
fixed or found innocent.

The events live with the host, so they hold whether or not the cluster is
reachable, and every client of the host sees the same ones:

- **docker** host: one labelled, empty named volume per event on the host's
  engine (``piceli-health-<host>-<time>-<random>``); creating a volume needs
  no image and works locally and over ``ssh://``;
- **native** host: ``health.json`` in its ``state_dir``.

The GitOps controller keeps its own record (``build_health`` in its state)
for its cluster builder node and for the hosts that answer it
(:mod:`piceli.infra.controller`).

Importing this module is side-effect free.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from piceli.dev.model import QUARANTINE_AFTER, BuildHost

#: Labels of a docker host's event volumes.
HEALTH_LABEL = "piceli.io/build-health"
HEALTH_AT_LABEL = "piceli.io/build-health-at"
HEALTH_SOURCE_LABEL = "piceli.io/build-health-source"
HEALTH_DETAIL_LABEL = "piceli.io/build-health-detail"
#: Events older than this are dropped when a new one is recorded.
KEEP_SECONDS = 30 * 86400
#: Events up to this far ahead of ``now`` still count: hosts' clocks differ
#: (a docker host's events are written by every client of it), and a check
#: right after recording must see the event it just wrote.
CLOCK_SKEW = 300.0


def _recent(item: HealthEvent, now: float, window: float) -> bool:
    return -CLOCK_SKEW <= now - item.at <= window


#: Events a native host's file keeps at most.
MAX_EVENTS = 200
DETAIL_CHARS = 200
#: Where events come from.
SOURCES = ("dev-run", "builder-serve", "build-job", "cache", "build-failed")


def iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _duration(seconds: float) -> str:
    if seconds % 3600 == 0:
        return f"{int(seconds // 3600)}h"
    if seconds % 60 == 0:
        return f"{int(seconds // 60)}m"
    return f"{int(seconds)}s"


@dataclass(frozen=True)
class HealthEvent:
    """One sign that a build host may be faulty."""

    at: float
    kind: str
    detail: str = ""
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": iso(self.at),
            # Down, never up: a stored time is never after the event.
            "epoch": int(self.at * 1000) / 1000,
            "kind": self.kind,
            "detail": self.detail,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> HealthEvent:
        return cls(
            float(value.get("epoch") or 0),
            str(value.get("kind") or "unknown"),
            str(value.get("detail") or "")[:DETAIL_CHARS],
            str(value.get("source") or ""),
        )


def event(
    signal: Mapping[str, Any], source: str, now: float | None = None
) -> HealthEvent:
    """A health event from a signal (``{"kind", "line"}``, see
    :func:`piceli.dev.pod.health_signal`)."""
    return HealthEvent(
        time.time() if now is None else now,
        str(signal.get("kind") or "unknown"),
        " ".join(str(signal.get("line") or signal.get("detail") or "").split())[
            :DETAIL_CHARS
        ],
        source,
    )


def quarantine(
    events: Iterable[HealthEvent],
    *,
    after: int | None,
    window: float,
    now: float,
) -> dict[str, Any] | None:
    """Whether ``events`` quarantine their host: ``None``, or ``{"since",
    "events", "kinds", "reason"}`` (``after`` events within ``window``
    seconds before ``now``)."""
    if after is None:
        return None
    recent = sorted(
        (item for item in events if _recent(item, now, window)),
        key=lambda item: item.at,
    )
    if len(recent) < after:
        return None
    kinds = sorted({item.kind for item in recent})
    last = recent[-1]
    return {
        "since": iso(recent[after - 1].at),
        "events": len(recent),
        "kinds": kinds,
        "reason": (
            f"{len(recent)} health events within {_duration(window)} "
            f"({', '.join(kinds)}); last {iso(last.at)}"
            + (f": {last.detail}" if last.detail else "")
        ),
    }


def view(
    events: Sequence[HealthEvent],
    *,
    after: int | None,
    window: float,
    now: float,
) -> dict[str, Any]:
    """A host's health as status and ``piceli dev hosts`` show it."""
    found = quarantine(events, after=after, window=window, now=now)
    ordered = sorted(events, key=lambda item: item.at)
    return {
        "quarantined": found is not None,
        "reason": None if found is None else found["reason"],
        "since": None if found is None else found["since"],
        "recent": sum(1 for item in events if _recent(item, now, window)),
        "policy": {"after": after, "window_seconds": int(window)},
        "events": [item.to_dict() for item in ordered[-10:]],
    }


class HealthStore(Protocol):
    """Where one build host's events live (see the module doc)."""

    def events(self) -> list[HealthEvent]: ...

    def record(self, item: HealthEvent) -> None: ...

    def clear(self) -> int: ...


class FileHealth:
    """A native host's events: ``health.json`` (locked while changed)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _read(self) -> list[HealthEvent]:
        try:
            value = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return []
        items = value.get("events") if isinstance(value, dict) else None
        return [
            HealthEvent.from_dict(item)
            for item in items or ()
            if isinstance(item, Mapping)
        ]

    def _change(self, change: Callable[[list[HealthEvent]], list[HealthEvent]]) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_name(self.path.name + ".lock")
        descriptor = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            before = self._read()
            after = change(before)
            temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}")
            temporary.write_text(
                json.dumps(
                    {"events": [item.to_dict() for item in after]},
                    indent=2,
                    sort_keys=True,
                )
            )
            os.replace(temporary, self.path)
            return len(before) - len(after)
        finally:
            os.close(descriptor)

    def events(self) -> list[HealthEvent]:
        return self._read()

    def record(self, item: HealthEvent) -> None:
        self._change(
            lambda found: [old for old in found if item.at - old.at <= KEEP_SECONDS][
                -(MAX_EVENTS - 1) :
            ]
            + [item]
        )

    def clear(self) -> int:
        return self._change(lambda _found: [])


def _label_text(value: str) -> str:
    """A label value the CLI's ``k=v,k=v`` listing keeps whole."""
    return " ".join(value.replace(",", ";").split())[:DETAIL_CHARS]


class DockerHealth:
    """A docker host's events: labelled empty volumes on its engine."""

    def __init__(self, cli: Any) -> None:
        self.cli = cli
        self.host = cli.host.name

    def _listed(self) -> list[tuple[str, dict[str, str]]]:
        from piceli.dev.hosts import HOST_LABEL, _labels

        text = self.cli.text(
            "volume", "ls",
            "--filter", f"label={HOST_LABEL}={self.host}",
            "--filter", f"label={HEALTH_LABEL}",
            "--format", "{{.Name}}\t{{.Labels}}",
            timeout=60,
        )  # fmt: skip
        found = []
        for line in text.splitlines():
            name, _, labels = line.partition("\t")
            if name.strip():
                found.append((name.strip(), _labels(labels)))
        return found

    def events(self) -> list[HealthEvent]:
        found = []
        for _name, labels in self._listed():
            try:
                at = float(labels.get(HEALTH_AT_LABEL) or 0)
            except ValueError:
                continue
            found.append(
                HealthEvent(
                    at,
                    labels.get(HEALTH_LABEL) or "unknown",
                    labels.get(HEALTH_DETAIL_LABEL, ""),
                    labels.get(HEALTH_SOURCE_LABEL, ""),
                )
            )
        return found

    def record(self, item: HealthEvent) -> None:
        from piceli.dev.hosts import HOST_LABEL

        name = f"piceli-health-{self.host}-{int(item.at * 1000)}-{os.urandom(3).hex()}"
        self.cli.run(
            "volume", "create",
            "--label", f"{HOST_LABEL}={self.host}",
            "--label", f"{HEALTH_LABEL}={_label_text(item.kind)}",
            "--label", f"{HEALTH_AT_LABEL}={item.at:.3f}",
            "--label", f"{HEALTH_SOURCE_LABEL}={_label_text(item.source)}",
            "--label", f"{HEALTH_DETAIL_LABEL}={_label_text(item.detail)}",
            name,
            timeout=60,
        )  # fmt: skip
        old = [
            name
            for name, labels in self._listed()
            if item.at - _float(labels.get(HEALTH_AT_LABEL)) > KEEP_SECONDS
        ]
        if old:
            self.cli.run("volume", "rm", *old, timeout=60)

    def clear(self) -> int:
        names = [name for name, _labels in self._listed()]
        if names:
            self.cli.run("volume", "rm", *names, timeout=120)
        return len(names)


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def native_root(host: BuildHost) -> Path:
    """A native host's state directory (its cache, queue and health)."""
    return Path(
        host.state_dir or Path.home() / ".cache" / "piceli" / "dev" / host.name
    ).expanduser()


def store_for(host: BuildHost, cli: Any = None) -> HealthStore | None:
    """Where ``host``'s events live; ``None`` for a cluster host (the GitOps
    controller keeps those)."""
    if host.kind == "native":
        return FileHealth(native_root(host) / "health.json")
    if host.kind == "docker":
        if cli is None:
            from piceli.dev.hosts import ContainerCli

            cli = ContainerCli(host)
        return DockerHealth(cli)
    return None


class Health:
    """One build host's health: its store and its declared policy."""

    def __init__(
        self,
        host: BuildHost,
        store: HealthStore,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.host = host
        self.store = store
        self.clock = clock

    @classmethod
    def of(
        cls, host: BuildHost, cli: Any = None, clock: Callable[[], float] = time.time
    ) -> Health | None:
        store = store_for(host, cli)
        return None if store is None else cls(host, store, clock)

    def _policy(self) -> tuple[int | None, int]:
        return self.host.quarantine_policy()

    def quarantined(self) -> dict[str, Any] | None:
        """The quarantine (``{"since", "reason", ...}``), or ``None``."""
        after, window = self._policy()
        return quarantine(
            self.store.events(), after=after, window=window, now=self.clock()
        )

    def view(self) -> dict[str, Any]:
        after, window = self._policy()
        return view(self.store.events(), after=after, window=window, now=self.clock())

    def record(self, signal: Mapping[str, Any], source: str) -> dict[str, Any] | None:
        """Record one event; the quarantine it leads to, if any."""
        self.store.record(event(signal, source, self.clock()))
        return self.quarantined()

    def release(self) -> int:
        """Clear every event (``piceli dev release-host``); how many."""
        return self.store.clear()


def describe_quarantine(name: str, found: Mapping[str, Any]) -> str:
    return (
        f"build host {name} is quarantined since {found.get('since')}: "
        f"{found.get('reason')} (piceli dev release-host {name} clears it)"
    )


__all__ = [
    "QUARANTINE_AFTER",
    "DockerHealth",
    "FileHealth",
    "Health",
    "HealthEvent",
    "event",
    "quarantine",
    "store_for",
    "view",
]
