"""Development runs on build hosts (0.20.0).

``DevBuilds(hosts=[BuildHost.docker(...), BuildHost.native(...), ...])``
declares where runs may execute besides the in-cluster builder node. A run
on a host is the same stdlib wrapper (:mod:`piceli.dev.pod`) as in a pod:

- **docker**: in a Linux container of the profile's image, through a
  Docker-compatible CLI (Docker Desktop, OrbStack, colima, Podman, a Linux
  host's engine), on this machine or over the CLI's own ``ssh://``
  transport. :class:`DockerPort` speaks the client's port with the CLI:
  ``run -d`` (start), ``exec`` (upload, have), ``cp`` (artifacts),
  ``logs -f`` (the stream, resumed when it drops), ``inspect`` (why it
  ended), ``rm -f``. The cache is a named volume (``piceli-dev-cache``).
- **native**: the wrapper on this machine itself, no container
  (:class:`NativePort`): macOS-only builds; cache and queue under
  ``state_dir``.

**The queue is on the host**, not in the cluster, so it works with the
cluster or its controller down, and every client of a host shares it.
A docker host's slots are containers named ``piceli-dev-<host>-<n>``: the
engine refuses a second container of a name, so claiming a slot is atomic,
locally and over SSH. A waiting run is an attached container
(``piceli-dev-<host>-wait-<run>``) that goes away with its client. A
native host uses lock files. Waiting runs start in the cluster scheduler's
order (:func:`piceli.dev.scheduler.order`: priority, fair share per
requester, age).

:func:`candidates` orders the declared hosts for a run (requested host,
platform, health, a free slot first); the CLI falls back to the next host
when one turns out unavailable (``dev-host-unavailable``).

Importing this module is side-effect free.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from piceli.dev.jobs import PRIORITY_LABEL, REQUESTER_LABEL, RUN_LABEL
from piceli.dev.model import BuildHost, DevBuilds, DevError

HOST_LABEL = "piceli.io/dev-host"
SLOT_LABEL = "piceli.io/dev-slot"
WAIT_LABEL = "piceli.io/dev-waiting"
CREATED_LABEL = "piceli.io/dev-created"
#: The named volume of a docker host's cache.
CACHE_VOLUME = "piceli-dev-cache"
#: Where a run works inside a docker host's container (the image's /tmp is
#: writable by its user; the container's layer goes with it).
WORK = "/tmp/work"
#: An exited slot container whose client did not remove it (the client
#: died) frees its slot after this long.
STALE_SECONDS = 60.0
_ARCH = {"aarch64": "linux/arm64", "arm64": "linux/arm64",
         "x86_64": "linux/amd64", "amd64": "linux/amd64"}  # fmt: skip
UNAVAILABLE = "dev-host-unavailable"
#: 0.24: how a ``DevProfile(nix=True)`` run (and ``dev nix build
#: --publish``) reaches a docker host's own Nix.
NIX_MOUNT = ("-v", "/nix:/nix:ro", "-e", "NIX_REMOTE=daemon")


def roots_mount(path: str) -> tuple[str, ...]:
    """``BuildHost.roots_dir`` in a Nix step (0.26.2): the host's directory at
    the same path, read-write, so the daemon finds the roots made there."""
    return ("-v", f"{path}:{path}")


NIX_PATH = "/nix/var/nix/profiles/default/bin:/nix/var/nix/profiles/system/sw/bin"


def _iso_ms(seconds: float) -> str:
    moment = datetime.fromtimestamp(seconds, UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _unavailable(host: BuildHost, detail: str) -> DevError:
    return DevError(UNAVAILABLE, f"build host {host.name} is unavailable: {detail}")


# ------------------------------------------------------------------ queue


@dataclass
class Entry:
    """A run in a host's queue: waiting, or holding ``slot``."""

    run: str
    requester: str
    priority: str
    created: str
    slot: int | None = None

    def job(self) -> dict[str, Any]:
        """The Job shape :func:`piceli.dev.scheduler.order` reads."""
        return {
            "metadata": {
                "labels": {
                    RUN_LABEL: self.run,
                    REQUESTER_LABEL: self.requester,
                    PRIORITY_LABEL: self.priority,
                },
                "creationTimestamp": self.created,
            }
        }


class HostQueue:
    """The slots and waiting runs of one host (see the module doc)."""

    def running(self) -> list[Entry]:  # pragma: no cover - interface
        raise NotImplementedError

    def waiting(self) -> list[Entry]:  # pragma: no cover - interface
        raise NotImplementedError


