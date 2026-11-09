"""The queue of development runs (0.18.0): slots, priorities, fair share.

With a scheduler alive, ``piceli dev run`` creates its Job suspended. Every
tick the scheduler:

1. records each finished run (its wrapper's result from the pod log; a Job
   that ended without one is ``dev-run-failed``, one that disappeared before
   it ended ``dev-run-cancelled``) and deletes its Job, which removes the
   run's scratch;
2. un-suspends queued runs into the free slots: ``round`` before ``agent``
   before ``normal``; within a priority the requester with the fewest runs
   going first (fair share); then the oldest;
3. publishes ``piceli-dev-status`` (``piceli.dev-status.v1``): slots,
   running and queued runs (with their position), the last 50 finished runs
   and the cache use, plus ``scheduler_at`` (clients suspend their Jobs only
   while it is recent).

0.38.1: while the last reported cache use is over its budget or its disk
under the floor, the queue starts one run at a time and only when none is
running (its start-time eviction trims the cache, which only free lineages
allow); while the builder node is under disk pressure it starts none. The
status says so in ``held``. The scheduler never creates a Job.

0.54.0: each queued run says why it waits (``waiting_for``); a queued run
whose client stopped beating (``piceli.io/dev-client-at``, every 30 s) for
5 minutes, or that waited past its queue timeout, is removed
(``dev-run-abandoned``); a restarted scheduler starts from the last
published cache use and recent runs.

0.53.0: the status's ``cache`` carries ``lineages``, each lineage as the
last finished run's end-of-run trim saw it (``at`` is its time).

It runs as a thread of the GitOps controller (``piceli gitops run``), or as
``piceli dev schedule``. Importing this module is side-effect free.
"""

from __future__ import annotations

import collections
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from piceli.dev.jobs import (
    CLIENT_AT_ANNOTATION,
    PRIORITY_LABEL,
    PROFILE_LABEL,
    QUEUE_TIMEOUT_ANNOTATION,
    REQUESTER_LABEL,
    RUN_LABEL,
)
from piceli.dev.model import DevBuilds, bytes_of, cpu_of

STATUS_SCHEMA = "piceli.dev-status.v1"
PRIORITY_RANK = {"round": 0, "agent": 1, "normal": 2}
MAX_RECENT = 50
#: 0.68.0: failed runs stay in the status this long after they left
#: ``recent`` (at most :data:`MAX_FAILED`, so the ConfigMap stays small).
FAILED_SECONDS = 7 * 24 * 3600
MAX_FAILED = 60
#: Log lines a finished run that did not pass keeps in the status.
RECORDED_TAIL = 30
#: A scheduler silent for longer is not alive: clients start runs at once.
ALIVE_SECONDS = 60
TICK_SECONDS = 3.0
#: 0.54.0: a queued run whose client has not said it waits for this long
#: is abandoned (its client beats every 30 s).
ABANDONED_SECONDS = 300.0
#: 0.54.0: a queued run from an older client (no beat) is abandoned once it
#: waited longer than its queue timeout, or this when it has none.
LEGACY_QUEUE_SECONDS = 7200.0


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seconds(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return (
            datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=UTC)
            .timestamp()
        )
    except ValueError:
        return None


def _labels(job: Mapping[str, Any]) -> dict[str, str]:
    return dict((job.get("metadata") or {}).get("labels") or {})


def run_of(job: Mapping[str, Any]) -> str:
    return _labels(job).get(RUN_LABEL, "")


def state_of(job: Mapping[str, Any]) -> str:
    """``queued``, ``running``, ``complete`` or ``failed``."""
    for condition in (job.get("status") or {}).get("conditions") or ():
        if condition.get("status") == "True" and condition.get("type") in {
            "Complete",
            "Failed",
        }:
            return "complete" if condition["type"] == "Complete" else "failed"
    return "queued" if (job.get("spec") or {}).get("suspend") else "running"


def _created(job: Mapping[str, Any]) -> str:
    return str((job.get("metadata") or {}).get("creationTimestamp") or "")


