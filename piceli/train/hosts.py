"""Steps on build hosts: the train's commands spread over its hosts.

A train names where its steps run (``Train(hosts=[...])``, a step's own
``hosts``): ``local`` (this machine, :class:`~piceli.train.runner.LocalRunner`
in one of its slots) and the build hosts of ``Train(builds=...)``, the same
:class:`~piceli.dev.model.BuildHost` declarations ``piceli dev run`` uses.

A command on a build host is a ``piceli dev run`` of the merged checkout
(:class:`DevHostRunner`): the same upload (a delta of what the host's
mirrors lack), the host's own queue and slots (a busy host queues the
command; every client of the host shares the queue), its quarantine, and
the profile's lineage of the host's warm cache, so a cargo step on the
host builds in that repository's shared target directory. Only the
variables the train and the step set travel with it, never the caller's
environment. Declared outputs come back as the run's artifacts.

:class:`HostRunner` places each command: the first of its hosts that is up
(and not quarantined) with a free slot, else the first that is up (it
queues there). A host that turns out unavailable when the command starts
hands it to the next one, as ``piceli dev run`` does; with
``Train(fallback="local")`` a command whose hosts are all down runs here.
The result's ``host`` is where it ran.

Importing this module is side-effect free.
"""

from __future__ import annotations

import shutil
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from piceli.train.model import LOCAL, TrainError
from piceli.train.runner import Command, Completed, LocalRunner, now_iso

#: The run's requester priority on a build host's queue.
PRIORITY = "round"


class PlacedRunner(Protocol):
    """A runner that can say whether it can take a command now."""

    host: str

    def state(self) -> tuple[bool, int, str]:
        """``(up, free slots, why not)``."""
        ...

    def run(self, command: Command) -> Completed: ...


def artifact_globs(outputs: Sequence[str]) -> list[str]:
    """The ``dev run --artifact`` globs of a step's outputs (a directory's
    files too)."""
    found: list[str] = []
    for output in outputs:
        found += [output, f"{output.rstrip('/')}/**/*"]
    return found