def queue_position(
    queue_waiting: Sequence[Entry], running: Sequence[Entry], run: str
) -> int:
    """``run``'s 0-based place among the waiting runs, in start order."""
    from piceli.dev.scheduler import order

    ordered = order([e.job() for e in queue_waiting], [e.job() for e in running])
    names = [job["metadata"]["labels"][RUN_LABEL] for job in ordered]
    return names.index(run) if run in names else len(names)


def acquire(
    queue: HostQueue,
    entry: Entry,
    slots: int,
    claim: Callable[[int], bool],
    *,
    timeout: float,
    on_wait: Callable[[str], None],
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    poll: float = 1.0,
    host: str = "host",
) -> int:
    """Wait for ``entry``'s turn and a free slot, then ``claim`` it.

    ``entry`` is already registered as waiting. Returns the slot.

    :raises DevError: ``dev-queue-timeout``.
    """
    deadline = clock() + timeout
    shown = ""
    while True:
        running = queue.running()
        waiting = queue.waiting()
        if not any(item.run == entry.run for item in waiting):
            waiting = [*waiting, entry]
        taken = {item.slot for item in running}
        free = [n for n in range(slots) if n not in taken]
        place = queue_position(waiting, running, entry.run)
        if place < len(free):
            for slot in free:
                if claim(slot):
                    return slot
        state = (
            f"queued #{place + 1} of {len(waiting)} on {host} "
            f"({len(running)}/{slots} busy)"
        )
        if state != shown:
            shown = state
            on_wait(state)
        if clock() >= deadline:
            raise DevError(
                "dev-queue-timeout", f"no free slot on {host} within {timeout:.0f}s"
            )
        sleep(poll)


