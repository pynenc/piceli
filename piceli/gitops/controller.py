"""The GitOps controller loop behind ``piceli gitops run``.

One poll (:meth:`Controller.poll_once`):

1. ``git ls-remote`` the repository (no webhook; a failure is recorded and
   the next poll tries again).
2. Handle requests: an approval of a pending plan hash, a promotion
   ``branch@sha`` to main or to a named environment.
3. Work out what should run: every branch matching the globs (except main
   and named environments) at its head; main only at the commit of a
   **new** tag matching ``v*`` or of a promotion (an untagged push to main
   does nothing); each named environment by its own rule (every push to a
   followed branch, a new matching tag, a promotion); a branch that
   disappeared is torn down. Tags present at the first poll are the baseline
   and deploy nothing. A branch environment without a push for
   ``idle_stop_seconds`` is scaled to zero (``stopped``) until its next push.
4. Work the queue **one item at a time**: tear-downs first, then deploys
   oldest push first. A deploy checks out the commit, loads its pipeline,
   builds (or uses pushed images), then ``env_up``: with the pipeline's
   ``auto_approve`` policy a branch applies when its plan is inside it;
   otherwise (and always for main unless the owner enabled
   ``main_auto_approve``) the step stops at ``approval-required`` with the
   plan hash, until ``piceli gitops approve ENV HASH``.
5. A failing step is retried with exponential backoff up to
   ``max_attempts``, then left ``failed`` until the next push. No exception
   escapes a step: one bad branch never stops the others.

Everything recorded is a commit id, a hash, a state or a registered error
code; never Git output, process output or secret values.

Importing this module is side-effect free.
"""

from __future__ import annotations

import fnmatch
import importlib.metadata
import re
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from piceli.gitops import GitOpsError
from piceli.gitops.config import ControllerConfig, backoff
from piceli.gitops.ports import APPROVE_POLICY, EnvOutcome, Ports
from piceli.gitops.repo import RemoteRefs
from piceli.gitops.state import (
    STATUS_SCHEMA,
    Channel,
    load_state,
    remember_rejected,
    save_state,
    write_json,
)

_SLUG = re.compile(r"[^a-z0-9]+")
_CODE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)+")


class Source(Protocol):
    """The Git side (:class:`piceli.gitops.repo.GitRemote`)."""

    def ls_remote(self) -> RemoteRefs: ...

    def fetch(self) -> None: ...

    def checkout(self, commit: str, parent: Path) -> Any: ...


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def slug(branch: str) -> str:
    """A cache key / file name for a branch (not the namespace: WP envs own that)."""
    return _SLUG.sub("-", branch.lower()).strip("-")[:48] or "branch"


def error_code(error: BaseException) -> str:
    """The error's registered code, else ``gitops-step-failed``."""
    code = getattr(error, "code", None)
    if isinstance(code, str) and _CODE.fullmatch(code):
        return code
    return "gitops-step-failed"


#: Codes of an approved hash that no longer matches the plan (a partial
#: apply, a drift): the controller plans again instead of retrying it.
STALE_APPROVAL_CODES = frozenset({"env-plan-changed", "pipeline-plan-changed"})


def replan_stale(
    record: dict[str, Any],
    env_up: Callable[[str | None], Any],
    *,
    policy: bool,
    log: Callable[[str], None] = lambda _: None,
) -> Any:
    """``env_up`` with the record's approval; a stale approval is planned again.

    The approval is the record's ``approved_hash``, else :data:`APPROVE_POLICY`
    when ``policy`` covers the environment, else ``None`` (plan only). When an
    approved hash no longer matches the plan (:data:`STALE_APPROVAL_CODES`,
    after a partial apply), the approval is dropped and the environment is
    planned again: the policy applies it when it covers the new plan, or the
    result asks for a new approval with the new hash.
    """
    fallback = APPROVE_POLICY if policy else None
    approved: str | None = record.get("approved_hash")
    if approved is None:
        return env_up(fallback)
    try:
        return env_up(approved)
    except Exception as error:
        if error_code(error) not in STALE_APPROVAL_CODES:
            raise
        record["approved_hash"] = None
        log(
            f"{record['branch']}: the approved plan changed ({error_code(error)}); "
            "planning again"
        )
        return env_up(fallback)