class DevHostRunner:
    """Commands on one build host, through the ``piceli dev run`` client.

    :param port: ``(host, dev) -> port`` (default: the host's
        :class:`~piceli.dev.hosts.DockerPort` or
        :class:`~piceli.dev.hosts.NativePort`); tests pass a fake.
    :param probe: ``host -> HostState`` (default :func:`piceli.dev.hosts.probe`).
    """

    def __init__(
        self,
        host: Any,
        dev: Any,
        *,
        source: str,
        requester: str,
        profile: str | None = None,
        port: Callable[[Any, Any], Any] | None = None,
        probe: Callable[[Any], Any] | None = None,
        record_health: bool = True,
    ) -> None:
        self.build_host = host
        self.host = str(host.name)
        self.dev = dev
        self.source = source
        self.requester = requester
        self.profile = profile
        self._port = port
        self._probe = probe
        self.record_health = record_health

    def state(self) -> tuple[bool, int, str]:
        if self.build_host.kind == "cluster":
            return False, 0, "in-cluster hosts take no train steps"
        probe = self._probe
        if probe is None:
            from piceli.dev.hosts import probe as probe_host

            probe = probe_host
        state = probe(self.build_host)
        if not state.up:
            return False, 0, state.detail or "down"
        if state.quarantined is not None:
            return False, 0, f"quarantined: {state.quarantined}"
        free = 0 if state.waiting else max(0, self.build_host.slots - state.running)
        return True, free, ""

    def port(self) -> Any:
        if self._port is not None:
            return self._port(self.build_host, self.dev)
        from piceli.dev.hosts import DockerPort, NativePort

        if self.build_host.kind == "docker":
            return DockerPort(self.build_host, self.dev)
        return NativePort(self.build_host, self.dev)

    def run(self, command: Command) -> Completed:
        from piceli.dev.client import RunRequest
        from piceli.dev.client import run as dev_run
        from piceli.dev.hosts import UNAVAILABLE
        from piceli.dev.model import DevError
        from piceli.dev.pack import SourceRequest
        from piceli.tempfiles import temporary_directory

        command.log.parent.mkdir(parents=True, exist_ok=True)
        started, clock = time.time(), time.monotonic()
        argv = list(command.argv)
        if command.step_env:
            argv = [
                "env",
                *(f"{key}={value}" for key, value in sorted(command.step_env.items())),
                *argv,
            ]
        lines: list[str] = []
        with (
            command.log.open("a", encoding="utf-8", errors="replace") as log,
            temporary_directory("host-run") as scratch,
        ):
            log.write(
                f"$ {' '.join(command.argv)}  # {now_iso(started)} on {self.host}\n"
            )
            log.flush()

            def say(text: str) -> None:
                log.write(f"[{self.host}] {text}\n")
                log.flush()

            def output(text: str) -> None:
                lines.append(text)
                log.write(text + "\n")

            request = RunRequest(
                root=SourceRequest(self.source, command.cwd, None),
                command=argv,
                cwd=".",
                profile=self.profile,
                requester=self.requester,
                priority=PRIORITY,
                artifacts=artifact_globs(command.outputs),
                artifacts_dir=Path(scratch),
                queue_timeout=float(command.timeout or 3600.0),
            )
            try:
                result = dev_run(
                    self.port(), self.dev, request, say=say, log=output,
                    host=self.build_host,
                )  # fmt: skip
            except DevError as error:
                say(f"refused: {error} ({error.code})")
                return Completed(
                    None, started, time.monotonic() - clock, self.host,
                    f"{error} ({error.code})",
                    error="unavailable" if error.code == UNAVAILABLE else "not-started",
                )  # fmt: skip
            self._outputs(result, Path(scratch), command.cwd)
            if self.record_health:
                self._health(result, say)
        duration = time.monotonic() - clock
        state = result.get("state")
        text = "\n".join(lines)
        if result.get("reason") == UNAVAILABLE:
            return Completed(
                None, started, duration, self.host,
                str(result.get("message") or "unavailable"), error="unavailable",
            )  # fmt: skip
        if state == "timed-out":
            return Completed(None, started, duration, self.host, text, "timeout")
        if state in ("passed", "failed"):
            code = result.get("exit_code")
            code = 0 if state == "passed" else (code if code not in (None, 0) else 1)
            return Completed(int(code), started, duration, self.host, text)
        message = str(result.get("message") or result.get("reason") or state)
        return Completed(
            None, started, duration, self.host, f"{text}\n{message}".strip(),
            error="not-started",
        )  # fmt: skip

    @staticmethod
    def _outputs(result: Mapping[str, Any], scratch: Path, cwd: Path) -> None:
        """Move the run's artifacts (the step's outputs) into the checkout."""
        run = str(result.get("run") or "")
        base = scratch / run
        for name in result.get("artifacts") or ():
            path = Path(str(name))
            if not path.is_file() or not path.is_relative_to(base):
                continue
            dest = cwd / path.relative_to(base)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), dest)

    def _health(self, result: Mapping[str, Any], say: Callable[[str], None]) -> None:
        """A run that showed a machine-health signature records an event on
        its host, as ``piceli dev run`` does (0.22.0)."""
        signal = result.get("health")
        if not isinstance(signal, Mapping) or self.build_host.kind == "cluster":
            return
        from piceli.dev.health import Health
        from piceli.dev.model import DevError

        try:
            health = Health.of(self.build_host)
            if health is not None:
                health.record(dict(signal), "train")
        except (DevError, OSError) as error:
            say(f"health event not recorded ({error})")
            return
        say(f"health event: {signal.get('kind')}")


