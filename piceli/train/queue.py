"""A train's queue: requests waiting for the train, served one at a time.

``piceli train APP --branches … --queue`` on a busy train writes a request
and returns at once with its id and position. Whoever runs the train serves
the queue when its own run ends: any ``piceli train`` process, after its
run, takes the oldest waiting request, runs it with the flags it was given
(``--promote``, ``--push``, ``--promote-run``), and so on until the queue
is empty. ``piceli
train-queue NAME --drain`` does the same without a run of its own.

Layout under the train's state directory::

    queue/waiting/<id>.json   requests waiting, oldest id first
    queue/running/<id>.json   the request being served (names its process)
    queue/done/<id>.json      served (done, rejected) or cancelled

A request moves between the directories by ``rename``, so exactly one
process claims it. Races are closed by order: a request is written before
its writer checks whether the train is busy, and a server releases the
train's lock before it looks for the next request. Either the server sees
the request, or the writer sees a free train and serves it itself.

If the serving process dies, the lock it held has no live owner and its
request stays in ``running`` with a dead pid (``stale``). The next server
(``piceli train …`` or ``piceli train-queue NAME --drain``) puts it back at
the head of the queue (``resumed`` counts how often) and runs it again; the
killed run's record shows ``interrupted``.

Importing this module is side-effect free.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from piceli.train.model import TrainError
from piceli.train.state import TrainState

if TYPE_CHECKING:
    from piceli.train.engine import TrainRun

REQUEST_SCHEMA = "piceli.train.request.v1"
QUEUED_SCHEMA = "piceli.train.queued.v1"
QUEUE_SCHEMA = "piceli.train.queue.v1"
_STATES = ("waiting", "running", "done")


def new_queue_id(at: float | None = None) -> str:
    moment = datetime.fromtimestamp(time.time() if at is None else at, UTC)
    return f"q-{moment.strftime('%Y%m%dT%H%M%S%fZ')}-{secrets.token_hex(2)}"


def is_queue_id(value: str) -> bool:
    return value.startswith("q-")


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _alive(pid: Any) -> bool:
    if not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class TrainQueue:
    """The queue of one train (see the module)."""

    def __init__(self, state: TrainState) -> None:
        self.state = state
        self.root = state.root / "queue"

    def _dir(self, which: str) -> Path:
        return self.root / which

    def _path(self, which: str, request_id: str) -> Path:
        return self._dir(which) / f"{request_id}.json"

    def _write(self, path: Path, body: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        partial = path.with_suffix(".partial")
        partial.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n")
        os.replace(partial, path)

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        try:
            body = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        return body if isinstance(body, dict) else None

    def _list(self, which: str) -> list[dict[str, Any]]:
        directory = self._dir(which)
        if not directory.is_dir():
            return []
        found = []
        for path in sorted(directory.glob("*.json")):
            body = self._read(path)
            if body is not None:
                found.append(body)
        return found

    # ------------------------------------------------------------ writing

    def enqueue(
        self,
        *,
        entry: str,
        cwd: str,
        branches: Sequence[str],
        promote: bool,
        push: bool,
        promote_run: str | None = None,
        retire: str | None = None,
    ) -> dict[str, Any]:
        """Add a request at the tail; returns it with its ``position``.

        ``promote_run``: promote that earlier run's head when served
        (``--promote-run``, the run id, not ``last``) instead of running
        ``branches``."""
        request: dict[str, Any] = {
            "schema": REQUEST_SCHEMA,
            "id": new_queue_id(),
            "train": self.state.root.name,
            "entry": entry,
            "cwd": cwd,
            "branches": list(branches),
            "promote": promote,
            "push": push,
            **({"promote_run": promote_run} if promote_run else {}),
            **({"retire": retire} if retire else {}),
            "queued_at": _now(),
            "state": "waiting",
            "resumed": 0,
        }
        self._write(self._path("waiting", request["id"]), request)
        return {**request, "position": self.position(request["id"])}

    def claim(self) -> dict[str, Any] | None:
        """Take the oldest waiting request (``running``, naming this process)."""
        for request in self._list("waiting"):
            source = self._path("waiting", request["id"])
            target = self._path("running", request["id"])
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.rename(source, target)
            except FileNotFoundError:
                continue  # another process claimed it
            return self.update(
                request["id"], state="running", pid=os.getpid(), started_at=_now()
            )
        return None

    def update(self, request_id: str, **fields: Any) -> dict[str, Any]:
        """Change fields of a request where it is."""
        for which in _STATES:
            path = self._path(which, request_id)
            body = self._read(path)
            if body is not None:
                body.update(fields)
                self._write(path, body)
                return body
        raise TrainError("train-queue-not-found", f"no queued request {request_id}")

    def finish(self, request_id: str, **fields: Any) -> dict[str, Any]:
        """Move a running request to ``done``."""
        source = self._path("running", request_id)
        body = self._read(source) or {"id": request_id}
        body.update({"state": "done", "finished_at": _now(), **fields})
        body.pop("pid", None)
        self._write(self._path("done", request_id), body)
        source.unlink(missing_ok=True)
        return body

    def release(self, request_id: str) -> None:
        """Put a claimed request back (the train was busy after all)."""
        source = self._path("running", request_id)
        body = self._read(source)
        if body is None:
            return
        body.update({"state": "waiting"})
        for key in ("pid", "started_at", "run"):
            body.pop(key, None)
        self._write(self._path("waiting", request_id), body)
        source.unlink(missing_ok=True)

    def cancel(self, request_id: str) -> dict[str, Any]:
        """Cancel a waiting request."""
        source = self._path("waiting", request_id)
        target = self._path("done", request_id)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.rename(source, target)
        except FileNotFoundError:
            if self.find(request_id) is None:
                raise TrainError(
                    "train-queue-not-found", f"no queued request {request_id}"
                ) from None
            raise TrainError(
                "train-queue-not-waiting",
                f"request {request_id} is no longer waiting",
            ) from None
        body = self._read(target) or {"id": request_id}
        body.update(
            {
                "state": "cancelled",
                "reason": "train-queue-cancelled",
                "finished_at": _now(),
            }
        )
        self._write(target, body)
        return body

    def requeue_stale(self) -> list[str]:
        """Put requests whose serving process died back at the head."""
        moved = []
        for request in self._list("running"):
            if _alive(request.get("pid")):
                continue
            request["resumed"] = int(request.get("resumed") or 0) + 1
            request["state"] = "waiting"
            for key in ("pid", "started_at", "run"):
                request.pop(key, None)
            self._write(self._path("waiting", request["id"]), request)
            self._path("running", request["id"]).unlink(missing_ok=True)
            moved.append(request["id"])
        return moved

    # ------------------------------------------------------------ reading

    def waiting(self) -> list[dict[str, Any]]:
        return self._list("waiting")

    def find(self, request_id: str) -> dict[str, Any] | None:
        for which in _STATES:
            body = self._read(self._path(which, request_id))
            if body is not None:
                return body
        return None

    def position(self, request_id: str) -> int | None:
        ids = [r["id"] for r in self.waiting()]
        return ids.index(request_id) + 1 if request_id in ids else None

    def pending(self) -> list[dict[str, Any]]:
        """Running then waiting requests, as views show them (``stale``: its
        process died; the next server resumes it)."""
        rows: list[dict[str, Any]] = []
        for request in self._list("running"):
            alive = _alive(request.get("pid"))
            rows.append({**request, "state": "running" if alive else "stale"})
        for n, request in enumerate(self.waiting(), start=1):
            rows.append({**request, "position": n})
        return [
            {
                key: row.get(key)
                for key in (
                    "id", "state", "position", "branches", "promote", "push",
                    "promote_run", "retire", "queued_at", "started_at", "run",
                    "resumed",
                )
            }
            for row in rows
        ]  # fmt: skip


def find_request(
    root: Path, request_id: str
) -> tuple[TrainQueue, dict[str, Any]] | None:
    """The queue and request of ``request_id`` among every train under ``root``."""
    from piceli.train.state import train_names

    for name in train_names(root):
        queue = TrainQueue(TrainState(root / name))
        found = queue.find(request_id)
        if found is not None:
            return queue, found
    return None


def drain(
    queue: TrainQueue,
    build: Callable[[Mapping[str, Any]], TrainRun],
    *,
    say: Callable[[str], None] = lambda _m: None,
) -> Iterator[tuple[dict[str, Any], dict[str, Any] | None]]:
    """Serve the queue until it is empty; yields ``(request, result or None)``.

    ``build`` makes the run of a request (raises :class:`TrainError` when it
    is refused: a branch that no longer exists). A run that fails midway
    (``run-failed``) ends its request ``failed``; the queue goes on. When
    the train turns out busy, the request goes back and draining stops: the
    holder serves it when its run ends.
    """
    queue.requeue_stale()
    while True:
        if queue.state.owner() is not None:
            return
        request = queue.claim()
        if request is None:
            return
        what = (
            f"--promote-run {request['promote_run']}"
            if request.get("promote_run")
            else " ".join(request["branches"])
        )
        say(f"queue: {request['id']}: {what}")
        try:
            run = build(request)
            run.prepare()
        except TrainError as error:
            done = queue.finish(
                request["id"], outcome="rejected", reason=error.code, message=str(error)
            )
            say(f"queue: {request['id']} rejected [{error.code}]")
            yield done, None
            continue
        queue.update(request["id"], run=run.run_id)
        try:
            result = run.run()
        except TrainError as error:
            if error.code == "train-busy":
                queue.release(request["id"])
                return
            done = queue.finish(request["id"], outcome="failed", reason=error.code)
            yield done, run.result()
            continue
        except KeyboardInterrupt:
            queue.finish(request["id"], outcome="failed", reason="train-run-cancelled")
            raise
        except Exception as error:  # run-failed already recorded and notified
            done = queue.finish(
                request["id"],
                outcome="failed",
                reason=run.failure or type(error).__name__,
            )
            yield done, run.result()
            continue
        done = queue.finish(
            request["id"], outcome=result["state"], ok=result["ok"], run=run.run_id
        )
        yield done, result
