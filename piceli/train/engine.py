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

``--promote`` tags the head and moves ``tested`` and ``base`` by
fast-forward only, then runs the publish steps; ``--push`` pushes the tag
and both branches (atomically). Nothing else changes a ref.

Importing this module is side-effect free.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from piceli.train.git import Git
from piceli.train.model import Train, TrainError
from piceli.train.runner import (
    EventSink,
    LocalRunner,
    Runner,
    base_environment,
    no_events,
    now_iso,
)
from piceli.train.spec import LoadedSpec, load
from piceli.train.state import RUN_SCHEMA, TrainState, new_run_id
from piceli.train.steps import StepContext, StepResult, run_step


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
        self.runner: Runner = runner or LocalRunner()
        self.events = events
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

    # ----------------------------------------------------------- the run

    def prepare(self) -> None:
        """Check the inputs before anything runs (raises :class:`TrainError`).

        Idempotent: :meth:`run` calls it when the caller did not.
        """
        if self.start:
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

    def run(self) -> dict[str, Any]:
        """Run the train; returns the result (also written as the run record)."""
        self.prepare()
        with self.state.lock():
            self.state.run_dir(self.run_id)
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
            if self.promote_requested:
                self.promotion = self._promote()
                if self.push_requested and self.promotion.get("state") in {
                    "promoted",
                    "publish-failed",
                }:
                    self.pushed = self._push(self.promotion["tag"])
            self.finished = time.time()
            at = now_iso(self.finished)
            self.flakes = self.state.record_flakes(
                self._observed_flakes(), self.run_id, at
            )
            result = self.result()
            self.state.write_record(self.run_id, result)
            self.emit(
                "run-finished", state=result["state"], duration=result["duration"]
            )
            return result

    def _resolve(self, head: str, group: list[Candidate]) -> str:
        if not group:
            return head
        trial = self._trial(head, group)
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
        head = self._resolve(head, group[:middle])
        return self._resolve(head, group[middle:])

    # ------------------------------------------------------------ trials

    def _trial(self, head: str, group: list[Candidate]) -> Trial:
        trial = Trial(
            len(self.trials) + 1, head, [c.branch for c in group], time.time()
        )
        self.trials.append(trial)
        clock = time.monotonic()
        self.emit("trial-started", trial=trial.id, base=head, branches=trial.branches)
        log_dir = self.state.run_dir(self.run_id)
        with self.git.worktree(head) as checkout:
            merge_started, merge_clock = time.time(), time.monotonic()
            merged = self.git.merge(
                checkout, [(c.branch, c.tip) for c in group], self.train.name
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
                self._run_steps(trial, checkout, log_dir)
        trial.duration = time.monotonic() - clock
        self.emit(
            "trial-finished",
            trial=trial.id,
            verdict=trial.verdict,
            step=trial.failed_step,
            duration=round(trial.duration, 3),
        )
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
        ctx = StepContext(
            checkout=checkout,
            base=trial.base,
            head=trial.head,
            branches=trial.branches,
            changed=self.git.changed(trial.base, trial.head),
            env=self._step_env(trial.base, trial.head, trial.branches),
            log_dir=log_dir,
            prefix=f"{trial.id:02d}-",
            runner=self.runner,
            events=self.events,
        )
        for step in train.test_steps:
            self.emit("step-started", trial=trial.id, step=step.name, kind=step.kind)
            result = run_step(step, ctx)
            trial.steps.append(result)
            self.emit(
                "step-finished",
                trial=trial.id,
                step=step.name,
                verdict=result.verdict,
                duration=round(result.duration, 3),
                host=result.host,
                log=result.log,
            )
            if not result.passed:
                trial.verdict, trial.failed_step = "failed", step.name
                trial.reason = (
                    "train-step-timeout"
                    if result.verdict == "timeout"
                    else "train-step-failed"
                )
                return
        trial.verdict = "passed"

    def _step_env(
        self, base: str, head: str, branches: Sequence[str], **extra: str
    ) -> dict[str, str]:
        return {
            **self.environment,
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
        with self.git.worktree(self.head) as checkout:
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
            ctx = StepContext(
                checkout=checkout,
                base=self.start,
                head=self.head,
                branches=[c.branch for c in self.joined()],
                changed=self.git.changed(self.start, self.head),
                env=self._step_env(
                    self.start,
                    self.head,
                    [c.branch for c in self.joined()],
                    PICELI_TRAIN_TAG=tag,
                ),
                log_dir=self.state.run_dir(self.run_id),
                prefix="publish-",
                runner=self.runner,
                events=self.events,
            )
            for step in train.publish_steps:
                self.emit("step-started", trial=None, step=step.name, kind=step.kind)
                result = run_step(step, ctx)
                results.append(result)
                self.emit(
                    "published" if result.passed else "publish-failed",
                    step=step.name,
                    verdict=result.verdict,
                    duration=round(result.duration, 3),
                    log=result.log,
                )
                if not result.passed:
                    break
        return results

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
        self.events({"event": event, "at": now_iso(), "run": self.run_id, **fields})

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
        state = (
            "running"
            if self.finished is None
            else ("ejected" if self.ejected() else "joined")
        )
        return {
            "schema": RUN_SCHEMA,
            "state": state,
            "ok": self.ok() if self.finished is not None else None,
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
        }


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