def order(
    queued: Iterable[Mapping[str, Any]], running: Iterable[Mapping[str, Any]]
) -> list[Mapping[str, Any]]:
    """Queued runs in the order they start (see the module doc)."""
    counts = collections.Counter(_labels(job).get(REQUESTER_LABEL) for job in running)
    left = list(queued)
    picked: list[Mapping[str, Any]] = []
    while left:
        best = min(
            left,
            key=lambda job: (
                PRIORITY_RANK.get(_labels(job).get(PRIORITY_LABEL, "agent"), 1),
                counts[_labels(job).get(REQUESTER_LABEL)],
                _created(job),
                run_of(job),
            ),
        )
        left.remove(best)
        picked.append(best)
        counts[_labels(best).get(REQUESTER_LABEL)] += 1
    return picked


def auto_slots(dev: DevBuilds, node: Mapping[str, Any] | None) -> int:
    """``DevBuilds(slots=)``, or what the node's allocatable CPU and memory hold."""
    if isinstance(dev.slots, int):
        return dev.slots
    allocatable = ((node or {}).get("status") or {}).get("allocatable") or {}
    cpu = cpu_of(str(allocatable.get("cpu") or "0")) // max(cpu_of(dev.run_cpu), 0.001)
    memory = bytes_of(str(allocatable.get("memory") or "0")) // max(
        bytes_of(dev.run_memory), 1
    )
    return max(1, int(min(cpu, memory)))


def run_summary(job: Mapping[str, Any], now: float) -> dict[str, Any]:
    labels = _labels(job)
    status = job.get("status") or {}
    body = {
        "run": labels.get(RUN_LABEL),
        "requester": labels.get(REQUESTER_LABEL),
        "priority": labels.get(PRIORITY_LABEL),
        "profile": labels.get(PROFILE_LABEL),
        "created_at": _created(job) or None,
        "started_at": status.get("startTime"),
    }
    created, started = _seconds(body["created_at"]), _seconds(body["started_at"])
    if created is not None:
        body["waited_seconds"] = round((started or now) - created, 1)
    return body


def waiting_for(held: Mapping[str, Any] | None, slots: int, running: int) -> str:
    """0.54.0: why a queued run has not started, in one line."""
    if held is not None:
        return str(held.get("message") or held.get("cause"))
    if running >= slots:
        return f"all {slots} slot(s) busy"
    return "its turn (starting)"


def abandoned(job: Mapping[str, Any], now: float) -> bool:
    """0.54.0: whether the client waiting for this queued run is gone."""
    notes = (job.get("metadata") or {}).get("annotations") or {}
    beat = _seconds(notes.get(CLIENT_AT_ANNOTATION))
    created = _seconds(_created(job))
    try:
        patience = float(notes.get(QUEUE_TIMEOUT_ANNOTATION) or LEGACY_QUEUE_SECONDS)
    except ValueError:
        patience = LEGACY_QUEUE_SECONDS
    if created is not None and now - created > patience + ABANDONED_SECONDS:
        return True  # its client gave up waiting long ago
    return beat is not None and now - beat > ABANDONED_SECONDS


