"""Release candidates (0.83.0): the images of a passed commit, copied out.

``Controller(candidate=Candidate(environment="staging", copies=(...)))``
makes the GitOps controller prepare a candidate for the newest commit the
environment passed (its deploy run finished with every check green, the
record of passed commits, :func:`piceli.infra.promotion.remember`) once no
newer commit passed for a quiet time. A candidate copies the images of that
run, by digest, to the outside registry paths the composition declares
(:class:`CandidateCopy`), with the resumable copy set
(:mod:`piceli.artifacts.copy_set`).

State of a candidate, ``state["candidates"][commit]``:

``waiting``
    the commit passed; the quiet time is not over;
``building``
    its images are being copied (a worker thread; retried with a backoff on
    the polls after a transient failure);
``ready``
    every image is copied;
``failed``
    ``reason`` says why (``candidate-no-image``, ``candidate-run-missing``,
    a permanent reason of the copy set, or the last transient one after
    :data:`MAX_ATTEMPTS` tries);
``cancelled``
    a newer commit passed while it was building
    (``candidate-superseded``); a request resumes it from its ledger.

``piceli gitops candidate request COMMIT`` starts one at once, ``release``
marks a ready one released, ``list`` shows them. The newest ``keep``
candidates and every released one stay; :func:`protected_digests` names
their digests, for the registry cleanup (``piceli artifacts retention
--pin-file``).

Importing this module is side-effect free.
"""

from __future__ import annotations

import re
import string
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: State key of the candidates (``{commit: record}``).
STATE_KEY = "candidates"
#: State key of the newest passed commit the candidates last saw.
NEWEST_KEY = "candidate_newest"
#: Where an install mounts the Secrets named by ``CandidateCopy(credentials=)``.
CREDENTIALS_DIR = "/etc/piceli/candidate-credentials"
#: The key of such a Secret: ``{"username": ..., "password": ...}`` or ``{"token": ...}``.
CREDENTIALS_KEY = "credentials.json"
#: Candidate states.
STATES = ("waiting", "building", "ready", "failed", "cancelled")
#: Tries of a candidate (each is a whole copy set with its own retries).
MAX_ATTEMPTS = 5
#: Seconds to wait before the next try, by attempt.
BACKOFF = (60, 300, 900, 1800, 3600)
#: A commit is named by this many characters in a tag (``{commit}``).
COMMIT_LENGTH = 12
SUPERSEDED = "candidate-superseded"
NO_IMAGE = "candidate-no-image"
NO_COMPONENT = "candidate-no-component"
RUN_MISSING = "candidate-run-missing"
COPY_ERROR = "candidate-copy-error"
CREDENTIALS_MISSING = "candidate-credentials-missing"
_TRANSIENT = ("unreachable", "timeout", "timed-out", "transient", "temporar")
_LABEL = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")
_TARGET = re.compile(
    r"oci://[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]{1,5})?"
    r"(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)+"
)
_TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
_COMMIT = re.compile(r"[0-9a-f]{7,40}")
_SECRET = re.compile(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?")


def _invalid(message: str) -> Exception:
    from piceli.infra.cluster import ClusterError

    return ClusterError("cluster-invalid", message)


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(text: Any) -> float | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# ---------------------------------------------------------------- declaration


@dataclass(frozen=True)
class CandidateCopy:
    """One image of the passed run, copied out.

    :param component: The component whose image of the run is copied.
    :param to: Where it goes, ``oci://host/repo`` (no tag, no digest).
    :param tag: Tag at the target (empty: none, the digest only); may hold
        ``{commit}``, the commit's first 12 characters.
    :param credentials: A Secret in the controller's namespace with the key
        ``credentials.json`` (``{"username": ..., "password": ...}`` or
        ``{"token": ...}``) for the target registry (none: anonymous).
    """

    component: str
    to: str
    tag: str = ""
    credentials: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.component, str) or not _LABEL.fullmatch(self.component):
            raise _invalid("CandidateCopy(component=) is a component's name")
        if not isinstance(self.to, str) or not _TARGET.fullmatch(self.to):
            raise _invalid(
                "CandidateCopy(to=) is oci://host/repository, with no tag or digest"
            )
        if not isinstance(self.tag, str):
            raise _invalid("CandidateCopy(tag=) is a text, may hold {commit}")
        try:
            fields = [name for _, name, _, _ in string.Formatter().parse(self.tag)]
            shown = self.tag.format(commit="0" * COMMIT_LENGTH) if self.tag else ""
        except (ValueError, IndexError, KeyError):
            raise _invalid("CandidateCopy(tag=) may only hold {commit}") from None
        if any(name not in (None, "commit") for name in fields) or (
            shown and not _TAG.fullmatch(shown)
        ):
            raise _invalid("CandidateCopy(tag=) must be a tag, it may hold {commit}")
        if self.credentials is not None and not (
            isinstance(self.credentials, str) and _SECRET.fullmatch(self.credentials)
        ):
            raise _invalid("CandidateCopy(credentials=) must name a Secret")

    def target(self, commit: str) -> str:
        """The path this copy writes: ``to`` with the tag, if any."""
        tag = self.tag.format(commit=commit[:COMMIT_LENGTH]) if self.tag else ""
        return f"{self.to}:{tag}" if tag else self.to

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "to": self.to,
            "tag": self.tag,
            "credentials": self.credentials,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CandidateCopy:
        if not isinstance(value, Mapping):
            raise _invalid("a candidate copy is CandidateCopy(...)")
        return cls(
            component=str(value.get("component") or ""),
            to=str(value.get("to") or ""),
            tag=str(value.get("tag") or ""),
            credentials=value.get("credentials") or None,
        )