class FileQueue(HostQueue):
    """A native host's queue: lock files in ``directory`` (this machine)."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        (directory / "slots").mkdir(parents=True, exist_ok=True)
        (directory / "waiting").mkdir(parents=True, exist_ok=True)
        self.held: dict[int, int] = {}

    def _alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def register(self, entry: Entry) -> None:
        path = self.directory / "waiting" / f"{entry.run}.json"
        path.write_text(json.dumps({**entry.__dict__, "pid": os.getpid()}))

    def unregister(self, run: str) -> None:
        (self.directory / "waiting" / f"{run}.json").unlink(missing_ok=True)

    def waiting(self) -> list[Entry]:
        found = []
        for path in sorted((self.directory / "waiting").glob("*.json")):
            try:
                value = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if not self._alive(int(value.pop("pid", 0) or 0)):
                path.unlink(missing_ok=True)  # its client died
                continue
            found.append(Entry(**{k: value.get(k) for k in Entry.__dataclass_fields__}))
        return found

    def running(self) -> list[Entry]:
        found = []
        for lock in sorted((self.directory / "slots").glob("*.lock")):
            slot = int(lock.stem)
            if slot in self.held:
                meta = self._meta(slot)
                if meta is not None:
                    found.append(meta)
                continue
            descriptor = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                meta = self._meta(slot)
                found.append(meta or Entry("?", "?", "normal", "", slot))
                continue
            finally:
                os.close(descriptor)
        return found

    def _meta(self, slot: int) -> Entry | None:
        try:
            value = json.loads((self.directory / "slots" / f"{slot}.json").read_text())
        except (OSError, ValueError):
            return None
        return Entry(**{k: value.get(k) for k in Entry.__dataclass_fields__})

    def claim(self, slot: int, entry: Entry) -> bool:
        descriptor = os.open(
            self.directory / "slots" / f"{slot}.lock", os.O_RDWR | os.O_CREAT, 0o600
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(descriptor)
            return False
        self.held[slot] = descriptor
        (self.directory / "slots" / f"{slot}.json").write_text(
            json.dumps({**entry.__dict__, "slot": slot})
        )
        return True

    def release(self, slot: int) -> None:
        descriptor = self.held.pop(slot, None)
        if descriptor is not None:
            (self.directory / "slots" / f"{slot}.json").unlink(missing_ok=True)
            os.close(descriptor)


# ------------------------------------------------------------------ docker


class ContainerCli:
    """A Docker-compatible CLI for one host (local, ``ssh://`` or a context)."""

    def __init__(
        self,
        host: BuildHost,
        runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    ) -> None:
        self.host = host
        self.runner = runner
        self.popen = popen

    def argv(self, *args: str) -> list[str]:
        context = self.host.context
        prefix = [self.host.cli]
        if context and "://" in context:
            prefix += ["--host", context]
        elif context:
            prefix += ["--context", context]
        return [*prefix, *args]

    def run(
        self, *args: str, input: bytes | None = None, timeout: float = 120
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            return self.runner(
                self.argv(*args),
                input=input,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError:
            raise _unavailable(self.host, f"{self.host.cli} is not installed") from None
        except subprocess.TimeoutExpired:
            raise _unavailable(
                self.host, f"{self.host.cli} {args[0] if args else ''} timed out"
            ) from None

    def text(self, *args: str, timeout: float = 120) -> str:
        done = self.run(*args, timeout=timeout)
        return done.stdout.decode("utf-8", "replace") if done.returncode == 0 else ""

    def platform(self, timeout: float = 15) -> str:
        """The engine's own platform (``linux/arm64``).

        :raises DevError: ``dev-host-unavailable`` (no engine reachable).
        """
        done = self.run("info", "--format", "{{.Architecture}}", timeout=timeout)
        if done.returncode != 0:
            detail = done.stderr.decode("utf-8", "replace").strip().splitlines()
            raise _unavailable(self.host, (detail[-1] if detail else "no engine")[:200])
        arch = done.stdout.decode().strip()
        return _ARCH.get(arch, f"linux/{arch}")


def _labels(text: str) -> dict[str, str]:
    found = {}
    for part in text.split(","):
        key, sep, value = part.partition("=")
        if sep:
            found[key.strip()] = value.strip()
    return found


class DockerQueue(HostQueue):
    """A docker host's queue: its containers (see the module doc)."""

    def __init__(self, cli: ContainerCli) -> None:
        self.cli = cli
        self.host = cli.host.name

    def _list(self, label: str) -> list[dict[str, Any]]:
        listed = self.cli.text(
            "ps", "-a",
            "--filter", f"label={HOST_LABEL}={self.host}",
            "--filter", f"label={label}",
            "--format", "{{.Names}}\t{{.State}}\t{{.Labels}}",
        )  # fmt: skip
        found = []
        for line in listed.splitlines():
            name, _, rest = line.partition("\t")
            state, _, labels = rest.partition("\t")
            found.append({"name": name, "state": state, "labels": _labels(labels)})
        return found

    @staticmethod
    def _entry(item: Mapping[str, Any]) -> Entry:
        labels = item["labels"]
        slot = labels.get(SLOT_LABEL)
        return Entry(
            labels.get(RUN_LABEL, "?"),
            labels.get(REQUESTER_LABEL, "?"),
            labels.get(PRIORITY_LABEL, "normal"),
            labels.get(CREATED_LABEL, ""),
            int(slot) if slot is not None and slot.isdigit() else None,
        )

    def running(self) -> list[Entry]:
        # An exited slot container is not busy: claiming its slot removes it
        # once it is stale (its client died; see DockerPort._reap).
        return [
            self._entry(item)
            for item in self._list(SLOT_LABEL)
            if item["state"] in {"running", "created", "restarting", "paused"}
        ]

    def waiting(self) -> list[Entry]:
        return [
            self._entry(item)
            for item in self._list(WAIT_LABEL)
            if item["state"] == "running"
        ]


def slot_name(host: str, slot: int) -> str:
    return f"piceli-dev-{host}-{slot}"


def wait_name(host: str, run: str) -> str:
    return f"piceli-dev-{host}-wait-{run}"


def _env(job: Mapping[str, Any]) -> dict[str, str]:
    container = job["spec"]["template"]["spec"]["containers"][0]
    return {item["name"]: item["value"] for item in container.get("env") or ()}


@dataclass
class _Started:
    """A run this port started: its container and slot."""

    container: str
    slot: int
    waiter: subprocess.Popen[bytes] | None = None


class DockerPort:
    """The client's port on a docker build host (see the module doc).

    ``create`` keeps the run's Job (built by :func:`piceli.dev.jobs.run_job`
    as for the cluster); ``wait_started`` waits in the host's queue, then
    starts the run's container in its slot.
    """

    def __init__(
        self,
        host: BuildHost,
        dev: DevBuilds,
        *,
        cli: ContainerCli | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        poll: float = 1.0,
        platform: str | None = None,
        cache_volume: str = CACHE_VOLUME,
    ) -> None:
        self.host = host
        self.dev = dev
        self.cache_volume = cache_volume
        self.cli = cli or ContainerCli(host)
        self.queue = DockerQueue(self.cli)
        self.clock = clock
        self.sleep = sleep
        self.poll = poll
        self.platform = platform
        self.jobs: dict[str, dict[str, Any]] = {}
        self.started: dict[str, _Started] = {}

    # ------------------------------------------------------------ setup
    def _image(self, job: Mapping[str, Any]) -> str:
        return self.host.image or str(
            job["spec"]["template"]["spec"]["containers"][0]["image"]
        )

    def _ensure(self, image: str, on_wait: Callable[[str], None]) -> None:
        if self.cli.run("image", "inspect", image, timeout=60).returncode != 0:
            on_wait(f"pulling the run image on {self.host.name}")
            pulled = self.cli.run("pull", *self._platform_args(), image, timeout=1800)
            if pulled.returncode != 0:
                tail = pulled.stderr.decode("utf-8", "replace").strip()[-200:]
                raise _unavailable(self.host, f"cannot pull the run image: {tail}")
        volume = self.cache_volume
        if self.cli.run("volume", "inspect", volume, timeout=60).returncode != 0:
            self.cli.run("volume", "create", volume, timeout=60)
            # The image's user owns the cache (the volume starts root's).
            self.cli.run(
                "run", "--rm", "--user", "0", "-v", f"{volume}:/cache",
                "--entrypoint", "chown", image, "65532:65532", "/cache",
                timeout=600,
            )  # fmt: skip

    def _platform_args(self) -> list[str]:
        return ["--platform", self.platform] if self.platform else []

    # ------------------------------------------------------------ port
    def create(self, job: dict[str, Any]) -> None:
        run = job["metadata"]["labels"][RUN_LABEL]
        self.jobs[run] = job

    def wait_started(
        self, run: str, timeout: float, on_wait: Callable[[str], None]
    ) -> str:
        job = self.jobs[run]
        labels = job["metadata"]["labels"]
        image = self._image(job)
        self._ensure(image, on_wait)
        entry = Entry(
            run,
            labels.get(REQUESTER_LABEL, "anonymous"),
            labels.get(PRIORITY_LABEL, "agent"),
            _iso_ms(time.time()),
        )
        waiter = self._wait(entry, image)
        try:
            slot = acquire(
                self.queue,
                entry,
                self.host.slots,
                lambda n: self._claim(n, entry, job, image),
                timeout=timeout,
                on_wait=on_wait,
                clock=self.clock,
                sleep=self.sleep,
                poll=self.poll,
                host=self.host.name,
            )
        finally:
            self._stop_waiting(waiter)
        container = slot_name(self.host.name, slot)
        self.started[run] = _Started(container, slot)
        if json.loads(_env(job)["PICELI_DEV_SPEC"]).get("sources"):
            self._wait_for_have(container)
        return container

    def _wait(self, entry: Entry, image: str) -> subprocess.Popen[bytes]:
        """An attached container that marks ``entry`` waiting; it exits (and is
        removed) when this client closes its stdin or dies."""
        name = wait_name(self.host.name, entry.run)
        try:
            waiter = self.cli.popen(
                self.cli.argv(
                    "run", "--rm", "-i", "--name", name,
                    "--label", f"{HOST_LABEL}={self.host.name}",
                    "--label", f"{WAIT_LABEL}=1",
                    "--label", f"{RUN_LABEL}={entry.run}",
                    "--label", f"{REQUESTER_LABEL}={entry.requester}",
                    "--label", f"{PRIORITY_LABEL}={entry.priority}",
                    "--label", f"{CREATED_LABEL}={entry.created}",
                    "--network", "none", "--memory", "32m",
                    "--entrypoint", "python3", image,
                    "-c", "import sys; sys.stdin.read()",
                ),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )  # fmt: skip
        except FileNotFoundError:
            raise _unavailable(self.host, f"{self.host.cli} is not installed") from None
        deadline = self.clock() + 60
        while self.clock() < deadline:
            if waiter.poll() is not None:
                raise _unavailable(self.host, "the queue marker did not start")
            if any(item.run == entry.run for item in self.queue.waiting()):
                return waiter
            self.sleep(0.2)
        self._stop_waiting(waiter)
        raise _unavailable(self.host, "the queue marker did not start in 60s")

    def _stop_waiting(self, waiter: subprocess.Popen[bytes] | None) -> None:
        if waiter is None:
            return
        try:
            if waiter.stdin is not None:
                waiter.stdin.close()
            waiter.wait(timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            waiter.kill()

    def _claim(
        self, slot: int, entry: Entry, job: Mapping[str, Any], image: str
    ) -> bool:
        name = slot_name(self.host.name, slot)
        argv = self._run_argv(name, slot, entry, job, image)
        started = self.cli.run(*argv, timeout=300)
        if started.returncode == 0:
            return True
        message = started.stderr.decode("utf-8", "replace")
        if "already in use" in message or "Conflict" in message:
            self._reap(name)
            return False
        raise _unavailable(self.host, f"cannot start the run: {message.strip()[-300:]}")

    def _reap(self, name: str) -> None:
        """Free a slot left by a client that died (its container exited a
        while ago and nobody removed it)."""
        raw = self.cli.text("inspect", "--format", "{{json .State}}", name)
        try:
            state = json.loads(raw or "{}")
        except ValueError:
            return
        if state.get("Running") or not state.get("FinishedAt"):
            return
        try:
            finished = (
                datetime.strptime(str(state["FinishedAt"])[:19], "%Y-%m-%dT%H:%M:%S")
                .replace(tzinfo=UTC)
                .timestamp()
            )
        except ValueError:
            return
        if time.time() - finished > STALE_SECONDS:
            self.cli.run("rm", "-f", name, timeout=60)

    def _run_argv(
        self, name: str, slot: int, entry: Entry, job: Mapping[str, Any], image: str
    ) -> list[str]:
        from piceli.dev.model import bytes_of

        env = _env(job)
        spec = json.loads(env["PICELI_DEV_SPEC"])
        # No API server to cut off here; the container is the boundary.
        spec["isolation_check"] = False
        container = job["spec"]["template"]["spec"]["containers"][0]
        limits = (container.get("resources") or {}).get("limits") or {}
        memory = self.host.memory or limits.get("memory")
        argv = [
            "run", "-d", "--name", name,
            "--label", f"{HOST_LABEL}={self.host.name}",
            "--label", f"{SLOT_LABEL}={slot}",
            "--label", f"{RUN_LABEL}={entry.run}",
            "--label", f"{REQUESTER_LABEL}={entry.requester}",
            "--label", f"{PRIORITY_LABEL}={entry.priority}",
            "--label", f"{CREATED_LABEL}={entry.created}",
            "--user", "65532:65532",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", "16384",
            "-v", f"{self.cache_volume}:/cache",
            "-e", f"PICELI_DEV_SPEC={json.dumps(spec, sort_keys=True)}",
            "-e", f"PICELI_DEV_WORK={WORK}",
            "-e", f"HOME={WORK}/home",
            "-e", f"TMPDIR={WORK}/tmp",
            "-e", f"PICELI_DEV_WRAPPER={self._wrapper()}",
            *self._platform_args(),
        ]  # fmt: skip
        if memory:
            argv += ["--memory", str(bytes_of(str(memory)))]
        if self.host.cpu:
            argv += ["--cpus", str(self.host.cpu).rstrip("m")]
        nix = ""
        if spec.get("nix"):
            # 0.24: the host's Nix, read-only: its store and its daemon's
            # socket; builds run in the daemon's own sandbox on the host.
            argv += [*NIX_MOUNT]
            nix = f'export PATH="$PATH:{NIX_PATH}"; '
        fetch = (
            'PICELI_DEV_ROLE=fetch python3 -I -c "$PICELI_DEV_WRAPPER"; '
            if spec.get("sources")
            else ""
        )
        script = (
            f"{nix}mkdir -p {WORK}/home {WORK}/tmp; {fetch}"
            'exec python3 -I -c "$PICELI_DEV_WRAPPER"'
        )
        return [*argv, "--entrypoint", "sh", image, "-c", script]

    @staticmethod
    def _wrapper() -> str:
        from piceli.dev.jobs import wrapper_source

        return wrapper_source()

    def _wait_for_have(self, container: str) -> None:
        deadline = self.clock() + 300
        while self.clock() < deadline:
            if self._exec(container, ["test", "-e", f"{WORK}/have.json"]) == 0:
                return
            if not self._running(container):
                return
            self.sleep(0.3)

    def _running(self, container: str) -> bool:
        return (
            self.cli.text(
                "inspect", "--format", "{{.State.Running}}", container
            ).strip()
            == "true"
        )

    def _exec(
        self,
        container: str,
        command: list[str],
        stdin: Iterator[bytes] | None = None,
        stdout: Callable[[bytes], Any] | None = None,
        timeout: float = 600,
    ) -> int:
        data = b"".join(stdin) if stdin is not None else None
        args = ["exec", *(["-i"] if data is not None else []), container, *command]
        try:
            done = self.cli.run(*args, input=data, timeout=timeout)
        except DevError as error:
            raise DevError(
                "dev-upload-failed", f"exec in the run's container failed: {error}",
                failed=True,
            ) from None  # fmt: skip
        if stdout is not None:
            stdout(done.stdout)
        return done.returncode

    def have(self, pod: str) -> dict[str, Any] | None:
        output = bytearray()
        if self._exec(pod, ["cat", f"{WORK}/have.json"], stdout=output.extend) != 0:
            return None
        try:
            value = json.loads(bytes(output).decode() or "null")
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    def upload(
        self,
        pod: str,
        archive: Path,
        sha256: str | None = None,
        progress: Callable[[int, int, float], None] = lambda _s, _t, _e: None,
    ) -> dict[str, Any]:
        from piceli.dev.cluster import upload_chunks

        return upload_chunks(
            lambda command, stdin: self._exec(pod, command, stdin=stdin, timeout=300),
            archive,
            sha256,
            progress,
            root=WORK,
        )

    def download(self, pod: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        copied = self.cli.run(
            "cp", f"{pod}:{WORK}/out/artifacts.tar.gz", str(dest), timeout=1800
        )
        self._exec(pod, ["touch", f"{WORK}/out/collected"])
        if copied.returncode != 0:
            raise DevError(
                "dev-upload-failed", "the artifacts could not be copied", failed=True
            )

    def follow(self, pod: str, on_line: Callable[[str], None]) -> None:
        """Every line of the run's output until its container ends; a dropped
        stream is resumed (the lines already seen are skipped)."""
        seen = 0
        for _attempt in range(20):
            process = self.cli.popen(
                self.cli.argv("logs", "-f", pod),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            assert process.stdout is not None
            for count, raw in enumerate(process.stdout, start=1):
                if count > seen:
                    seen = count
                    on_line(raw.decode("utf-8", "replace").rstrip("\n"))
            process.wait()
            if not self._running(pod):
                return
            self.sleep(1.0)

    def ended(self, run: str) -> str:
        started = self.started.get(run)
        if started is None:
            return "dev-run-cancelled"
        raw = self.cli.text("inspect", "--format", "{{json .State}}", started.container)
        if not raw.strip():
            return "dev-run-cancelled"
        try:
            state = json.loads(raw)
        except ValueError:
            return "dev-run-failed"
        if state.get("OOMKilled"):
            return "dev-run-out-of-memory"
        return "dev-run-failed"

    def delete(self, run: str) -> None:
        started = self.started.pop(run, None)
        if started is not None:
            self.cli.run("rm", "-f", started.container, timeout=120)
        self.cli.run("rm", "-f", wait_name(self.host.name, run), timeout=60)


# ------------------------------------------------------------------ native


class NativePort:
    """The client's port on a native build host: the wrapper as a process of
    this machine (no container), its cache and queue under ``state_dir``."""

    def __init__(
        self,
        host: BuildHost,
        dev: DevBuilds,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        poll: float = 0.5,
        python: str = sys.executable,
    ) -> None:
        self.host = host
        self.dev = dev
        self.root = Path(
            host.state_dir or Path.home() / ".cache" / "piceli" / "dev" / host.name
        ).expanduser()
        self.queue = FileQueue(self.root / "queue")
        self.clock = clock
        self.sleep = sleep
        self.poll = poll
        self.python = python
        self.jobs: dict[str, dict[str, Any]] = {}
        self.slots: dict[str, int] = {}
        self.processes: dict[str, subprocess.Popen[bytes]] = {}
        self.cancelled: set[str] = set()

    def _work(self, run: str) -> Path:
        return self.root / "work" / run

    def create(self, job: dict[str, Any]) -> None:
        self.jobs[job["metadata"]["labels"][RUN_LABEL]] = job

    def _environment(self, run: str, role: str | None = None) -> dict[str, str]:
        from piceli.dev.jobs import wrapper_source  # noqa: F401 (same source)

        spec = json.loads(_env(self.jobs[run])["PICELI_DEV_SPEC"])
        spec["isolation_check"] = False
        # This machine's own rustup honours rust-toolchain.toml itself, and
        # its own ~/.cargo already holds the crates.
        spec["provision_toolchains"] = False
        spec["own_cargo_home"] = True
        env = {
            **os.environ,
            "PICELI_DEV_SPEC": json.dumps(spec, sort_keys=True),
            "PICELI_DEV_WORK": str(self._work(run)),
            "PICELI_DEV_CACHE": str(self.root / "cache"),
        }
        if role:
            env["PICELI_DEV_ROLE"] = role
        return env

    def wait_started(
        self, run: str, timeout: float, on_wait: Callable[[str], None]
    ) -> str:
        from piceli.dev.jobs import wrapper_source

        labels = self.jobs[run]["metadata"]["labels"]
        entry = Entry(
            run,
            labels.get(REQUESTER_LABEL, "anonymous"),
            labels.get(PRIORITY_LABEL, "agent"),
            _iso_ms(time.time()),
        )
        self.queue.register(entry)
        try:
            slot = acquire(
                self.queue,
                entry,
                self.host.slots,
                lambda n: self.queue.claim(n, entry),
                timeout=timeout,
                on_wait=on_wait,
                clock=self.clock,
                sleep=self.sleep,
                poll=self.poll,
                host=self.host.name,
            )
        finally:
            self.queue.unregister(run)
        self.slots[run] = slot
        work = self._work(run)
        work.mkdir(parents=True, exist_ok=True)
        (self.root / "cache").mkdir(parents=True, exist_ok=True)
        source = wrapper_source()
        spec = json.loads(_env(self.jobs[run])["PICELI_DEV_SPEC"])
        if spec.get("sources"):
            subprocess.run(
                [self.python, "-I", "-c", source],
                env=self._environment(run, "fetch"),
                capture_output=True,
                timeout=600,
                check=False,
            )
        self.processes[run] = subprocess.Popen(
            [self.python, "-I", "-c", source],
            env=self._environment(run),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        return run

    def have(self, pod: str) -> dict[str, Any] | None:
        try:
            value = json.loads((self._work(pod) / "have.json").read_text())
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def upload(
        self,
        pod: str,
        archive: Path,
        sha256: str | None = None,
        progress: Callable[[int, int, float], None] = lambda _s, _t, _e: None,
    ) -> dict[str, Any]:
        began = time.monotonic()
        upload = self._work(pod) / "upload"
        upload.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(archive, upload / ".tree.tar.gz")
        os.replace(upload / ".tree.tar.gz", upload / "tree.tar.gz")
        if sha256:
            (upload / "tree.sha256").write_text(sha256)
        (upload / "ready").touch()
        size = archive.stat().st_size
        progress(size, size, time.monotonic() - began)
        return {
            "bytes": size,
            "chunks": 1,
            "retries": 0,
            "seconds": round(time.monotonic() - began, 3),
        }

    def download(self, pod: str, dest: Path) -> None:
        out = self._work(pod) / "out"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(out / "artifacts.tar.gz", dest)
        (out / "collected").touch()

    def follow(self, pod: str, on_line: Callable[[str], None]) -> None:
        process = self.processes.get(pod)
        if process is None or process.stdout is None:
            return
        for raw in process.stdout:
            on_line(raw.decode("utf-8", "replace").rstrip("\n"))
        process.wait()

    def ended(self, run: str) -> str:
        if run in self.cancelled or run not in self.processes:
            return "dev-run-cancelled"
        return "dev-run-failed"

    def delete(self, run: str) -> None:
        process = self.processes.pop(run, None)
        if process is not None and process.poll() is None:
            self.cancelled.add(run)
            try:
                os.killpg(process.pid, 15)
                process.wait(timeout=10)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, 9)
                except ProcessLookupError:
                    pass
        slot = self.slots.pop(run, None)
        if slot is not None:
            self.queue.release(slot)
        shutil.rmtree(self._work(run), ignore_errors=True)


# ------------------------------------------------------------------ choosing


@dataclass
class HostState:
    """A host's health and load, as :func:`candidates` sees it."""

    host: BuildHost
    up: bool
    detail: str = ""
    platforms: tuple[str, ...] = ()
    running: int = 0
    waiting: int = 0
    notes: list[str] = field(default_factory=list)
    #: 0.22.0: :func:`piceli.dev.health.view` (``None``: not tracked here).
    health: dict[str, Any] | None = None

    @property
    def quarantined(self) -> str | None:
        """Why the host is quarantined, or ``None``."""
        if self.health and self.health.get("quarantined"):
            return str(self.health.get("reason") or "quarantined")
        return None

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.host.name,
            "kind": self.host.kind,
            "up": self.up,
            "detail": self.detail,
            "platforms": list(self.platforms),
            "slots": self.host.slots,
            "running": self.running,
            "waiting": self.waiting,
            "quarantined": self.quarantined is not None,
            "health": self.health,
            # 0.25.0: False: the host only publishes Nix releases.
            "runs": self.host.runs,
        }


def native_platform() -> str:
    machine = os.uname().machine
    system = sys.platform
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64"}.get(
        machine, machine
    )
    return f"{'darwin' if system == 'darwin' else 'linux'}/{arch}"


def _health(host: BuildHost, cli: Any = None) -> dict[str, Any] | None:
    """``host``'s health view (0.22.0); ``None`` when unreadable or untracked."""
    from piceli.dev.health import Health

    try:
        found = Health.of(host, cli)
        return None if found is None else found.view()
    except (DevError, OSError):
        return None


def probe(host: BuildHost, cluster_up: Callable[[], bool] | None = None) -> HostState:
    """Whether ``host`` can take a run now, its queue and its health."""
    if host.kind == "native":
        queue = FileQueue(
            Path(host.state_dir or Path.home() / ".cache/piceli/dev" / host.name)
            .expanduser() / "queue"
        )  # fmt: skip
        return HostState(
            host, True, platforms=tuple(host.platforms) or (native_platform(),),
            running=len(queue.running()), waiting=len(queue.waiting()),
            health=_health(host),
        )  # fmt: skip
    if host.kind == "cluster":
        up = cluster_up() if cluster_up is not None else True
        return HostState(
            host, up, "" if up else "the cluster or its builder node is not reachable",
            platforms=tuple(host.platforms),
        )  # fmt: skip
    cli = ContainerCli(host)
    try:
        own = cli.platform()
    except DevError as error:
        return HostState(host, False, str(error))
    queue = DockerQueue(cli)
    return HostState(
        host, True, platforms=tuple(host.platforms) or (own,),
        running=len(queue.running()), waiting=len(queue.waiting()),
        health=_health(host, cli),
    )  # fmt: skip


def candidates(
    dev: DevBuilds,
    *,
    requested: str | None = None,
    platform: str | None = None,
    probe: Callable[[BuildHost], HostState] = probe,
) -> list[HostState]:
    """The hosts to try for a run, best first: the requested one only, else
    every host that is up, not quarantined (0.22.0), builds ``platform`` and
    takes runs (0.25.0: not ``runs=False``), one with a free slot first,
    then by declaration order.

    :raises DevError: ``dev-host-unknown`` (no such host), ``dev-host-unavailable``
        (none is up for the platform).
    """
    # 0.25.0: a host that only publishes is never a default choice.
    hosts = [item for item in dev.hosts if item.runs]
    if requested is not None:
        hosts = [item for item in dev.hosts if item.name == requested]
        if not hosts:
            raise DevError(
                "dev-host-unknown",
                f"no build host {requested!r}; declared: "
                + ", ".join(item.name for item in dev.hosts),
            )
    if not hosts:
        raise DevError(
            UNAVAILABLE,
            "every declared build host only publishes Nix releases (runs=False: "
            + ", ".join(item.name for item in dev.hosts)
            + "); name one with --host, or declare a host that takes runs",
        )
    states = [probe(item) for item in hosts]
    usable = [
        state
        for state in states
        if state.up
        and state.quarantined is None
        and (
            platform is None
            or platform in state.platforms
            or state.host.kind == "docker"  # emulated, slower
        )
    ]
    if not usable:
        detail = "; ".join(
            f"{s.host.name}: "
            + (
                f"quarantined: {s.quarantined} (piceli dev release-host "
                f"{s.host.name} clears it)"
                if s.up and s.quarantined is not None
                else s.detail or "not " + str(platform)
            )
            for s in states
        )
        raise DevError(UNAVAILABLE, f"no build host can take the run ({detail})")
    order = {state.host.name: index for index, state in enumerate(states)}
    usable.sort(
        key=lambda s: (
            0 if s.running < s.host.slots and not s.waiting else 1,
            order[s.host.name],
        )
    )
    return usable


def clear_host_cache(
    host: BuildHost,
    dev: DevBuilds,
    *,
    lineage: str | None = None,
    crates: Sequence[str] = (),
    cache_volume: str = CACHE_VOLUME,
) -> dict[str, Any]:
    """``piceli dev cache clear --host``: empty a docker or native host's free
    lineages (or only ``crates``); a lineage a run holds is skipped (busy).

    :raises DevError: ``dev-host-unavailable``, ``dev-cache-clear-failed``.
    """
    from piceli.dev.pod import RESULT_MARKER, clear_cache

    if host.kind == "native":
        root = Path(
            host.state_dir or Path.home() / ".cache" / "piceli" / "dev" / host.name
        ).expanduser()
        return clear_cache(root / "cache", lineage, list(crates))
    from piceli.dev.jobs import wrapper_source

    cli = ContainerCli(host)
    image = host.image or dev.image
    spec = json.dumps({"lineage": lineage, "crates": list(crates)})
    done = cli.run(
        "run", "--rm", "--user", "65532:65532", "--network", "none",
        "-v", f"{cache_volume}:/cache",
        "-e", f"PICELI_DEV_SPEC={spec}", "-e", "PICELI_DEV_ROLE=clear",
        "--entrypoint", "python3", image, "-I", "-c", wrapper_source(),
        timeout=1800,
    )  # fmt: skip
    for line in reversed(done.stdout.decode("utf-8", "replace").splitlines()):
        if line.startswith(RESULT_MARKER):
            value = json.loads(line[len(RESULT_MARKER) :])
            if isinstance(value, dict):
                return value
    tail = done.stderr.decode("utf-8", "replace").strip()[-300:]
    raise DevError(
        "dev-cache-clear-failed", f"the cache clear on {host.name} failed: {tail}"
    )
