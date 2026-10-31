"""In-cluster image builds that never block the controller's loop (0.42.0).

Before 0.42.0 the controller created a build Job and waited for it inside
its poll: for a 25-minute build nothing else ran (no other environment, no
approval, no status). Now :class:`ClusterBuilds` starts the Job, records it
in the controller's state (:data:`STATE_KEY`, by the build's identity) and
raises :class:`~piceli.infra.remote_builds.BuildPending`, as a build on a
build host does. Each poll the waiting step asks again and the Job is looked
at once: still running, the step keeps waiting (its status says the Job and
its phase); passed, its receipt is read as before; failed, the step fails
with the Job's log tail as before.

The build's identity is its change keys (the images it makes, each with
its key, and the platforms), not the commit: a commit that changes no input
of a running build keeps it, and its images serve the new revision. A
build with other inputs for the same environment stops the older Job
(superseded). ``piceli gitops cancel`` deletes it. A restarted controller
finds the record and looks at the same Job; with no record, starting it
again adopts a running Job of the same name.

A builder that cannot start a Job without waiting (``gitops run --once``,
``--local-build``, the test fakes) still builds while the step waits.

Importing this module is side-effect free.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from piceli.infra.remote_builds import (
    FAILED_KEY,
    BuildPending,
    _failure_error,
    _iso,
    build_id,
    components_body,
    identity,
    spec_body,
)

#: Where the controller's state keeps the running in-cluster build Jobs.
STATE_KEY = "cluster_builds"


class ClusterBuilds:
    """The in-cluster builder, each build Job looked at once per poll.

    :param inner: The in-cluster builder (a
        :class:`~piceli.infra.builders.JobBuilder`); one without
        ``builds_async`` builds while the step waits, as before.
    :param owner: The environment whose step asks (its key).
    """

    def __init__(
        self,
        inner: Any,
        state: dict[str, Any],
        *,
        platforms: Sequence[str],
        clock: Callable[[], float],
        owner: Callable[[], str | None] = lambda: None,
    ) -> None:
        self.inner = inner
        self.state = state
        self.platforms = tuple(platforms)
        self.clock = clock
        self.owner = owner

    def __getattr__(self, name: str) -> Any:
        if name == "inner":
            raise AttributeError(name)
        return getattr(self.inner, name)

    @property
    def environment(self) -> Any:
        return getattr(self.inner, "environment", None)

    @environment.setter
    def environment(self, value: Any) -> None:
        if hasattr(self.inner, "environment"):
            self.inner.environment = value

    @property
    def attempt(self) -> Any:
        return getattr(self.inner, "attempt", 1)

    @attempt.setter
    def attempt(self, value: Any) -> None:
        if hasattr(self.inner, "attempt"):
            self.inner.attempt = value

    @property
    def asynchronous(self) -> bool:
        return bool(getattr(self.inner, "builds_async", False))

    def _jobs(self) -> dict[str, Any]:
        return self.state.setdefault(STATE_KEY, {})

    def key(self, kind: str, work: Any) -> str:
        """The build's identity: its kind, change keys and platforms."""
        body = spec_body(work) if kind == "spec" else components_body(work, {})
        return build_id(
            kind, {**identity(kind, body), "platforms": list(self.platforms)}
        )

    def running(self, kind: str, work: Any) -> bool:
        """Whether a Job of this build runs (0.42.0): the step looks at it
        instead of offering the build to the hosts again."""
        return self.asynchronous and self.key(kind, work) in self._jobs()

    def cancelled(self, kind: str, work: Any) -> None:
        """Raise the cancel recorded for this build (``piceli gitops cancel``),
        until ``piceli gitops sync``."""
        found = self.key(kind, work)
        failure = (self.state.get(FAILED_KEY) or {}).get(found)
        if failure is not None and failure.get("cancelled"):
            raise _failure_error(failure, found)

    def supersede(self, kind: str, work: Any) -> list[str]:
        """Stop this environment's Jobs of other builds; their Job names."""
        owner = self.owner()
        if owner is None or not self.asynchronous:
            return []
        keep = self.key(kind, work)
        jobs = self._jobs()
        stopped = []
        for key in [
            k for k, v in jobs.items() if k != keep and v.get("owner") == owner
        ]:
            record = jobs.pop(key)
            stop(self.inner, record)
            stopped.append(str(record.get("job")))
        return stopped

    def build_spec(self, request: Any, checkout: Any) -> Any:
        if not request.images or not self.asynchronous:
            return self.inner.build_spec(request, checkout)
        return self._run("spec", request, sorted(request.images))

    def build(self, items: Sequence[Any], checkout: Any) -> Any:
        if not items or not self.asynchronous:
            return self.inner.build(items, checkout)
        return self._run("components", items, sorted(i.component for i in items))

    def _run(self, kind: str, work: Any, names: list[str]) -> Any:
        self.cancelled(kind, work)
        self.supersede(kind, work)
        found = self.key(kind, work)
        jobs = self._jobs()
        now = self.clock()
        owner = self.owner()
        record = jobs.get(found)
        if record is None:
            record = dict(self.inner.start(kind, work))
            record.update(
                id=found,
                kind=kind,
                images=names,
                began=_iso(now),  # the provenance's start, on the loop's clock
                started_at=_iso(now),
                started_epoch=now,
            )
            jobs[found] = record
        record["asked_epoch"] = now
        if owner is not None:
            record["owner"] = owner
        try:
            built = self.inner.collect(
                kind, work, record, max(0.0, now - float(record["started_epoch"]))
            )
        except Exception:
            jobs.pop(found, None)
            raise
        if built is None:
            raise BuildPending(waiting(record))
        jobs.pop(found, None)
        return built