class HostRunner:
    """Places each command on one of the train's hosts (see the module doc).

    :param runners: ``{name: runner}``; ``local`` is this machine.
    :param default: Where a command without ``hosts`` runs, in order.
    :param fallback: ``"local"``: run here when every host named is down.
    """

    host = LOCAL

    def __init__(
        self,
        runners: Mapping[str, PlacedRunner],
        *,
        default: Sequence[str] = (LOCAL,),
        fallback: str | None = LOCAL,
    ) -> None:
        self.runners = dict(runners)
        self.default = tuple(default)
        self.fallback = fallback
        #: Commands this runner has running on each host (parallel trials
        #: prefer a host it is not using yet).
        self.inflight: dict[str, int] = {}
        #: Told ``(command log, host)`` when a command goes to a host (the
        #: run's ``step-placed`` event); it never changes the placement.
        self.on_place: Callable[[Path, str], None] | None = None
        self._lock = threading.Lock()

    @property
    def local(self) -> PlacedRunner:
        found = self.runners.get(LOCAL)
        if found is None:
            found = self.runners[LOCAL] = LocalRunner()
        return found

    def names(self, wanted: Sequence[str]) -> tuple[list[str], list[str]]:
        """``(known host names, in order; notes on the unknown ones)``."""
        notes: list[str] = []
        names: list[str] = []
        for name in wanted or self.default:
            if name in self.runners or name == LOCAL:
                if name not in names:
                    names.append(name)
            else:
                notes.append(f"{name}: not a build host of this train; skipped")
        if not names:
            names = list(self.default)
        return names, notes

    def free_slots(self, wanted: Sequence[str] = ()) -> int:
        """Free slots now over the hosts of ``wanted`` (else the default)."""
        names, _ = self.names(wanted)
        total = 0
        for name in names:
            up, free, _why = self._runner(name).state()
            if up:
                total += free
        return total

    def host_count(self) -> int:
        """How many hosts the default names (a host with several slots counts
        each slot)."""
        names, _ = self.names(())
        total = 0
        for name in names:
            runner = self._runner(name)
            slots = getattr(getattr(runner, "slots", None), "count", None)
            build = getattr(runner, "build_host", None)
            total += int(slots or getattr(build, "slots", 1) or 1)
        return total

    def _runner(self, name: str) -> PlacedRunner:
        return self.local if name == LOCAL else self.runners[name]

    def run(self, command: Command) -> Completed:
        if command.stdout:
            return self.local.run(command)
        names, notes = self.names(command.hosts)
        states = []
        with self._lock:
            busy = dict(self.inflight)
        for index, name in enumerate(names):
            up, free, why = self._runner(name).state()
            if not up:
                notes.append(f"{name}: {why or 'down'}; skipped")
                continue
            states.append((0 if free > 0 else 1, busy.get(name, 0), index, name))
        order = [name for _free, _busy, _index, name in sorted(states)]
        if not order and self.fallback == LOCAL and LOCAL not in names:
            notes.append("every host named is down: running here (fallback local)")
            order = [LOCAL]
        for position, name in enumerate(order):
            with self._lock:
                self.inflight[name] = self.inflight.get(name, 0) + 1
            if self.on_place is not None:
                try:
                    self.on_place(command.log, name)
                except Exception:  # an observer never changes the run
                    pass
            try:
                done = self._runner(name).run(command)
            finally:
                with self._lock:
                    self.inflight[name] -= 1
            if done.error == "unavailable":
                notes.append(f"{name}: unavailable at start ({done.output[-200:]})")
                if (
                    position + 1 == len(order)
                    and self.fallback == LOCAL
                    and (LOCAL not in order)
                ):
                    order.append(LOCAL)
                    notes.append("running here (fallback local)")
                continue
            return Completed(
                done.exit_code, done.started, done.duration, done.host,
                done.output, done.error, (*notes, *done.notes),
            )  # fmt: skip
        raise TrainError(
            "train-host-unavailable",
            "no host could take the step: " + "; ".join(notes or ["none named"]),
        )


def build_runner(
    train: Any,
    repo: Path,
    *,
    port: Callable[[Any, Any], Any] | None = None,
    probe: Callable[[Any], Any] | None = None,
) -> HostRunner:
    """The runner of ``train``: this machine (its slots, warm cargo targets)
    and the build hosts of ``train.builds``."""
    from piceli.train.local import CargoCache, Slots, cache_root

    root = cache_root(train.cache)
    runners: dict[str, PlacedRunner] = {
        LOCAL: LocalRunner(
            Slots(root / "slots", train.local_slots),
            CargoCache(
                root, train.name, repo, target=train.target_dir, sccache=train.sccache
            ),
        )
    }
    dev = train.builds
    if dev is not None and not isinstance(dev, str):
        from piceli.dev.pack import repository_name

        source = repository_name(repo)
        for host in dev.hosts:
            if host.name == LOCAL or not host.runs:
                continue
            runners[host.name] = DevHostRunner(
                host, dev, source=source, requester=f"train-{train.name}",
                port=port, probe=probe,
            )  # fmt: skip
    return HostRunner(runners, default=train.hosts, fallback=train.fallback)