def _version() -> str:
    try:
        return importlib.metadata.version("piceli")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover
        return "unknown"


class Controller:
    """Poll a repository and keep branch environments at their commits.

    :param config: What to watch (:class:`ControllerConfig`).
    :param state_dir: The controller's private state (its volume).
    :param source: The Git remote.
    :param ports: Pipeline loading, builds and environments.
    :param channel: Where status is published and requests arrive.
    :param clock: Seconds since the epoch (tests pass a fake).
    :param log: Human progress lines (stderr in the CLI).
    """

    def __init__(
        self,
        config: ControllerConfig,
        *,
        state_dir: Path,
        source: Source,
        ports: Ports,
        channel: Channel,
        clock: Callable[[], float] = time.time,
        log: Callable[[str], None] = lambda _: None,
    ) -> None:
        self.config = config
        self.state_dir = state_dir
        self.source = source
        self.ports = ports
        self.channel = channel
        self.clock = clock
        self.log = log
        self.state = load_state(state_dir)
        self.busy = False  # one step at a time (asserted by the tests)

    # ------------------------------------------------------------ helpers
    def _envs(self) -> dict[str, dict[str, Any]]:
        envs: dict[str, dict[str, Any]] = self.state["envs"]
        return envs

    def _want(self, branch: str, commit: str, trigger: str) -> None:
        """Ask for ``branch`` at ``commit`` (a new push, tag or promotion)."""
        now = self.clock()
        record = self._envs().get(branch)
        if (
            record is not None
            and record.get("commit") == commit
            and (record.get("state") != "deleting")
        ):
            return
        self._envs()[branch] = {
            "branch": branch,
            "commit": commit,
            "deployed_commit": (record or {}).get("deployed_commit"),
            "namespace": (record or {}).get("namespace"),
            "state": "pending",
            "trigger": trigger,
            "attempts": 0,
            "next_attempt_at": None,
            "plan_hash": None,
            "approved_hash": None,
            "reason": None,
            "pushed_at": _iso(now),
            "updated_at": _iso(now),
        }
        self.log(f"{branch}: {trigger} -> {commit[:12]}")

    def _set(self, record: dict[str, Any], **values: Any) -> None:
        record.update(values)
        record["updated_at"] = _iso(self.clock())

    def _fail(self, record: dict[str, Any], reason: str) -> None:
        attempts = int(record.get("attempts") or 0) + 1
        deleting = record.get("state") == "deleting"
        if attempts >= self.config.max_attempts and not deleting:
            self._set(
                record,
                state="failed",
                attempts=attempts,
                reason=reason,
                next_attempt_at=None,
            )
            self.log(f"{record['branch']}: failed ({reason}); waiting for a new push")
            return
        # A teardown is never given up: it backs off (capped) and retries.
        delay = backoff(self.config, attempts)
        self._set(
            record,
            state="deleting" if deleting else "retrying",
            attempts=attempts,
            reason=reason,
            next_attempt_at=self.clock() + delay,
        )
        self.log(f"{record['branch']}: {reason}; retry in {delay}s")

    # ------------------------------------------------------------ requests
    def _requests(self, refs: RemoteRefs) -> None:
        try:
            pending = self.channel.requests()
        except GitOpsError as error:
            self.state["last_error"] = error.code
            return
        handled: list[str] = []
        for key, body in sorted(pending.items()):
            kind = body.get("kind")
            handled.append(key)
            if kind == "approve":
                self._approve(key, body)
            elif kind == "promote":
                self._promote(key, body, refs)
            elif kind == "sync":
                self._sync(key, body)
            else:
                self._reject(
                    key,
                    body,
                    GitOpsError("gitops-request-invalid", "unknown request kind"),
                )
        if handled:
            try:
                self.channel.remove_requests(handled)
            except GitOpsError as error:
                self.state["last_error"] = error.code

    def _reject(self, key: str, body: Mapping[str, Any], error: GitOpsError) -> None:
        remember_rejected(
            self.state,
            {
                "request": key,
                "kind": str(body.get("kind")),
                "reason": error.code,
                "message": str(error),
                "at": _iso(self.clock()),
            },
        )
        self.log(f"request {key}: {error} ({error.code})")

    def _approve(self, key: str, body: Mapping[str, Any]) -> None:
        record = self._envs().get(str(body.get("env")))
        if (
            record is None
            or record.get("state") != "approval-required"
            or record.get("plan_hash") != body.get("plan_hash")
        ):
            # The plan moved on (new push, re-plan) or the env is not waiting.
            self._reject(
                key,
                body,
                GitOpsError(
                    "gitops-approval-stale",
                    "the environment is not waiting for that plan hash",
                ),
            )
            return
        self._set(
            record,
            state="pending",
            approved_hash=body["plan_hash"],
            attempts=0,
            next_attempt_at=None,
            reason=None,
        )

    def _sync(self, key: str, body: Mapping[str, Any]) -> None:
        """Deploy an environment again at its commit (``piceli gitops sync``)."""
        if body.get("component") is not None:
            self._reject(
                key,
                body,
                GitOpsError(
                    "gitops-request-invalid",
                    "--component needs a composition controller (gitops enable infra.py)",
                ),
            )
            return
        env = body.get("env")
        records = self._envs()
        names = sorted(records) if env is None else [str(env)]
        if env is not None and str(env) not in records:
            self._reject(
                key,
                body,
                GitOpsError(
                    "gitops-request-invalid", "sync names no known environment"
                ),
            )
            return
        for name in names:
            record = records[name]
            if record.get("state") == "deleting" or not record.get("commit"):
                continue
            self._set(
                record,
                state="pending",
                trigger="sync",
                attempts=0,
                next_attempt_at=None,
                reason=None,
            )

    def _promote(self, key: str, body: Mapping[str, Any], refs: RemoteRefs) -> None:
        branch, commit = str(body.get("branch")), str(body.get("commit"))
        target = str(body.get("env") or self.config.main_branch)
        rule = self.config.rule(target)
        if body.get("env") and (rule is None or not rule.promote):
            self._reject(
                key,
                body,
                GitOpsError(
                    "gitops-promote-not-allowed",
                    "the environment is unknown or does not follow Promote()",
                ),
            )
            return
        known = {refs.branches.get(branch)}
        record = self._envs().get(branch)
        if record is not None:
            known |= {record.get("commit"), record.get("deployed_commit")}
        full = next(
            (sha for sha in known if isinstance(sha, str) and sha.startswith(commit)),
            None,
        )
        if rule is None or not rule.promote or full is None:
            # Only a commit the controller saw on that branch can be promoted.
            self._reject(
                key,
                body,
                GitOpsError(
                    "gitops-promote-unknown",
                    "the commit is not the branch's head or deployed commit, or "
                    "the main branch is not watched",
                ),
            )
            return
        self._want(target, full, f"promote {branch}@{full[:12]}")

    # ------------------------------------------------------------ desired
    def _desired(self, refs: RemoteRefs) -> None:
        main = self.config.main_branch
        fixed = self.config.fixed
        for branch, commit in sorted(refs.branches.items()):
            if not fixed(branch) and self.config.watches(branch):
                self._want(branch, commit, "push")
        tags = {
            name: sha
            for name, sha in refs.tags.items()
            if fnmatch.fnmatchcase(name, self.config.tags)
        }
        seen: dict[str, str] = self.state["tags"]
        new = sorted(
            (name for name in tags if seen.get(name) != tags[name]),
            key=_version_key,
        )
        implicit = self.config.rule(main)
        if (
            self.state.get("baseline")
            and new
            and implicit is not None
            and implicit not in self.config.environments
        ):
            latest = new[-1]
            self._want(main, tags[latest], f"tag {latest}")
        self.state["tags"] = dict(sorted(tags.items()))
        self.state["baseline"] = True
        for rule in self.config.environments:
            self._follow(rule, refs)
        for branch, record in self._envs().items():
            if fixed(branch) or record.get("state") == "deleting":
                continue
            if branch not in refs.branches or not self.config.watches(branch):
                self._set(
                    record,
                    state="deleting",
                    attempts=0,
                    next_attempt_at=None,
                    reason=None,
                )
                self.log(f"{branch}: branch gone; tearing its environment down")

    def _follow(self, rule: Any, refs: RemoteRefs) -> None:
        """A named environment's own trigger: followed branches, then new tags."""
        heads: dict[str, dict[str, str]] = self.state.setdefault("env_heads", {})
        known = heads.setdefault(rule.name, {})
        for branch in rule.branches:
            commit = refs.branches.get(branch)
            if commit is not None and known.get(branch) != commit:
                # Every push to a followed branch (the first one seen too).
                self._want(rule.name, commit, f"push {branch}")
                known[branch] = commit
        if not rule.tags:
            return
        all_tags: dict[str, dict[str, str]] = self.state.setdefault("env_tags", {})
        tags = {
            name: sha
            for name, sha in refs.tags.items()
            if any(fnmatch.fnmatchcase(name, glob) for glob in rule.tags)
        }
        seen = all_tags.get(rule.name)
        if seen is not None:  # the first poll of an environment is its baseline
            new = sorted(
                (name for name in tags if seen.get(name) != tags[name]),
                key=_version_key,
            )
            if new:
                self._want(rule.name, tags[new[-1]], f"tag {new[-1]}")
        all_tags[rule.name] = dict(sorted(tags.items()))

    def _idle(self) -> list[dict[str, Any]]:
        """Branch environments without a push for ``idle_stop_seconds``."""
        limit = self.config.idle_stop_seconds
        if limit is None:
            return []
        now = self.clock()
        found = []
        for branch, record in self._envs().items():
            if self.config.fixed(branch) or record.get("state") != "deployed":
                continue
            pushed = _seconds(record.get("pushed_at"))
            at = record.get("next_attempt_at")
            if pushed is None or now - pushed < limit or (at is not None and at > now):
                continue
            found.append(record)
        return sorted(found, key=lambda r: r["branch"])

    def _due(self) -> list[dict[str, Any]]:
        now = self.clock()

        def ready(record: dict[str, Any]) -> bool:
            if record.get("state") not in {"pending", "retrying", "deleting"}:
                return False
            at = record.get("next_attempt_at")
            return at is None or at <= now

        records = [r for r in self._envs().values() if ready(r)]
        return sorted(
            records,
            key=lambda r: (
                r["state"] != "deleting",
                r.get("pushed_at") or "",
                r["branch"],
            ),
        )

    # ------------------------------------------------------------ steps
    def _deploy(self, record: dict[str, Any]) -> None:
        branch, commit = record["branch"], record["commit"]
        rule = self.config.rule(branch)
        fixed = self.config.fixed(branch)
        work = self.state_dir / "work"
        with self.source.checkout(commit, work) as tree:
            pipeline = self.ports.load_pipeline(
                tree, self.config.pipeline, self.config.env
            )
            namespace = self.ports.prepare_env(pipeline, branch)
            if namespace:
                record["namespace"] = namespace
            # A digest pushed from a laptop (`piceli env push`) for exactly
            # this commit wins over a build.
            pushed = (
                self.ports.pushed_images(pipeline, branch, namespace)
                if namespace
                else None
            )
            digests: Mapping[str, Any] | None = None
            receipt: Mapping[str, Any] | None = None
            if pushed and pushed.get("commit") == commit and pushed.get("images"):
                digests = dict(pushed["images"])
            else:
                receipt = self._receipt(pipeline, branch, commit, tree)
            policy = getattr(pipeline, "auto_approve", None) is not None and (
                not fixed or (rule is not None and rule.auto_approve)
            )
            outcome = EnvOutcome.from_result(
                replan_stale(
                    record,
                    lambda approve: self.ports.env_up(
                        pipeline,
                        branch,
                        commit=commit,
                        receipt=receipt,
                        digests=digests,
                        approve=approve,
                    ),
                    policy=policy,
                    log=self.log,
                )
            )
        self._outcome(record, outcome)

    def _receipt(
        self, pipeline: Any, branch: str, commit: str, tree: Path
    ) -> Mapping[str, Any]:
        """The build receipt of ``commit``: kept on the volume, built once."""
        path = self.state_dir / "receipts" / f"{slug(branch)}-{commit}.json"
        from piceli.gitops.state import read_json

        kept = read_json(path)
        if isinstance(kept, dict):
            return kept
        # The same commit built for another environment (main and rc).
        for other in sorted(path.parent.glob(f"*-{commit}.json")):
            kept = read_json(other)
            if isinstance(kept, dict):
                return kept
        receipt = dict(
            self.ports.build(
                pipeline,
                commit,
                cache_key=branch,
                platforms=self.config.platforms,
                checkout=tree,
            )
        )
        write_json(path, receipt)
        return receipt

    def _outcome(self, record: dict[str, Any], outcome: EnvOutcome) -> None:
        if outcome.namespace:
            record["namespace"] = outcome.namespace
        if outcome.state == "deployed":
            self._set(
                record,
                state="deployed",
                deployed_commit=record["commit"],
                attempts=0,
                next_attempt_at=None,
                plan_hash=None,
                approved_hash=None,
                reason=None,
            )
            self.log(f"{record['branch']}: deployed {record['commit'][:12]}")
        elif outcome.state == "approval-required":
            self._set(
                record,
                state="approval-required",
                plan_hash=outcome.plan_hash,
                approved_hash=None,
                next_attempt_at=None,
                reason=outcome.reason,
            )
            self.log(
                f"{record['branch']}: approval required; piceli gitops approve "
                f"{record['branch']} {outcome.plan_hash}"
            )
        else:
            self._fail(record, outcome.reason or "gitops-step-failed")

    def _teardown(self, record: dict[str, Any], refs: RemoteRefs) -> None:
        branch = record["branch"]
        # The environment settings live in the pipeline: load it from main's
        # head (the deleted branch's commit may be gone from the remote).
        commit = refs.branches.get(self.config.main_branch) or record.get(
            "deployed_commit"
        )
        if not commit:
            raise GitOpsError(
                "gitops-step-failed", "no commit to load the pipeline from"
            )
        with self.source.checkout(commit, self.state_dir / "work") as tree:
            pipeline = self.ports.load_pipeline(
                tree, self.config.pipeline, self.config.env
            )
            self.ports.env_down(pipeline, branch)
        del self._envs()[branch]
        self.log(f"{branch}: environment removed")

    def _stop(self, record: dict[str, Any], refs: RemoteRefs) -> None:
        """Scale an idle branch environment to zero (the next push starts it)."""
        branch = record["branch"]
        commit = str(refs.branches.get(branch) or record.get("deployed_commit"))
        try:
            with self.source.checkout(commit, self.state_dir / "work") as tree:
                pipeline = self.ports.load_pipeline(
                    tree, self.config.pipeline, self.config.env
                )
                self.ports.env_stop(pipeline, branch)
        except Exception as error:  # retried on a later poll
            delay = backoff(self.config, int(record.get("attempts") or 0) + 1)
            self._set(
                record,
                reason=error_code(error),
                attempts=int(record.get("attempts") or 0) + 1,
                next_attempt_at=self.clock() + delay,
            )
            self.log(f"{branch}: idle stop failed ({record['reason']})")
            return
        self._set(
            record,
            state="stopped",
            reason="idle-stop",
            attempts=0,
            next_attempt_at=None,
            stopped_at=_iso(self.clock()),
        )
        self.log(f"{branch}: no push for a while; scaled to zero")

    def _step(self, record: dict[str, Any], refs: RemoteRefs) -> None:
        if self.busy:  # pragma: no cover - guarded by the tests
            raise RuntimeError("controller steps must not overlap")
        self.busy = True
        try:
            if record["state"] == "deleting":
                self._teardown(record, refs)
            else:
                self._deploy(record)
        except Exception as error:  # one bad branch never stops the loop
            self._fail(record, error_code(error))
        finally:
            self.busy = False
            save_state(self.state_dir, self.state)

    # ------------------------------------------------------------ poll
    def poll_once(self) -> dict[str, Any]:
        """One poll and the work it found; returns the published status."""
        now = self.clock()
        self.state["last_poll"] = _iso(now)
        try:
            refs = self.source.ls_remote()
        except GitOpsError as error:
            self.state["last_error"] = error.code
            self.state["poll_failures"] = int(self.state.get("poll_failures") or 0) + 1
            self.log(
                f"poll failed ({error.code}); next poll in {self.config.poll_seconds}s"
            )
            return self._publish()
        self.state["last_error"] = None
        self.state["poll_failures"] = 0
        self._requests(refs)
        self._desired(refs)
        save_state(self.state_dir, self.state)
        due = self._due()
        idle = self._idle()
        if due or idle:
            try:
                self.source.fetch()
            except GitOpsError as error:
                self.state["last_error"] = error.code
                return self._publish()
            for record in due:
                self._step(record, refs)
            for record in idle:
                self._step_stop(record, refs)
        return self._publish()

    def _step_stop(self, record: dict[str, Any], refs: RemoteRefs) -> None:
        if self.busy:  # pragma: no cover - guarded by the tests
            raise RuntimeError("controller steps must not overlap")
        self.busy = True
        try:
            self._stop(record, refs)
        finally:
            self.busy = False
            save_state(self.state_dir, self.state)

    def status(self) -> dict[str, Any]:
        """The published status document (``piceli.gitops-status.v1``)."""
        envs = {
            branch: {
                key: value for key, value in record.items() if key != "approved_hash"
            }
            for branch, record in sorted(self._envs().items())
        }
        failures = int(self.state.get("poll_failures") or 0)
        return {
            "schema": STATUS_SCHEMA,
            "controller": {
                "state": "degraded" if self.state.get("last_error") else "running",
                "version": _version(),
                "repo": self.config.repo,
                "branches": list(self.config.branches),
                "main_branch": self.config.main_branch,
                "tags": self.config.tags,
                "pipeline": self.config.pipeline,
                "poll_seconds": self.config.poll_seconds,
                "last_poll": self.state.get("last_poll"),
                "last_error": self.state.get("last_error"),
                "poll_failures": failures,
                **(
                    {
                        "environments": [
                            rule.to_dict() for rule in self.config.environments
                        ]
                    }
                    if self.config.environments
                    else {}
                ),
                **(
                    {"idle_stop_seconds": self.config.idle_stop_seconds}
                    if self.config.idle_stop_seconds is not None
                    else {}
                ),
            },
            "envs": envs,
            "rejected_requests": list(self.state.get("rejected") or []),
        }

    def _publish(self) -> dict[str, Any]:
        save_state(self.state_dir, self.state)
        status = self.status()
        try:
            self.channel.publish(status)
        except GitOpsError as error:
            self.log(f"status not published ({error.code})")
        return status

    def run_forever(
        self,
        *,
        stop: Callable[[], bool] = lambda: False,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Poll every ``poll_seconds`` until ``stop()``; never raises on a step."""
        while not stop():
            started = self.clock()
            self.poll_once()
            sleep(max(1.0, self.config.poll_seconds - (self.clock() - started)))


def _version_key(tag: str) -> tuple[Any, ...]:
    """``v1.10.0`` after ``v1.9.0``: numbers compare as numbers."""
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in re.split(r"(\d+)", tag)
        if part
    )


def _seconds(value: Any) -> float | None:
    """Seconds since the epoch of an ``_iso`` time, or ``None``."""
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