@dataclass(frozen=True)
class Candidate:
    """A release candidate of an environment (0.83.0).

    :param environment: The environment whose passed commits are candidates.
        An environment with several sources uses its first source's commit.
    :param quiet: How long no newer commit may have passed before the
        candidate is prepared (``30m``, ``1h``).
    :param keep: How many of the newest candidates stay (released ones always
        stay).
    :param copies: The images to copy out, one per component and target.
    """

    environment: str
    quiet: str = "30m"
    keep: int = 5
    copies: tuple[CandidateCopy, ...] = ()

    def __post_init__(self) -> None:
        from piceli.gitops import GitOpsError
        from piceli.gitops.config import parse_duration

        if not isinstance(self.environment, str) or not _LABEL.fullmatch(
            self.environment
        ):
            raise _invalid("Candidate(environment=) is an environment's name")
        try:
            seconds = parse_duration(self.quiet)
        except (GitOpsError, TypeError):
            raise _invalid("Candidate(quiet=) must be like 30s, 30m or 1h") from None
        if seconds < 0:
            raise _invalid("Candidate(quiet=) cannot be negative")
        if (
            isinstance(self.keep, bool)
            or not isinstance(self.keep, int)
            or self.keep < 1
        ):
            raise _invalid("Candidate(keep=) is a number of candidates, 1 or more")
        if isinstance(self.copies, CandidateCopy) or not isinstance(
            self.copies, Sequence
        ):
            raise _invalid("Candidate(copies=) is a list of CandidateCopy")
        copies = tuple(self.copies)
        if not all(isinstance(item, CandidateCopy) for item in copies):
            raise _invalid("Candidate(copies=) is a list of CandidateCopy")
        object.__setattr__(self, "copies", copies)
        for what, values in (
            ("component", [item.component for item in copies]),
            ("target", [item.to for item in copies]),
        ):
            if len(set(values)) != len(values):
                raise _invalid(f"Candidate(copies=) names a {what} twice")

    @property
    def quiet_seconds(self) -> int:
        from piceli.gitops.config import parse_duration

        return parse_duration(self.quiet)

    @property
    def secrets(self) -> tuple[str, ...]:
        """The Secrets the copies name for their credentials."""
        return tuple(
            sorted({item.credentials for item in self.copies if item.credentials})
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "environment": self.environment,
            "quiet": self.quiet,
            "keep": self.keep,
            "copies": [item.to_dict() for item in self.copies],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Candidate:
        if not isinstance(value, Mapping):
            raise _invalid("candidate is Candidate(...)")
        return cls(
            environment=str(value.get("environment") or ""),
            quiet=str(value.get("quiet") or "30m"),
            keep=value.get("keep", 5),
            copies=tuple(
                CandidateCopy.from_dict(item) for item in value.get("copies") or ()
            ),
        )


def ledger_name(commit: str, component: str) -> str:
    """The copy set's ledger of one copy of a candidate (its name is at most
    62 characters of ``a-z0-9.-``): the same name resumes the copy."""
    name = f"candidate-{commit[:COMMIT_LENGTH]}-{component}"
    if len(name) > 62:
        import hashlib

        name = (
            f"candidate-{commit[:COMMIT_LENGTH]}-"
            + hashlib.sha256(component.encode()).hexdigest()[:20]
        )
    return name


def credentials_path(secret: str) -> Path:
    """Where the install mounts the Secret ``secret`` in the controller."""
    return Path(CREDENTIALS_DIR) / secret / CREDENTIALS_KEY


# ---------------------------------------------------------------- the state


def protected_digests(state: Mapping[str, Any]) -> frozenset[str]:
    """The image digests the candidates in ``state`` need: pin them in the
    registry cleanup (``piceli artifacts retention --pin-file``, one per
    line) so a candidate's source images outlive their runs."""
    found: set[str] = set()
    for record in (state.get(STATE_KEY) or {}).values():
        if isinstance(record, Mapping):
            found.update(str(item) for item in record.get("digests") or ())
    return frozenset(found)


class _Refused(Exception):
    """A copy that cannot start; ``code`` is the candidate's reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _transient(reason: str) -> bool:
    return any(part in reason for part in _TRANSIENT)


class _Job:
    """One running copy of a candidate (a worker thread's results)."""

    def __init__(self) -> None:
        self.cancel = threading.Event()
        self.done = False
        self.lock = threading.Lock()
        self.results: dict[str, tuple[bool, str | None]] = {}

    def set(self, component: str, ok: bool, reason: str | None = None) -> None:
        with self.lock:
            self.results[component] = (ok, reason)

    def snapshot(self) -> dict[str, tuple[bool, str | None]]:
        with self.lock:
            return dict(self.results)


def _spawn(function: Callable[[], None]) -> None:
    threading.Thread(target=function, daemon=True, name="piceli-candidate").start()


class Candidates:
    """The controller's candidates: their records in its state, the quiet
    timer, the copies (in a worker thread, between items cancellable), the
    requests and the status.

    :param state: The controller's state (``state["candidates"]``).
    :param candidate: The declaration.
    :param clock: Seconds since the epoch (fake in tests).
    :param artifacts: ``artifacts(document, run_id, env)`` of
        :mod:`piceli.gitops.releases` (default).
    :param copy: ``copy(item, secret, ledger)`` (``item``: ``source``,
        ``target``, ``component``, ``digest``; ``secret``: the target's
        credentials Secret or ``None``) runs one resumable copy set and
        returns its report (``.ok``, ``.failed``); default: the copy set of
        :mod:`piceli.artifacts.copy_set` with a ledger in ``state_dir``.
    :param spawn: Starts a function in the background (inline in tests).
    :param registry_host: The cluster registry's service address, for a
        run's image the history gives without a host.
    :param state_dir: Where the copy set's ledgers live (``candidates/ledgers``).
    :param sources: The environment's source keys; the first names the
        candidate's commit (:meth:`poll` may set them again).
    """

    def __init__(
        self,
        state: dict[str, Any],
        candidate: Candidate,
        *,
        clock: Callable[[], float],
        log: Callable[[str], None] = lambda _m: None,
        artifacts: Callable[..., Any] | None = None,
        copy: Callable[..., Any] | None = None,
        spawn: Callable[[Callable[[], None]], Any] = _spawn,
        registry_host: str | None = None,
        state_dir: Path | None = None,
        sources: Sequence[str] = (),
    ) -> None:
        self.state = state
        self._sources = tuple(sources)
        self.candidate = candidate
        self.clock = clock
        self.log = log
        self._artifacts = artifacts
        self._copy = copy
        self._spawn = spawn
        self.registry_host = registry_host
        self.state_dir = state_dir
        self._jobs: dict[str, _Job] = {}

    # ------------------------------------------------------------ records
    @property
    def records(self) -> dict[str, dict[str, Any]]:
        found = self.state.setdefault(STATE_KEY, {})
        for commit in [
            key for key, item in found.items() if not isinstance(item, dict)
        ]:
            del found[commit]
        return found

    def resolve(self, commit: str, *, among: Sequence[str]) -> str:
        """The full commit ``commit`` (7 to 40 hex characters) names in ``among``.

        :raises GitOpsError: ``gitops-request-invalid``."""
        from piceli.gitops import GitOpsError

        text = str(commit or "").strip().lower()
        if not _COMMIT.fullmatch(text):
            raise GitOpsError(
                "gitops-request-invalid", "a commit is 7 to 40 hexadecimal characters"
            )
        found = sorted({item for item in among if item.startswith(text)})
        if len(found) > 1:
            raise GitOpsError(
                "gitops-request-invalid", f"commit {text} names several candidates"
            )
        if not found:
            raise GitOpsError(
                "gitops-request-invalid",
                f"commit {text} did not pass in {self.candidate.environment}: "
                "no candidate and no passed run",
            )
        return found[0]

    def _passed(
        self, passed: Mapping[str, Any] | None, *, every: bool = False
    ) -> dict[str, dict[str, Any]]:
        """``{commit: entry}`` of the passed commits of the environment's
        first source (``every``: of all its sources, to find a commit)."""
        record = (passed or {}).get(self.candidate.environment) or {}
        sources = sorted(record) if every else (self._sources or sorted(record))[:1]
        found: dict[str, dict[str, Any]] = {}
        for source in sources:
            for commit, entry in (record.get(source) or {}).items():
                if isinstance(entry, Mapping):
                    found[str(commit)] = dict(entry)
        return found

    @staticmethod
    def _newest(commits: Mapping[str, Mapping[str, Any]]) -> str | None:
        if not commits:
            return None
        return max(
            commits, key=lambda key: (str(commits[key].get("last_at") or ""), key)
        )

    # ------------------------------------------------------------ the poll
    def poll(
        self,
        passed: Mapping[str, Any] | None,
        document: Any = None,
        *,
        sources: Sequence[str] = (),
    ) -> None:
        """One poll: note the newest passed commit, cancel what it
        supersedes, start what is due, take in the copies' results, prune.

        :param passed: The record of passed commits
            (``{env: {source: {commit: {last_run, last_at, ...}}}}``).
        :param document: The history document (the digests of a run).
        :param sources: The environment's source keys, the first one names
            the candidate's commit."""
        self._sources = tuple(sources)
        now = self.clock()
        commits = self._passed(passed)
        newest = self._newest(commits)
        records = self.records
        if newest is not None and newest != self.state.get(NEWEST_KEY):
            self._superseded(newest, now)
            self.state[NEWEST_KEY] = newest
            again = records.get(newest)
            if again is not None and again.get("state") == "cancelled":
                # The commit passed again after a newer one: a new quiet time.
                at = _epoch(commits[newest].get("last_at"))
                start = now if at is None else min(at, now)
                again.update(
                    state="waiting",
                    since=_iso(now),
                    passed_at=_iso(start),
                    quiet_until=_iso(start + self.candidate.quiet_seconds),
                    run=str(commits[newest].get("last_run") or again.get("run") or ""),
                    reason=None,
                )
                again.pop("detail", None)
        if newest is not None and newest not in records:
            entry = commits[newest]
            at = _epoch(entry.get("last_at"))
            passed_at = now if at is None else min(at, now)
            records[newest] = {
                "state": "waiting",
                "since": _iso(now),
                "passed_at": _iso(passed_at),
                "run": str(entry.get("last_run") or ""),
                "quiet_until": _iso(passed_at + self.candidate.quiet_seconds),
                "copies": {},
                "reason": None,
                "released": False,
                "digests": [],
            }
            self.log(f"candidate {newest[:7]}: waiting for a quiet time")
        for commit, record in sorted(records.items()):
            self._advance(commit, record, document, now)
        self._prune()

    def _superseded(self, newest: str, now: float) -> None:
        for commit, record in list(self.records.items()):
            if commit == newest or record.get("requested"):
                continue
            if record.get("state") == "waiting":
                del self.records[commit]
            elif record.get("state") == "building":
                self._cancel(commit, record, newest)

    def _cancel(self, commit: str, record: dict[str, Any], newest: str) -> None:
        job = self._jobs.get(commit)
        if job is not None:
            job.cancel.set()
        record.update(
            state="cancelled",
            since=_iso(self.clock()),
            reason=SUPERSEDED,
            detail=f"commit {newest[:COMMIT_LENGTH]} passed",
            retry_at=None,
        )
        self.log(f"candidate {commit[:7]}: cancelled, {newest[:7]} passed")

    def _advance(
        self, commit: str, record: dict[str, Any], document: Any, now: float
    ) -> None:
        state = record.get("state")
        if state == "waiting":
            due = _epoch(record.get("quiet_until"))
            if due is None or now >= due:
                self._begin(commit, record, now)
        if record.get("state") != "building":
            return
        if record.get("plan") is None:
            if not self._plan(commit, record, document):
                return
        job = self._jobs.get(commit)
        if job is None or (job.cancel.is_set() and job.done):
            retry = _epoch(record.get("retry_at"))
            if retry is not None and now < retry:
                return
            self._start(commit, record)
            job = self._jobs.get(commit)
            if job is None or job.cancel.is_set():
                return
        self._absorb(commit, record, job, now)

    def _begin(self, commit: str, record: dict[str, Any], now: float) -> None:
        record.update(state="building", since=_iso(now), reason=None, retry_at=None)
        record.pop("detail", None)
        record["attempts"] = 0
        self.log(f"candidate {commit[:7]}: building")

    # ------------------------------------------------------------ the plan
    def _plan(self, commit: str, record: dict[str, Any], document: Any) -> bool:
        """The copies of the run (``record["plan"]``); ``False`` after it
        failed the candidate."""
        run = str(record.get("run") or "")
        found = self._artifact_reader()(document, run, self.candidate.environment)
        if not isinstance(found, Mapping):
            return self._fail(commit, record, RUN_MISSING, f"run {run or '?'} is gone")
        components = {
            str(item.get("name")): item
            for item in found.get("components") or ()
            if isinstance(item, Mapping)
        }
        plan: list[dict[str, str]] = []
        for copy in self.candidate.copies:
            item = components.get(copy.component)
            if item is None:
                return self._fail(
                    commit,
                    record,
                    NO_COMPONENT,
                    f"component {copy.component} is not in run {run}",
                )
            source = self._source(item)
            if source is None:
                return self._fail(
                    commit,
                    record,
                    NO_IMAGE,
                    f"component {copy.component} has no image reference",
                )
            plan.append(
                {
                    "component": copy.component,
                    "source": source,
                    "target": copy.target(commit),
                    "digest": str(item["digest"]),
                }
            )
        record["plan"] = plan
        record["digests"] = sorted({item["digest"] for item in plan})
        record["copies"] = {
            item["component"]: record.get("copies", {}).get(item["component"])
            or "pending"
            for item in plan
        }
        return True

    def _artifact_reader(self) -> Callable[..., Any]:
        if self._artifacts is not None:
            return self._artifacts
        from piceli.gitops import releases

        return releases.artifacts

    def _source(self, item: Mapping[str, Any]) -> str | None:
        """``oci://host/repo@digest`` of a run's component, else ``None``."""
        digest = str(item.get("digest") or "")
        image = item.get("image")
        if not isinstance(image, Mapping) or not digest.startswith("sha256:"):
            return None
        reference = str(image.get("reference") or "")
        if "@" in reference:
            return "oci://" + reference.removeprefix("oci://")
        repository = str(image.get("repository") or "")
        if not reference or not repository or not self.registry_host:
            return None
        return f"oci://{self.registry_host}/{repository}@{digest}"

    # ------------------------------------------------------------ copying
    def _start(self, commit: str, record: dict[str, Any]) -> None:
        old = self._jobs.get(commit)
        if old is not None and not old.done:
            return  # a cancelled worker is still on its last item
        job = _Job()
        self._jobs[commit] = job
        record["retry_at"] = None
        plan = [dict(item) for item in record.get("plan") or ()]
        todo = [
            item for item in plan if record["copies"].get(item["component"]) != "copied"
        ]
        credentials = {
            item.component: item.credentials for item in self.candidate.copies
        }

        def work() -> None:
            try:
                for item in todo:
                    if job.cancel.is_set():
                        return
                    name = item["component"]
                    try:
                        report = self._run_copy(
                            item, credentials.get(name), ledger_name(commit, name)
                        )
                    except Exception as error:  # a worker never dies silently
                        reason = getattr(error, "code", None) or COPY_ERROR
                        job.set(name, False, str(reason))
                        return
                    if getattr(report, "ok", False):
                        job.set(name, True)
                    else:
                        job.set(
                            name, False, str(getattr(report, "failed", None) or "?")
                        )
                        return
            finally:
                job.done = True

        self._spawn(work)

    def _run_copy(
        self, item: Mapping[str, str], secret: str | None, ledger: str
    ) -> Any:
        if self._copy is not None:
            return self._copy(item, secret, ledger)
        if self.state_dir is None:
            raise _Refused(COPY_ERROR, "no state directory for the copy ledgers")
        from piceli.artifacts.copy_set import (
            CopyItem,
            CopyOptions,
            FileLedger,
            prepare_copies,
            run_copy_set,
        )

        found = CopyItem(item["source"], item["target"])
        options = CopyOptions()
        return run_copy_set(
            [found],
            ledger=ledger,
            store=FileLedger(self.state_dir / STATE_KEY / "ledgers"),
            attempts=5,
            copy=prepare_copies(
                [found],
                options,
                target_credentials=_load_credentials(secret) if secret else None,
            ),
            options=options,
        )

    def _absorb(
        self, commit: str, record: dict[str, Any], job: _Job, now: float
    ) -> None:
        """Take a worker's results into the record; settle it when it is done."""
        for name, (ok, _) in job.snapshot().items():
            if name in record["copies"]:
                record["copies"][name] = "copied" if ok else "failed"
        if not job.done or job.cancel.is_set():
            return
        self._jobs.pop(commit, None)
        failures = [
            (name, reason) for name, (ok, reason) in job.snapshot().items() if not ok
        ]
        if not failures:
            if all(value == "copied" for value in record["copies"].values()):
                record.update(state="ready", since=_iso(now), reason=None)
                record.pop("detail", None)
                record["retry_at"] = None
                self.log(f"candidate {commit[:7]}: ready")
            return
        name, reason = failures[0]
        attempts = int(record.get("attempts") or 0) + 1
        record["attempts"] = attempts
        if _transient(str(reason)) and attempts < MAX_ATTEMPTS:
            wait = BACKOFF[min(attempts, len(BACKOFF)) - 1]
            record.update(reason=reason, detail=name, retry_at=_iso(now + wait))
            record["copies"][name] = "pending"
            self.log(
                f"candidate {commit[:7]}: {name} {reason}, trying again in {wait}s"
            )
            return
        self._fail(commit, record, str(reason), name)

    def _fail(
        self, commit: str, record: dict[str, Any], reason: str, detail: str
    ) -> bool:
        record.update(
            state="failed",
            since=_iso(self.clock()),
            reason=reason,
            detail=detail,
            retry_at=None,
        )
        self.log(f"candidate {commit[:7]}: failed ({reason}: {detail})")
        return False

    # ------------------------------------------------------------ pruning
    def _prune(self) -> None:
        records = self.records
        ordered = sorted(
            records,
            key=lambda key: (str(records[key].get("passed_at") or ""), key),
            reverse=True,
        )
        keep = set(ordered[: self.candidate.keep])
        for commit in ordered:
            record = records[commit]
            if (
                commit in keep
                or record.get("released")
                or record.get("state") in ("waiting", "building")
            ):
                continue
            del records[commit]
            self._jobs.pop(commit, None)
            self.log(f"candidate {commit[:7]}: dropped (keeping {self.candidate.keep})")

    # ------------------------------------------------------------ requests
    def handle(self, body: Mapping[str, Any]) -> None:
        """A ``candidate`` request: ``action`` ``request`` or ``release``.

        :raises GitOpsError: ``gitops-request-invalid``."""
        from piceli.gitops import GitOpsError

        action = body.get("action")
        env = body.get("env")
        if env is not None and env != self.candidate.environment:
            raise GitOpsError(
                "gitops-request-invalid",
                f"this controller prepares candidates of {self.candidate.environment}, "
                f"not {env}",
            )
        if action == "request":
            self.request(str(body.get("commit") or ""))
        elif action == "release":
            self.release(str(body.get("commit") or ""))
        else:
            raise GitOpsError("gitops-request-invalid", "unknown candidate action")

    def request(self, commit: str) -> str:
        """Start the candidate of ``commit`` at once (no quiet time); a
        building or ready one is left as it is. Returns the full commit.

        :raises GitOpsError: ``gitops-request-invalid`` (unknown commit)."""
        every = self._passed(self.state.get("passed_commits"), every=True)
        full = self.resolve(commit, among=sorted(set(self.records) | set(every)))
        record = self.records.get(full)
        now = self.clock()
        if record is not None and record.get("state") in ("building", "ready"):
            self.log(f"candidate {full[:7]}: requested, already {record['state']}")
            return full
        if record is None:
            entry = every.get(full) or {}
            record = self.records[full] = {
                "since": _iso(now),
                "passed_at": str(entry.get("last_at") or _iso(now)),
                "run": str(entry.get("last_run") or ""),
                "copies": {},
                "released": False,
                "digests": [],
            }
        record.update(requested=True, quiet_until=_iso(now))
        self._begin(full, record, now)
        self.log(f"candidate {full[:7]}: requested")
        return full

    def release(self, commit: str) -> str:
        """Mark the ready candidate of ``commit`` released: it stays.

        :raises GitOpsError: ``gitops-request-invalid``."""
        from piceli.gitops import GitOpsError

        full = self.resolve(commit, among=sorted(self.records))
        record = self.records[full]
        if record.get("state") != "ready":
            raise GitOpsError(
                "gitops-request-invalid",
                f"candidate {full[:7]} is {record.get('state')}, not ready",
            )
        record["released"] = True
        self.log(f"candidate {full[:7]}: released")
        return full

    # ------------------------------------------------------------ status
    def status(self) -> list[dict[str, Any]]:
        """``[{commit, state, since, reason, copies, ...}]``, newest first."""
        return status_view(self.state)


def status_view(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The candidates of ``state`` as the status lists them, newest first:
    ``commit``, ``state``, ``since``, ``reason``, ``detail``, ``copies``
    (``{component: pending|copied|failed}``), ``released``, ``run``,
    ``passed_at``, ``quiet_until`` (while waiting) and ``digests``."""
    records = state.get(STATE_KEY) or {}
    ordered = sorted(
        (key for key, item in records.items() if isinstance(item, Mapping)),
        key=lambda key: (str(records[key].get("passed_at") or ""), key),
        reverse=True,
    )
    found = []
    for commit in ordered:
        record = records[commit]
        waiting = record.get("state") == "waiting"
        found.append(
            {
                "commit": commit,
                "state": record.get("state"),
                "since": record.get("since"),
                "reason": record.get("reason"),
                "detail": record.get("detail"),
                "copies": dict(record.get("copies") or {}),
                "released": bool(record.get("released")),
                "run": record.get("run"),
                "passed_at": record.get("passed_at"),
                **({"quiet_until": record.get("quiet_until")} if waiting else {}),
                "digests": list(record.get("digests") or ()),
            }
        )
    return found


def describe(item: Mapping[str, Any]) -> str:
    """One status line: ``candidate abc1234: ready`` or ``candidate
    abc1234: not ready: <reason>``."""
    name = f"candidate {str(item.get('commit') or '')[:7]}"
    state = item.get("state")
    if state == "ready":
        return f"{name}: ready" + (", released" if item.get("released") else "")
    why = item.get("reason")
    detail = item.get("detail")
    if state == "waiting":
        until = item.get("quiet_until")
        why = "waiting for a quiet time" + (f" until {until}" if until else "")
    elif state == "building":
        copies = item.get("copies") or {}
        done = sum(1 for value in copies.values() if value == "copied")
        why = f"building ({done}/{len(copies)} copied)" if copies else "building"
        if item.get("reason"):  # a transient failure waiting for its next try
            why += f", {item['reason']}"
    elif why and detail:
        why = f"{why} ({detail})"
    return f"{name}: not ready: {why or state}"


def _load_credentials(secret: str) -> Any:
    """The target credentials of a Secret mounted by the install.

    :raises _Refused: ``candidate-credentials-missing``."""
    import json

    from piceli.artifacts.registry import RegistryCredentials

    try:
        document = json.loads(credentials_path(secret).read_text())
        if not isinstance(document, Mapping):
            raise ValueError("not an object")
        return RegistryCredentials(
            username=document.get("username"),
            password=document.get("password"),
            token=document.get("token"),
        )
    except (OSError, ValueError):
        raise _Refused(
            CREDENTIALS_MISSING, f"the credentials {secret} are not readable"
        ) from None
