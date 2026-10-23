"""The train run: joint trials, bisection, promotion and push.

A *trial* merges a set of candidates, in order, onto the train's current
head (``git merge --no-ff`` each, in a temporary worktree), loads the spec
from the merged tree and runs its test steps; the first failing step ends
it. The run starts with one trial of every candidate. When it passes, all
join. When it fails, the set is split in halves: the first half is tried on
the head, its passing members join, then the second half is tried on the
new head, recursively. A single candidate that fails is ejected with the
failing step (or ``train-merge-conflict``) and its output tail. So every
joined head is a commit whose tree passed every step, and joined branches
keep the order given.

Parallel halves (``Train(parallel=...)``): after a failed trial, the left
half's merge is made first; the left half is judged on it while the right
half is judged, at the same time, on top of it (speculating that the whole
left half joins). When it does, the right half's verdict stands (its trial
was on exactly the new head); otherwise the speculative trial is discarded
and the right half is judged again on the head the left half reached. The
verdicts are those of the sequential order.

Every run that joined something points ``refs/piceli/train/<name>/last`` at
its head. ``--promote`` tags the head and moves ``tested`` and ``base`` by
fast-forward only, then runs the publish steps (the outputs of test steps
that declared them restored first); ``--push`` pushes the tag and both
branches (atomically). ``--promote-run RUN`` promotes the head of a
reviewed earlier run without running it again. Nothing else changes a ref.

Importing this module is side-effect free.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from piceli.train.git import Git, Merged
from piceli.train.model import ImagesStep, Train, TrainError
from piceli.train.reuse import Store
from piceli.train.runner import (
    EventSink,
    Runner,
    base_environment,
    no_events,
    now_iso,
)
from piceli.train.spec import LoadedSpec, load
from piceli.train.state import RUN_SCHEMA, TrainState, new_run_id
from piceli.train.steps import StepContext, StepResult, log_path, run_step
from piceli.train.summary import summarize


@dataclass
class Candidate:
    """One branch in the run, its pinned tip and its verdict."""

    branch: str
    tip: str
    verdict: str = "pending"
    reason: str | None = None
    step: str | None = None
    tail: str = ""
    trial: int | None = None
    files: tuple[str, ...] = ()

    def to_dict(self, trials: Mapping[int, dict[str, Any]]) -> dict[str, Any]:
        body: dict[str, Any] = {
            "branch": self.branch,
            "tip": self.tip,
            "verdict": self.verdict,
        }
        if self.reason:
            body["reason"] = self.reason
        if self.step:
            body["step"] = self.step
        if self.tail:
            body["tail"] = self.tail
        if self.files:
            body["files"] = list(self.files)
        if self.trial is not None:
            body["trial"] = self.trial
            body["steps"] = trials[self.trial]["steps"]
        return body


@dataclass
class Trial:
    """One trial: candidates merged onto a head and the steps' results."""

    id: int
    base: str
    branches: list[str]
    started: float
    head: str | None = None
    steps: list[StepResult] = field(default_factory=list)
    verdict: str = "running"
    reason: str | None = None
    failed_step: str | None = None
    conflict: str | None = None
    files: tuple[str, ...] = ()
    spec: str = "merged-tree"
    duration: float = 0.0
    #: Judged at the same time as the half before it, on top of it.
    speculative: bool = False
    #: A speculative trial whose base the run did not reach (not counted).
    discarded: bool = False

    @property
    def passed(self) -> bool:
        return self.verdict == "passed"

    def tail(self) -> str:
        for step in self.steps:
            if not step.passed:
                return step.tail
        return ""

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "id": self.id,
            "base": self.base,
            "head": self.head,
            "branches": self.branches,
            "verdict": self.verdict,
            "started": now_iso(self.started),
            "duration": round(self.duration, 3),
            "spec": self.spec,
            "steps": [step.to_dict() for step in self.steps],
        }
        if self.reason:
            body["reason"] = self.reason
        if self.failed_step:
            body["step"] = self.failed_step
        if self.conflict:
            body["conflict"] = {"branch": self.conflict, "files": list(self.files)}
        if self.speculative:
            body["speculative"] = True
        if self.discarded:
            body["discarded"] = True
        return body


