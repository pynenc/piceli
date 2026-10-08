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

It runs as a thread of the GitOps controller (``piceli gitops run``), or as
``piceli dev schedule``. Importing this module is side-effect free.
"""

from __future__ import annotations

import collections
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from piceli.dev.jobs import PRIORITY_LABEL, PROFILE_LABEL, REQUESTER_LABEL, RUN_LABEL
from piceli.dev.model import DevBuilds, bytes_of, cpu_of

STATUS_SCHEMA = "piceli.dev-status.v1"
PRIORITY_RANK = {"round": 0, "agent": 1, "normal": 2}
MAX_RECENT = 50
#: Log lines a finished run that did not pass keeps in the status.
RECORDED_TAIL = 20
#: A scheduler silent for longer is not alive: clients start runs at once.
ALIVE_SECONDS = 60
TICK_SECONDS = 3.0


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
    ) -> None:
        self.port = port
        self.dev = dev
        self.clock = clock
        self.on_finished = on_finished
        self.say = say
        self.recent: collections.deque[dict[str, Any]] = collections.deque(
            maxlen=MAX_RECENT
        )
        self.seen: dict[str, dict[str, Any]] = {}
        self.cache: dict[str, Any] = {}
        self._slots: tuple[float, int] | None = None

    def record(self, entry: dict[str, Any]) -> None:
        self.recent.appendleft(entry)
        used = (entry.get("cache") or {}).get("used_bytes")
        if isinstance(used, int):
            self.cache = {
                "used_bytes": used,
                "max_bytes": (entry.get("cache") or {}).get("max_bytes"),
                "at": entry.get("finished_at"),
            }

    def slots(self) -> int:
        now = self.clock()
        if self._slots is None or now - self._slots[0] > 60:
            node = (
                None
                if isinstance(self.dev.slots, int)
                else self.port.node(self.dev.node)
            )
            self._slots = (now, auto_slots(self.dev, node))
        return self._slots[1]

    def _finish(self, job: Mapping[str, Any], state: str, now: float) -> None:
        run = run_of(job)
        entry = {**self.seen.get(run, {}), **run_summary(job, now)}
        result = self.port.result(run) if state != "cancelled" else None
        if result is not None:
            entry.update(
                {
                    key: result.get(key)
                    for key in ("state", "exit_code", "reason", "durations", "tests")
                }
            )
            cache = dict(result.get("cache") or {})
            cache.pop("lineage_dir", None)
            entry["cache"] = cache
            if result.get("state") != "passed":
                entry["log_tail"] = list(result.get("log_tail") or ())[-RECORDED_TAIL:]
        elif state == "cancelled":
            entry.update(state="cancelled", reason="dev-run-cancelled", exit_code=None)
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

    def tick(self) -> dict[str, Any]:
        """Record, start and publish once; the published status."""
        now = self.clock()
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
            elif state == "queued":
                queued.append(job)
            else:
                running.append(job)
        slots = self.slots()
        waiting = order(queued, running)
        for job in waiting[: max(0, slots - len(running))]:
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
                {**run_summary(job, now), "position": index + 1}
                for index, job in enumerate(waiting)
            ],
            "recent": list(self.recent),
            "cache": dict(self.cache),
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


def start_thread(
    make_api: Callable[[], Any],
    *,
    on_finished: Callable[[dict[str, Any]], None] = lambda _record: None,
    log: Callable[[str], None] = lambda _line: None,
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
        return Scheduler(port, dev, on_finished=on_finished, say=log)

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