def disk_pressure(node: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """0.35.0: ``{node, since, message}`` when ``node``'s ``DiskPressure``
    condition holds, else ``None``."""
    for condition in ((node or {}).get("status") or {}).get("conditions") or ():
        if (
            condition.get("type") == "DiskPressure"
            and condition.get("status") == "True"
        ):
            return {
                "node": str(((node or {}).get("metadata") or {}).get("name") or ""),
                "since": condition.get("lastTransitionTime"),
                "message": str(condition.get("message") or "")[:200] or None,
            }
    return None


class Scheduler:
    """One development-run queue over a port (:class:`piceli.dev.cluster.DevCluster`)."""

    def __init__(
        self,
        port: Any,
        dev: DevBuilds,
        *,
        clock: Callable[[], float] = time.time,
        on_finished: Callable[[dict[str, Any]], None] = lambda _record: None,
        say: Callable[[str], None] = lambda _line: None,
        git_credentials: Callable[[], tuple[str, str, list[str]] | None] | None = None,
    ) -> None:
        self.git_credentials = git_credentials
        self._git_checked = 0.0
        self.port = port
        self.dev = dev
        self.clock = clock
        self.on_finished = on_finished
        self.say = say
        self.recent: collections.deque[dict[str, Any]] = collections.deque(
            maxlen=MAX_RECENT
        )
        self.seen: dict[str, dict[str, Any]] = {}
        #: 0.68.0: failed runs older than ``recent`` holds, newest first.
        self.failed: list[dict[str, Any]] = []
        self.cache: dict[str, Any] = {}
        self._slots: tuple[float, int] | None = None
        #: 0.35.0: the node's DiskPressure condition while it holds.
        self.node_pressure: dict[str, Any] | None = None
        self._held: dict[str, Any] | None = None
        self._restored = False

    def record(self, entry: dict[str, Any]) -> None:
        cache = dict(entry.get("cache") or {})
        # 0.38.1: what the run left after its end-of-run trim, when it has it.
        trim = cache.get("trim")
        lineages = None
        if isinstance(trim, Mapping):
            # 0.53.0: the cache's lineages go to the status once, not into
            # every recent run.
            lineages = trim.get("lineages")
            if "lineages" in trim:
                entry = {
                    **entry,
                    "cache": {
                        **cache,
                        "trim": {k: v for k, v in trim.items() if k != "lineages"},
                    },
                }
        if len(self.recent) == MAX_RECENT:
            self._keep_failed(self.recent[-1])
        self.recent.appendleft(entry)
        if isinstance(trim, Mapping) and isinstance(trim.get("used_bytes"), int):
            cache.update(trim)
        used = cache.get("used_bytes")
        if isinstance(used, int):
            self.cache = {
                "used_bytes": used,
                "max_bytes": cache.get("max_bytes"),
                "at": entry.get("finished_at"),
                # 0.35.0: still over budget after eviction, or its disk low.
                **{
                    key: cache[key]
                    for key in (
                        "over_budget",
                        "fs_low",
                        "fs_free_bytes",
                        "fs_total_bytes",
                    )
                    if key in cache
                },
                # 0.53.0: each lineage at the end of that run (piceli dev status).
                **({"lineages": list(lineages)} if isinstance(lineages, list) else {}),
            }

    def held(self) -> dict[str, Any] | None:
        """0.38.1: why new runs are held back (``cause``, ``message``), or
        ``None``: the node under disk pressure, else the last reported cache
        use over its budget or its disk under the floor."""
        if self.node_pressure:
            return {
                "cause": "node-disk-pressure",
                "message": f"node {self.node_pressure.get('node')} is under disk "
                "pressure; piceli dev cache clear frees the cache",
            }
        if self.cache.get("over_budget") or self.cache.get("fs_low"):
            gib = 2**30
            used = int(self.cache.get("used_bytes") or 0)
            limit = int(self.cache.get("max_bytes") or 0)
            return {
                "cause": "cache-over-budget",
                "message": f"the cache holds {used / gib:.1f} GiB of a "
                f"{limit / gib:.1f} GiB budget"
                + (", its disk is nearly full" if self.cache.get("fs_low") else "")
                + "; one run at a time until a run's eviction trims it",
            }
        return None

    def slots(self) -> int:
        now = self.clock()
        if self._slots is None or now - self._slots[0] > 60:
            try:
                node = self.port.node(self.dev.node) if self.dev.node else None
            except Exception:  # a read, never a reason to stop the queue
                node = None
            self.node_pressure = disk_pressure(node)
            self._slots = (now, auto_slots(self.dev, node))
        return self._slots[1]

    def restore(self) -> None:
        """0.54.0: start from the last published status (the cache use and
        recent runs), so a restarted scheduler does not show an empty cache."""
        self._restored = True
        try:
            status = self.port.status() or {}
        except Exception:  # a read, never a reason to stop the queue
            return
        if not self.cache and isinstance(status.get("cache"), dict):
            self.cache = dict(status["cache"])
        if not self.recent:
            for entry in reversed(list(status.get("recent") or ())[:MAX_RECENT]):
                if isinstance(entry, dict):
                    self.recent.appendleft(entry)
        if not self.failed:
            self.failed = [
                dict(item)
                for item in list(status.get("failed") or ())[:MAX_FAILED]
                if isinstance(item, dict)
            ]

    def _published_failed(self) -> list[dict[str, Any]]:
        self._trim_failed()
        return list(self.failed)

    def _keep_failed(self, entry: Mapping[str, Any]) -> None:
        """0.68.0: a run leaving ``recent`` stays in ``failed`` when it did
        not pass (cancelled runs aside), for :data:`FAILED_SECONDS`."""
        if entry.get("state") in ("passed", "cancelled"):
            return
        self.failed.insert(0, dict(entry))
        self._trim_failed()

    def _trim_failed(self) -> None:
        cutoff = self.clock() - FAILED_SECONDS
        self.failed = [
            item
            for item in self.failed
            if (_seconds(item.get("finished_at")) or 0) >= cutoff
        ][:MAX_FAILED]

    def _finish(
        self,
        job: Mapping[str, Any],
        state: str,
        now: float,
        code: str = "dev-run-cancelled",
    ) -> None:
        run = run_of(job)
        entry = {**self.seen.get(run, {}), **run_summary(job, now)}
        result = self.port.result(run) if state != "cancelled" else None
        if result is not None:
            entry.update(
                {
                    key: result.get(key)
                    for key in (
                        "state",
                        "exit_code",
                        "reason",
                        "durations",
                        "tests",
                        "timeline",
                    )
                }
            )
            cache = dict(result.get("cache") or {})
            cache.pop("lineage_dir", None)
            entry["cache"] = cache
            if result.get("state") != "passed":
                entry["log_tail"] = list(result.get("log_tail") or ())[-RECORDED_TAIL:]
                # 0.19: the cause survives a tail that only shows the end.
                if result.get("first_error"):
                    entry["first_error"] = list(result["first_error"])[:RECORDED_TAIL]
                # 0.67.0: what ended it, so a later look needs no log.
                for key in ("signal", "cause", "health"):
                    if result.get(key):
                        entry[key] = result[key]
        elif state == "cancelled":
            entry.update(state="cancelled", reason=code, exit_code=None)
        else:
            entry.update(state="error", reason="dev-run-failed", exit_code=None)
        entry["finished_at"] = _iso(now)
        self.record(entry)
        self.seen.pop(run, None)
        try:
            self.on_finished(entry)
        except Exception:  # telemetry never stops the queue
            pass
        self.say(f"dev run {run}: {entry.get('state')}")

    def sync_git_secret(self, now: float) -> None:
        """Keep ``piceli-dev-git`` equal to the controller's own Git
        credentials (a rotated token reaches runs within a minute)."""
        if self.git_credentials is None or now - self._git_checked < 60:
            return
        self._git_checked = now
        found = self.git_credentials()
        if found is not None and self.port.put_git_secret(*found):
            self.say("dev scheduler: runs' Git credentials updated")

    def tick(self) -> dict[str, Any]:
        """Record, start and publish once; the published status."""
        now = self.clock()
        if not self._restored:
            self.restore()
        try:
            self.sync_git_secret(now)
        except Exception as error:  # never stops the queue
            self.say(
                f"dev scheduler: Git credentials not synced ({type(error).__name__})"
            )
        jobs = self.port.jobs()
        current = {run_of(job) for job in jobs}
        for run in [r for r in self.seen if r not in current]:
            self._finish({"metadata": {"labels": {RUN_LABEL: run}}}, "cancelled", now)
        queued, running = [], []
        for job in jobs:
            state = state_of(job)
            if state in {"complete", "failed"}:
                self._finish(job, state, now)
                try:
                    self.port.delete(run_of(job))
                except Exception:  # the Job's TTL removes it
                    pass
            elif state == "queued" and abandoned(job, now):
                # 0.54.0: its client left; it never holds a place again.
                self._finish(job, "cancelled", now, code="dev-run-abandoned")
                try:
                    self.port.delete(run_of(job))
                except Exception:  # tried again next tick
                    pass
            elif state == "queued":
                queued.append(job)
            else:
                running.append(job)
        slots = self.slots()
        waiting = order(queued, running)
        held = self.held()
        free = max(0, slots - len(running))
        if held is not None:
            # 0.38.1: one run at a time, none under disk pressure.
            free = 0 if held["cause"] == "node-disk-pressure" or running else 1
            if waiting and held != self._held:
                self.say(f"dev scheduler: runs held: {held['message']}")
        self._held = held
        for job in waiting[:free]:
            self.port.unsuspend(run_of(job))
            job["spec"]["suspend"] = False
            running.append(job)
            self.say(f"dev run {run_of(job)}: started")
        waiting = [job for job in waiting if (job.get("spec") or {}).get("suspend")]
        for job in running:
            self.seen[run_of(job)] = run_summary(job, now)
        for job in waiting:
            self.seen[run_of(job)] = run_summary(job, now)
        document = {
            "schema": STATUS_SCHEMA,
            "scheduler_at": _iso(now),
            "node": self.dev.node,
            "slots": slots,
            "running": [run_summary(job, now) for job in running],
            "queued": [
                {
                    **run_summary(job, now),
                    "position": index + 1,
                    "waiting_for": waiting_for(held, slots, len(running)),
                }
                for index, job in enumerate(waiting)
            ],
            "recent": list(self.recent),
            **({"failed": failed} if (failed := self._published_failed()) else {}),
            "cache": dict(self.cache),
            # 0.35.0: the node is under disk pressure (runs cannot start).
            **(
                {"node_disk_pressure": self.node_pressure} if self.node_pressure else {}
            ),
            **({"held": held} if held else {}),
        }
        self.port.publish(document)
        return document


def alive(document: Mapping[str, Any] | None, now: float) -> bool:
    """Whether a scheduler published ``document`` recently."""
    at = _seconds((document or {}).get("scheduler_at"))
    return at is not None and now - at < ALIVE_SECONDS


def loop(
    scheduler_for: Callable[[], Scheduler | None],
    *,
    stop: Callable[[], bool],
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = lambda _line: None,
) -> None:
    """Tick every few seconds while ``scheduler_for()`` gives a scheduler
    (``None``: development builds are not installed; checked every minute)."""
    scheduler: Scheduler | None = None
    checked = 0.0
    while not stop():
        if scheduler is None and time.monotonic() - checked >= 60:
            checked = time.monotonic()
            try:
                scheduler = scheduler_for()
            except Exception as error:
                log(f"dev scheduler: not started ({type(error).__name__})")
        if scheduler is not None:
            try:
                scheduler.tick()
            except Exception as error:  # rebuilt (a rotated token) and retried
                log(f"dev scheduler: tick failed ({type(error).__name__})")
                scheduler, checked = None, 0.0
        sleep(TICK_SECONDS)


def git_credentials_of(
    directory: Path | None, urls: Iterable[str]
) -> Callable[[], tuple[str, str, list[str]] | None]:
    """The controller's mounted Git Secret (``username``, ``password``) and
    the canonical URLs runs may fetch (the composition's sources)."""
    from piceli.dev.delta import canonical_url

    allowed = sorted({u for u in (canonical_url(url) for url in urls) if u})

    def read() -> tuple[str, str, list[str]] | None:
        if directory is None or not (directory / "password").is_file():
            return None
        username = (
            (directory / "username").read_text().strip()
            if (directory / "username").is_file()
            else "x-access-token"
        )
        return username, (directory / "password").read_text().strip(), allowed

    return read


def start_thread(
    make_api: Callable[[], Any],
    *,
    on_finished: Callable[[dict[str, Any]], None] = lambda _record: None,
    log: Callable[[str], None] = lambda _line: None,
    git_credentials: Callable[[], tuple[str, str, list[str]] | None] | None = None,
) -> Callable[[], None]:
    """Run the queue on a daemon thread (the GitOps controller's); returns its stop.

    The thread idles while development builds are not installed
    (``piceli-dev-config`` absent) and starts queueing once they are.
    """
    import threading

    from piceli.dev.cluster import DevCluster

    stopping = threading.Event()

    def scheduler_for() -> Scheduler | None:
        port = DevCluster(make_api())
        dev = port.config()
        if dev is None:
            return None
        log(f"dev scheduler: queueing development runs on {dev.node}")
        return Scheduler(
            port, dev, on_finished=on_finished, say=log, git_credentials=git_credentials
        )

    thread = threading.Thread(
        target=loop,
        args=(scheduler_for,),
        kwargs={"stop": stopping.is_set, "sleep": stopping.wait, "log": log},
        name="piceli-dev-scheduler",
        daemon=True,
    )
    thread.start()

    def stop() -> None:
        stopping.set()
        thread.join(10)

    return stop