def waiting(record: Mapping[str, Any]) -> str:
    """What the step waits for, for example ``building web in the cluster:
    Job piceli-image-build-0a1b2c3d4e since 2026-01-01T00:00:00Z (running)``."""
    text = (
        f"building {', '.join(record.get('images') or ())} in the cluster: "
        f"Job {record.get('job')} since {record.get('started_at')}"
    )
    if record.get("phase"):
        text += f" ({record['phase']})"
    return text


def stop(builder: Any, record: Mapping[str, Any]) -> bool:
    """Delete a recorded Job through ``builder`` (never raises)."""
    method = getattr(builder, "stop", None)
    if not callable(method):
        return False
    try:
        return bool(method(record))
    except Exception:  # a delete that fails never fails the poll
        return False


def owned(state: Mapping[str, Any], owners: set[str]) -> list[str]:
    """The recorded Jobs of ``owners``, by build identity."""
    return [
        key
        for key, item in (state.get(STATE_KEY) or {}).items()
        if item.get("owner") in owners
    ]


def cancel(
    state: dict[str, Any], key: str, by: Any, now: float, builder: Any
) -> dict[str, Any] | None:
    """``piceli gitops cancel``: delete the Job and count its inputs as
    cancelled (the waiting step fails ``cancelled``) until ``gitops sync``."""
    record = (state.get(STATE_KEY) or {}).pop(key, None)
    if record is None:
        return None
    stop(builder, record)
    state.setdefault(FAILED_KEY, {})[key] = {
        "host": "cluster",
        "reason": "cancelled" + (f" by {str(by)[:120]}" if by else ""),
        "log_tail": "",
        "owner": record.get("owner"),
        "at": _iso(now),
        "cancelled": True,
    }
    return record


def settle(state: dict[str, Any], owner: str, builder: Any) -> list[str]:
    """``owner``'s step ended without waiting: its Jobs are stopped."""
    jobs = state.get(STATE_KEY) or {}
    gone = [key for key, item in jobs.items() if item.get("owner") == owner]
    for key in gone:
        stop(builder, jobs.pop(key))
    return gone


def prune(state: dict[str, Any], now: float, max_age: float, builder: Any) -> list[str]:
    """Stop the Jobs no step asked about in ``max_age`` seconds."""
    jobs = state.get(STATE_KEY) or {}
    gone = [
        key
        for key, item in jobs.items()
        if now - float(item.get("asked_epoch") or item.get("started_epoch") or 0)
        > max_age
    ]
    for key in gone:
        stop(builder, jobs.pop(key))
    return gone


def public(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """``cluster_builds`` in the controller's status (oldest first)."""
    return [
        {
            key: item.get(key)
            for key in ("id", "kind", "images", "job", "owner", "started_at", "phase")
            if item.get(key) is not None
        }
        for item in sorted(
            (state.get(STATE_KEY) or {}).values(),
            key=lambda item: float(item.get("started_epoch") or 0),
        )
    ]