class TrainRun:
    """One run of a train over candidate branches.

    :param spec: The train as loaded from the caller's checkout (its
        identity: name, base, tested, tag, remote).
    :param repo: The repository's top level.
    :param spec_path: The spec module relative to ``repo`` (loaded from each
        merged tree), or ``None`` when it lives outside the repository (its
        steps then come from ``spec``).
    :param runner: Where commands run (default :class:`LocalRunner`).
    :param events: Receives one dict per event (:mod:`piceli.train.runner`).
        Every event is also appended to ``runs/<id>/events.jsonl``.
    :param observe: Attach the train's own observers: its telemetry
        (:mod:`piceli.train.telemetry`) and, when the run ends, its
        notifications (:mod:`piceli.train.notify`).
    :param warn: Prints an observer's warning line (default: nowhere).
    :param queue: The queued request this run serves (recorded as ``queue``).
    :param promote_run: Promote the head of this earlier run (its id, or
        ``last``) instead of running branches (``--promote-run``); the run is
        then a promotion only, with the same record, events, observers and
        ``run-failed`` handling as any other.

    An observer (an event sink, the telemetry, a notifier) that fails never
    changes the run.
    """

    def __init__(
        self,
        spec: LoadedSpec,
        *,
        repo: Path,
        spec_path: str | None,
        branches: Sequence[str],
        state: TrainState,
        promote: bool = False,
        push: bool = False,
        runner: Runner | None = None,
        events: EventSink = no_events,
        environment: Mapping[str, str] | None = None,
        store: Store | None = None,
        images: Any = None,
        observe: bool = True,
        warn: Callable[[str], None] = lambda _message: None,
        queue: str | None = None,
        promote_run: str | None = None,
    ) -> None:
        if push and not promote:
            raise TrainError("train-push-needs-promote", "--push needs --promote")
        self.spec = spec
        self.train: Train = spec.train
        self.repo = repo
        self.spec_path = spec_path
        self.branches = list(branches)
        self.state = state
        self.promote_requested = promote
        self.push_requested = push
        if runner is None:
            from piceli.train.hosts import build_runner

            runner = build_runner(self.train, repo)
        self.runner: Runner = runner
        if store is None:
            from piceli.train.local import cache_root

            store = Store(cache_root(self.train.cache) / "store")
        self.store = store
        #: How an images step builds, pushes and offers (tests pass a fake;
        #: default :func:`piceli.train.images.cluster_tools`).
        self.images = images
        from piceli.train.local import Checkouts, cache_root

        self.checkouts = Checkouts(cache_root(self.train.cache), self.train.name, repo)
        self._events = events
        self._lock = threading.RLock()
        self.environment = dict(
            environment if environment is not None else base_environment()
        )
        self.git = Git(repo)
        self.run_id = new_run_id()
        self.trials: list[Trial] = []
        self.candidates: list[Candidate] = []
        self.started = time.time()
        self.start = ""
        self.head = ""
        self.promotion: dict[str, Any] | None = None
        self.pushed: dict[str, Any] | None = None
        self.flakes: list[dict[str, Any]] = []
        self.finished: float | None = None
        self.last: dict[str, Any] | None = None
        self.promoted_from: str | None = None
        self.pruned: dict[str, Any] | None = None
        self.observe = observe
        self.warn = warn
        self.observers: list[EventSink] = []
        self.notified: list[dict[str, Any]] = []
        self.queue_id = queue
        #: ``--promote-run``: the run as asked (an id or ``last``).
        self.promote_run_of = promote_run
        #: ``Runner`` hook: the host a command was placed on (``step-placed``).
        self._placing: dict[str, dict[str, Any]] = {}
        if promote_run is not None:
            self.promote_requested = True
        #: The reason code of a run that raised midway (``run-failed``).
        self.failure: str | None = None

    # ----------------------------------------------------------- the run

    def prepare(self) -> None:
        """Check the inputs before anything runs (raises :class:`TrainError`).

        Idempotent: :meth:`run` calls it when the caller did not.
        """
        if self.start:
            return
        if self.observe:
            from piceli.train.telemetry import check_export

            check_export(self.train.telemetry)
        if self.promote_run_of is not None:
            self._prepare_promote(self.promote_run_of)
            return
        if not self.branches:
            raise TrainError("train-branches-required", "name at least one branch")
        if len(set(self.branches)) != len(self.branches):
            raise TrainError("train-branch-unknown", "a branch is named twice")
        start = self.git.ref(self.train.base)
        if start is None:
            raise TrainError(
                "train-repo-invalid",
                f"the base branch {self.train.base!r} does not exist",
            )
        self.start = self.head = start
        for branch in self.branches:
            if branch in {self.train.base, self.train.tested}:
                raise TrainError(
                    "train-branch-unknown", f"{branch!r} is the train's own branch"
                )
            tip = self.git.commit(branch) if not branch.startswith("-") else None
            if tip is None:
                raise TrainError("train-branch-unknown", f"no branch {branch!r}")
            self.candidates.append(Candidate(branch, tip))

    @property
    def last_ref(self) -> str:
        return f"refs/piceli/train/{self.train.name}/last"

    def run(self) -> dict[str, Any]:
        """Run the train; returns the result (also written as the run record)."""
        self.prepare()
        with self.state.lock(self.run_id):
            self.state.run_dir(self.run_id)
            telemetry = None
            if self.observe:
                from piceli.train.telemetry import telemetry_for

                telemetry = telemetry_for(self.train.telemetry, self.warn)
                if telemetry is not None:
                    self.observers.append(telemetry)
            on_place = getattr(self.runner, "on_place", False)
            if on_place is None:  # a runner that places commands on hosts
                self.runner.on_place = self._placed  # type: ignore[attr-defined]
            try:
                if self.promote_run_of is not None:
                    return self._promote_run()
                return self._run()
            except BaseException as error:
                self._failed(error)
                raise
            finally:
                if on_place is None:
                    self.runner.on_place = None  # type: ignore[attr-defined]
                if telemetry is not None:
                    telemetry.close()

    def _failed(self, error: BaseException) -> None:
        """A run that raised midway (a Git failure, a crash, Ctrl-C): record
        it ``failed`` with the reason, emit ``run-failed`` with the summary
        and send the notifications. Never raises."""
        if isinstance(error, TrainError):
            self.failure = error.code
        elif isinstance(error, KeyboardInterrupt):
            self.failure = "train-run-cancelled"
        else:
            self.failure = "train-run-failed"
        try:
            self.finished = time.time()
            result = self.result()
            self.state.write_record(self.run_id, result)
            summary = summarize(result)
            self.emit(
                "run-failed",
                state="failed",
                ok=False,
                reason=self.failure,
                duration=result["duration"],
                summary=summary,
            )
            self._notify(summary)
        except BaseException:  # the original error is what the caller sees
            pass

    def _notify(self, summary: Mapping[str, Any]) -> None:
        if not (self.observe and self.train.notify):
            return
        from piceli.train.notify import send_all

        self.notified = send_all(self.train.notify, summary, self.warn)
        for outcome in self.notified:
            if outcome["state"] == "failed":
                self.emit("notify-failed", **outcome)
        self.state.write_record(self.run_id, self.result())

    def _run(self) -> dict[str, Any]:
        self.emit(
            "run-started",
            run=self.run_id,
            train=self.train.name,
            base=self.start,
            branches=self.branches,
        )
        pending = []
        for candidate in self.candidates:
            if self.git.is_ancestor(candidate.tip, self.head):
                candidate.verdict = "included"
                self.emit("branch-included", branch=candidate.branch)
            else:
                pending.append(candidate)
        self.head = self._resolve(self.head, pending)
        if self.head != self.start:
            self.git.set_ref(self.last_ref, self.head)
            self.last = {"ref": self.last_ref, "head": self.head}
        if self.promote_requested:
            self.promotion = self._promote()
            if self.push_requested and self.promotion.get("state") in {
                "promoted",
                "publish-failed",
            }:
                self.pushed = self._push(self.promotion["tag"])
        self.finished = time.time()
        at = now_iso(self.finished)
        self.flakes = self.state.record_flakes(self._observed_flakes(), self.run_id, at)
        return self._conclude(self._finish())

    def _conclude(self, result: dict[str, Any]) -> dict[str, Any]:
        """Emit ``run-finished`` with the summary and send the notifications."""
        summary = summarize(result)
        self.emit(
            "run-finished",
            state=result["state"],
            ok=result["ok"],
            duration=result["duration"],
            summary=summary,
        )
        if self.observe and self.train.notify:
            self._notify(summary)
            result = self.result()
        return result

    def _finish(self) -> dict[str, Any]:
        """Write the record, prune what the train keeps (never failing the
        run), and write the record again with the sizes."""
        self.state.write_record(self.run_id, self.result())
        try:
            from piceli.train.local import cache_root
            from piceli.train.prune import prune

            done = prune(
                self.train.name,
                cache=cache_root(self.train.cache),
                state_root=self.state.root,
                retention=self.train.retention,
                current=self.run_id,
            )
            self.pruned = {
                "freed": done["freed"],
                "removed": len(done["removed"]),
                "sizes": done["sizes"],
                "limits": done["limits"],
            }
        except Exception as error:  # a prune never fails the run
            self.pruned = {"state": "failed", "message": type(error).__name__}
        if self.pruned:
            self.emit("pruned", **self.pruned)
        result = self.result()
        self.state.write_record(self.run_id, result)
        return result

    def _resolve(
        self, head: str, group: list[Candidate], premerged: str | None = None
    ) -> str:
        if not group:
            return head
        trial = self._trial(head, group, premerged=premerged)
        return self._judged(head, group, trial)

    def _judged(self, head: str, group: list[Candidate], trial: Trial) -> str:
        """Join ``group`` on a passed trial; else eject it (one branch) or
        bisect it."""
        if trial.passed:
            for candidate in group:
                candidate.verdict = "joined"
                candidate.trial = trial.id
                self.emit("branch-joined", branch=candidate.branch, trial=trial.id)
            assert trial.head is not None
            return trial.head
        if len(group) == 1:
            candidate = group[0]
            candidate.verdict = "ejected"
            candidate.reason = trial.reason
            candidate.step = trial.failed_step
            candidate.tail = trial.tail()
            candidate.files = trial.files
            candidate.trial = trial.id
            self.emit(
                "branch-ejected",
                branch=candidate.branch,
                trial=trial.id,
                reason=trial.reason,
                step=trial.failed_step,
            )
            return head
        middle = (len(group) + 1) // 2
        left, right = group[:middle], group[middle:]
        if self._parallel():
            merged = self.git.merged(
                head, [(c.branch, c.tip) for c in left], self.train.name
            )
            if merged.head is not None:
                return self._halves(head, left, right, merged.head)
        head = self._resolve(head, left)
        return self._resolve(head, right)

    def _halves(
        self, head: str, left: list[Candidate], right: list[Candidate], merged: str
    ) -> str:
        """Judge ``left`` on ``head`` (its merge made: ``merged``) while
        ``right`` is judged on ``merged`` (see the module doc)."""
        self.emit(
            "halves-parallel",
            left=[c.branch for c in left],
            right=[c.branch for c in right],
            base=merged,
        )
        with ThreadPoolExecutor(1, thread_name_prefix="half") as pool:
            ahead = pool.submit(self._trial, merged, right, speculative=True)
            reached = self._resolve(head, left, premerged=merged)
            speculated = ahead.result()
        if reached == merged:
            return self._judged(merged, right, speculated)
        with self._lock:
            speculated.discarded = True
        self.emit("trial-discarded", trial=speculated.id, base=merged, reached=reached)
        return self._resolve(reached, right)

    def _parallel(self) -> bool:
        mode = self.train.parallel
        if mode == "off":
            return False
        free = getattr(self.runner, "free_slots", None)
        if mode == "on":
            count = getattr(self.runner, "host_count", None)
            return callable(count) and count() >= 2
        return callable(free) and free() >= 2

    # ------------------------------------------------------------ trials

    def _trial(
        self,
        head: str,
        group: list[Candidate],
        *,
        premerged: str | None = None,
        speculative: bool = False,
    ) -> Trial:
        with self._lock:
            trial = Trial(
                len(self.trials) + 1, head, [c.branch for c in group], time.time()
            )
            trial.speculative = speculative
            self.trials.append(trial)
        clock = time.monotonic()
        self.emit(
            "trial-started",
            trial=trial.id,
            base=head,
            branches=trial.branches,
            **({"speculative": True} if speculative else {}),
        )
        log_dir = self.state.run_dir(self.run_id)
        with (
            self.checkouts.take() as place,
            self.git.worktree(premerged or head, path=place) as checkout,
        ):
            merge_started, merge_clock = time.time(), time.monotonic()
            if premerged is not None:
                self.checkouts.keep_times(checkout)
            merged = (
                Merged(premerged)
                if premerged is not None
                else self.git.merge(
                    checkout, [(c.branch, c.tip) for c in group], self.train.name
                )
            )
            merge = StepResult(
                "merge",
                "merge",
                "passed" if merged.head else "failed",
                merge_started,
                time.monotonic() - merge_clock,
                "local",
            )
            trial.steps.append(merge)
            if merged.head is None:
                trial.verdict, trial.reason, trial.failed_step = (
                    "failed",
                    "train-merge-conflict",
                    "merge",
                )
                trial.conflict, trial.files = merged.conflict, merged.files
                merge.detail = {
                    "conflict": merged.conflict,
                    "files": list(merged.files),
                }
                merge.tail = f"{merged.conflict} conflicts in: " + ", ".join(
                    merged.files
                )
            else:
                trial.head = merged.head
                if premerged is None:
                    self.checkouts.keep_times(checkout)
                self._run_steps(trial, checkout, log_dir)
        trial.duration = time.monotonic() - clock
        self.emit(
            "trial-finished",
            trial=trial.id,
            branches=trial.branches,
            verdict=trial.verdict,
            step=trial.failed_step,
            duration=round(trial.duration, 3),
            **({"speculative": True} if trial.speculative else {}),
        )
        with self._lock:
            self.state.write_record(self.run_id, self.result())
        return trial

    def _spec_steps(self, checkout: Path) -> tuple[Train, str]:
        if self.spec_path is None:
            return self.train, "caller"
        path = checkout / self.spec_path
        if not path.is_file():
            raise TrainError(
                "train-spec-invalid", f"{self.spec_path} does not exist in this commit"
            )
        try:
            loaded = load(f"{path}:{self.spec.attr}", checkout)
        except TrainError as error:
            raise TrainError("train-spec-invalid", str(error)) from None
        return loaded.train, "merged-tree"

    def _run_steps(self, trial: Trial, checkout: Path, log_dir: Path) -> None:
        assert trial.head is not None
        started, clock = time.time(), time.monotonic()
        try:
            train, source = self._spec_steps(checkout)
        except TrainError as error:
            trial.steps.append(
                StepResult(
                    "spec",
                    "spec",
                    "failed",
                    started,
                    time.monotonic() - clock,
                    "local",
                    tail=str(error),
                )
            )
            trial.verdict, trial.reason, trial.failed_step = (
                "failed",
                "train-spec-invalid",
                "spec",
            )
            return
        trial.spec = source
        trial.steps.append(
            StepResult(
                "spec",
                "spec",
                "passed",
                started,
                time.monotonic() - clock,
                "local",
                detail={"source": source, "steps": [s.name for s in train.test_steps]},
            )
        )
        own = self._own_env(trial.base, trial.head, trial.branches)
        ctx = StepContext(
            checkout=checkout,
            base=trial.base,
            head=trial.head,
            branches=trial.branches,
            changed=self.git.changed(trial.base, trial.head),
            env={**self.environment, **own},
            log_dir=log_dir,
            prefix=f"{trial.id:02d}-",
            runner=self.runner,
            events=self.events,
            own_env=own,
            store=self.store,
            origin={"train": self.train.name, "run": self.run_id, "trial": trial.id},
        )
        for step in train.test_steps:
            self._step_started(trial.id, step.name, step.kind, ctx)
            result = run_step(step, ctx)
            trial.steps.append(result)
            self._step_done(str(log_path(ctx, step.name)))
            self.emit("step-finished", **self._step_fields(trial.id, result, ctx))
            if not result.passed:
                trial.verdict, trial.failed_step = "failed", step.name
                trial.reason = (
                    "train-step-timeout"
                    if result.verdict == "timeout"
                    else "train-step-failed"
                )
                return
        trial.verdict = "passed"

    def _step_started(
        self, trial: int | None, step: str, kind: str, ctx: StepContext
    ) -> None:
        log = str(log_path(ctx, step))
        with self._lock:
            self._placing[log] = {"trial": trial, "step": step}
        self.emit(
            "step-started",
            trial=trial,
            step=step,
            kind=kind,
            host=self.runner.host,
            branches=list(ctx.branches),
            log=log,
        )

    def _placed(self, log: Path, host: str) -> None:
        """A command of a running step went to ``host`` (a build host, or
        here): ``step-placed``, so a status view shows where it runs now."""
        with self._lock:
            step = self._placing.get(str(log))
        if step is not None:
            self.emit("step-placed", **step, host=host, log=str(log))

    def _step_done(self, log: str | None) -> None:
        with self._lock:
            self._placing.pop(str(log), None)

    @staticmethod
    def _step_fields(
        trial: int | None, result: StepResult, ctx: StepContext
    ) -> dict[str, Any]:
        """A finished step's event fields: what status views, telemetry and
        notifications read (no output beyond the failing tail)."""
        fields: dict[str, Any] = {
            "trial": trial,
            "step": result.name,
            "kind": result.kind,
            "verdict": result.verdict,
            "started": now_iso(result.started),
            "duration": round(result.duration, 3),
            "host": result.host,
            "log": result.log,
            "branches": list(ctx.branches),
        }
        if "selection" in result.detail:
            fields["selection"] = result.detail["selection"]
        reused = result.detail.get("reused")
        if result.verdict == "reused" and isinstance(reused, Mapping):
            fields["reused"] = {
                key: reused.get(key) for key in ("run", "trial", "host", "at")
            }
        if result.flaky:
            fields["flaky"] = [
                {"target": item["target"], "test": item["test"]}
                for item in result.flaky
            ]
        if result.tail and not result.passed:
            fields["tail"] = result.tail
        return fields

    def _own_env(
        self, base: str, head: str, branches: Sequence[str], **extra: str
    ) -> dict[str, str]:
        """The variables the train sets for a step."""
        return {
            "PICELI_TRAIN": self.train.name,
            "PICELI_TRAIN_BASE": base,
            "PICELI_TRAIN_HEAD": head,
            "PICELI_TRAIN_BRANCHES": " ".join(branches),
            **extra,
        }

    # -------------------------------------------------- promote and push

    def joined(self) -> list[Candidate]:
        return [c for c in self.candidates if c.verdict == "joined"]

    def _next_tag(self) -> str:
        date = datetime.now(UTC).strftime("%Y-%m-%d")
        pattern = re.escape(self.train.tag).replace(r"\{n\}", r"(\d+)")
        pattern = pattern.replace(r"\{date\}", re.escape(date))
        pattern = pattern.replace(r"\{name\}", re.escape(self.train.name))
        matcher = re.compile(pattern)
        glob = self.train.tag.format(date=date, n="*", name=self.train.name)
        used = [
            int(match.group(1))
            for name in self.git.tags(glob)
            if (match := matcher.fullmatch(name))
        ]
        return self.train.tag_name(date, max(used, default=0) + 1)

    def _refuse(self, reason: str, message: str) -> dict[str, Any]:
        self.emit("promote-rejected", reason=reason, message=message)
        return {"state": "rejected", "reason": reason, "message": message}

    def _promote(self) -> dict[str, Any]:
        train, head = self.train, self.head
        if head == self.start:
            return {"state": "nothing-joined"}
        base_now = self.git.ref(train.base)
        tested_now = self.git.ref(train.tested)
        for name, current in ((train.base, base_now), (train.tested, tested_now)):
            if current is not None and not self.git.is_ancestor(current, head):
                return self._refuse(
                    "train-not-fast-forward",
                    f"{name} ({current[:12]}) is not an ancestor of the train's "
                    f"head ({head[:12]}); it moved outside the train",
                )
        if base_now is None:
            return self._refuse(
                "train-not-fast-forward", f"{train.base} no longer exists"
            )
        for name in (train.base, train.tested):
            checkout = self.git.checked_out(name)
            if checkout is not None and self.git.dirty(checkout):
                return self._refuse(
                    "train-checkout-dirty",
                    f"{name} is checked out in {checkout} with uncommitted changes",
                )
        tag = self._next_tag()
        joined = ", ".join(c.branch for c in self.joined())
        self.git.tag(tag, head, f"train {train.name}: {joined}")
        self.git.fast_forward(train.tested, tested_now, head)
        self.git.fast_forward(train.base, base_now, head)
        self.emit("promoted", tag=tag, head=head)
        published = self._publish(tag)
        state = "promoted" if all(s.passed for s in published) else "publish-failed"
        body: dict[str, Any] = {
            "state": state,
            "tag": tag,
            "head": head,
            "tested": train.tested,
            "base": train.base,
            "publish": [s.to_dict() for s in published],
        }
        if state == "publish-failed":
            body["reason"] = "train-publish-failed"
        return body

    def _publish(self, tag: str) -> list[StepResult]:
        results: list[StepResult] = []
        with (
            self.checkouts.take() as place,
            self.git.worktree(self.head, path=place) as checkout,
        ):
            self.checkouts.keep_times(checkout)
            started, clock = time.time(), time.monotonic()
            try:
                train, _source = self._spec_steps(checkout)
            except TrainError as error:
                return [
                    StepResult(
                        "spec",
                        "spec",
                        "failed",
                        started,
                        time.monotonic() - clock,
                        "local",
                        tail=str(error),
                    )
                ]
            own = self._own_env(
                self.start,
                self.head,
                [c.branch for c in self.joined()],
                PICELI_TRAIN_TAG=tag,
            )
            ctx = StepContext(
                checkout=checkout,
                base=self.start,
                head=self.head,
                branches=[c.branch for c in self.joined()],
                changed=self.git.changed(self.start, self.head),
                env={**self.environment, **own},
                log_dir=self.state.run_dir(self.run_id),
                prefix="publish-",
                runner=self.runner,
                events=self.events,
                own_env=own,
                store=self.store,
                origin={"train": self.train.name, "run": self.run_id, "trial": None},
                base_branch=self.train.base,
            )
            restored = self._restore_tested(train, checkout)
            if restored:
                self.emit("outputs-restored", steps=restored)
            for step in train.publish_steps:
                self._step_started(None, step.name, step.kind, ctx)
                if isinstance(step, ImagesStep):
                    from piceli.train.images import run_images_step

                    result = run_images_step(step, ctx, tag=tag, tools=self.images)
                else:
                    result = run_step(step, ctx)
                results.append(result)
                self._step_done(str(log_path(ctx, step.name)))
                self.emit(
                    "published" if result.passed else "publish-failed",
                    **self._step_fields(None, result, ctx),
                )
                if not result.passed:
                    break
        return results

    def _restore_tested(self, train: Train, checkout: Path) -> list[str]:
        """Restore, into the publish checkout, the outputs of every test step
        that declared some and passed on this tree (the tested binaries are
        the ones published, not a rebuild); the steps restored."""
        from piceli.train.reuse import step_key

        found = []
        for step in train.test_steps:
            if not step.outputs or not step.inputs:
                continue
            extra = (
                {"selection": self._selection_of(step.name)}
                if step.cargo and step.kind != "command"
                else {}
            )
            key, _count = step_key(step.to_dict(), step.inputs, checkout, extra)
            record = self.store.lookup(key)
            if record is not None:
                self.store.restore(record, checkout)
                found.append(step.name)
        return found

    def _selection_of(self, step: str) -> Any:
        """The impact selection a step of the trial that reached the head used."""
        for trial in reversed(self.trials):
            if trial.head == self.head:
                for result in trial.steps:
                    if result.name == step:
                        return result.detail.get("selection")
        return None

    def _push(self, tag: str) -> dict[str, Any]:
        refs = [
            f"refs/tags/{tag}",
            f"refs/heads/{self.train.tested}",
            f"refs/heads/{self.train.base}",
        ]
        done = self.git.push(self.train.remote, refs)
        if done.returncode == 0:
            self.emit("pushed", remote=self.train.remote, refs=refs)
            return {"state": "pushed", "remote": self.train.remote, "refs": refs}
        lines = (done.stderr or done.stdout).strip().splitlines()
        self.emit("push-failed", remote=self.train.remote, refs=refs)
        return {
            "state": "failed",
            "reason": "train-push-failed",
            "remote": self.train.remote,
            "refs": refs,
            "message": lines[-1] if lines else "git push failed",
        }

    # ------------------------------------------------------------ output

    def _observed_flakes(self) -> list[dict[str, Any]]:
        return [
            {**item, "trial": trial.id}
            for trial in self.trials
            for step in trial.steps
            for item in step.flaky
        ]

    def emit(self, event: str, **fields: Any) -> None:
        """Record one event (``events.jsonl``) and hand it to every sink; a
        sink that raises never stops the run."""
        self.emit_raw({"event": event, "at": now_iso(), "run": self.run_id, **fields})

    @property
    def events(self) -> EventSink:
        return self.emit_raw

    def emit_raw(self, event: Mapping[str, Any]) -> None:
        """Record an event a step made (already shaped) as :meth:`emit` does."""
        with self._lock:
            try:
                self.state.append_event(self.run_id, event)
            except OSError:
                pass
            for sink in (self._events, *self.observers):
                try:
                    sink(event)
                except Exception:  # an observer never changes the run
                    pass

    def ejected(self) -> list[Candidate]:
        return [c for c in self.candidates if c.verdict == "ejected"]

    def ok(self) -> bool:
        """Every branch joined (or was already in), and promote and push did
        what was asked."""
        if self.ejected():
            return False
        if self.promotion and self.promotion["state"] not in {
            "promoted",
            "nothing-joined",
        }:
            return False
        return not (self.pushed and self.pushed["state"] != "pushed")

    def result(self) -> dict[str, Any]:
        """The run's result (``piceli.train.v1``)."""
        with self._lock:
            return self._result()

    def _result(self) -> dict[str, Any]:
        trials = {t.id: t.to_dict() for t in self.trials}
        end = self.finished or time.time()
        flaky = self.flakes or [
            {"key": f"{item['target']} {item['test']}", **item}
            for item in self._observed_flakes()
        ]
        tags = (
            [self.promotion["tag"]]
            if self.promotion and self.promotion.get("tag")
            else []
        )
        if self.failure is not None:
            state = "failed"
        elif self.finished is None:
            state = "running"
        else:
            state = "ejected" if self.ejected() else "joined"
        return {
            "schema": RUN_SCHEMA,
            "state": state,
            "ok": (
                False
                if self.failure
                else (self.ok() if self.finished is not None else None)
            ),
            "run": self.run_id,
            "train": self.train.name,
            "repo": str(self.repo),
            "base": self.train.base,
            "tested": self.train.tested,
            "start": self.start,
            "head": self.head,
            "started": now_iso(self.started),
            "duration": round(end - self.started, 3),
            "host": self.runner.host,
            "branches": [c.to_dict(trials) for c in self.candidates],
            "trials": list(trials.values()),
            "flaky": flaky,
            "tags": tags,
            "promote": self.promotion or {"state": "not-requested"},
            "push": self.pushed or {"state": "not-requested"},
            "record": str(self.state.runs / self.run_id / "run.json"),
            **({"last": self.last} if self.last else {}),
            **({"promoted_from": self.promoted_from} if self.promoted_from else {}),
            **({"prune": self.pruned} if self.pruned else {}),
            **({"notify": self.notified} if self.notified else {}),
            **({"reason": self.failure} if self.failure else {}),
            **({"queue": self.queue_id} if self.queue_id else {}),
        }

    # ------------------------------------------------- promote a past run

    def promote_run(self, run: str) -> dict[str, Any]:
        """Promote the head an earlier run reached, without running it again
        (``--promote-run``; the same as ``TrainRun(promote_run=run).run()``).
        Refused (``train-run-unknown``, ``train-not-fast-forward``) when the
        run is unknown, joined nothing, or the base moved in a way that is not
        a fast-forward of its head."""
        self.promote_run_of = run
        self.promote_requested = True
        return self.run()

    def _prepare_promote(self, run: str) -> None:
        record = self._record_of(run)
        head, start = str(record.get("head") or ""), str(record.get("start") or "")
        if not head or head == start or self.git.commit(head) is None:
            raise TrainError(
                "train-run-unknown", f"run {record.get('run')} joined nothing"
            )
        base_now = self.git.ref(self.train.base)
        if base_now is None or not self.git.is_ancestor(base_now, head):
            raise TrainError(
                "train-not-fast-forward",
                f"{self.train.base} moved since run {record.get('run')}: its head "
                f"({head[:12]}) is no fast-forward of {self.train.base} "
                f"({(base_now or '?')[:12]}); run the train again",
            )
        self.promoted_from = str(record.get("run"))
        self.start, self.head = base_now, head
        for item in record.get("branches") or ():
            candidate = Candidate(str(item["branch"]), str(item.get("tip") or ""))
            candidate.verdict = str(item.get("verdict") or "pending")
            self.candidates.append(candidate)
        self.branches = [c.branch for c in self.candidates]

    def _promote_run(self) -> dict[str, Any]:
        self.emit(
            "run-started",
            run=self.run_id,
            train=self.train.name,
            base=self.start,
            branches=self.branches,
            promote_run=self.promoted_from,
            verdicts={c.branch: c.verdict for c in self.candidates},
        )
        self.promotion = self._promote()
        if self.push_requested and self.promotion.get("state") in {
            "promoted",
            "publish-failed",
        }:
            self.pushed = self._push(self.promotion["tag"])
        self.finished = time.time()
        return self._conclude(self._finish())

    def _record_of(self, run: str) -> dict[str, Any]:
        if run == "last":
            for item in self.state.history():
                if item.get("head") and item.get("head") != item.get("start"):
                    if item.get("state") in {"joined", "ejected"}:
                        return item
            raise TrainError("train-run-unknown", "no earlier run joined anything")
        if not re.fullmatch(r"[0-9TZ]+-[0-9a-f]+", run):
            raise TrainError("train-run-unknown", f"{run!r} is not a run id")
        path = self.state.runs / run / "run.json"
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            raise TrainError("train-run-unknown", f"no run {run}") from None
        if record.get("state") not in {"joined", "ejected"}:
            raise TrainError("train-run-unknown", f"run {run} did not finish")
        return dict(record)


def locate(entry: str, cwd: Path) -> tuple[LoadedSpec, Path, str | None]:
    """Load the train ``entry`` names (from ``cwd``): ``(spec, repository
    top level, spec module relative to it or None when outside it)``."""
    loaded = load(entry, cwd)
    anchor = loaded.file.parent if loaded.file is not None else cwd
    directory = (anchor / loaded.train.repo).resolve()
    if not directory.is_dir():
        raise TrainError(
            "train-repo-invalid", f"{loaded.train.repo!r} is not a directory"
        )
    try:
        repo = Git(directory).toplevel()
    except TrainError:
        raise TrainError(
            "train-repo-invalid", f"{loaded.train.repo!r} is not in a Git repository"
        ) from None
    spec_path: str | None = None
    if loaded.file is not None and loaded.file.is_relative_to(repo):
        spec_path = loaded.file.relative_to(repo).as_posix()
    return loaded, repo, spec_path
