"""Builds you can see and start (0.26.0).

Three kinds of build reach the GitOps controller: a
:class:`~piceli.infra.NixRelease` publish (``nix-publish``), an
environment's image build (``images``, on a build host or the in-cluster
builder) and a merge train's image handoff (``train-handoff``, a
``prebuilt`` offer). This module holds what they share:

- **triggers**: :func:`parse_schedule` and :func:`schedule_due` (a
  ``NixRelease(schedule=)``: ``hourly``, ``daily``, ``weekly`` or a
  five-field cron string, in UTC) and :data:`TRIGGERS`, the kinds a build
  record names;
- **records**: :func:`records` projects the controller's published status
  and history (``piceli gitops status``, the history ConfigMap) into one
  uniform record per build, ``piceli.builds.v1``, joined with the targets
  that run each build now (:func:`piceli.targets.build`). ``piceli gitops
  builds`` and the UI's Builds page show the same document;
- **progress**: :class:`Progress`, what a build host reports while it
  builds (its stage and log tail, ``build-progress``), and the cancel check
  it makes at each stage (``piceli gitops cancel``).

Importing this module is side-effect free.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = "piceli.builds.v1"
#: What started a build.
TRIGGERS = (
    "tag", "branch", "schedule", "webhook", "cli", "ui", "train", "sync", "push",
)  # fmt: skip
#: A build's stage, in order; the last three end it.
STAGES = (
    "approval-required",
    "queued",
    "fetching-inputs",
    "building",
    "signing",
    "uploading",
    "published",
    "failed",
    "cancelled",
    "rejected",
)
DONE = frozenset({"published", "failed", "cancelled", "rejected"})
#: A build host's stage names (its receipt, ``build-progress``) as stages.
HOST_STAGES = {
    "fetch": "fetching-inputs",
    "inputs": "fetching-inputs",
    "build": "building",
    "sign": "signing",
    "publish": "uploading",
    "push": "uploading",
}
#: The reason of a cancelled build.
CANCELLED = "gitops-build-cancelled"
#: Characters of a log tail a progress report carries.
PROGRESS_TAIL = 4000
#: Seconds between two progress reports of one build.
PROGRESS_SECONDS = 15.0
_NAMED = {"hourly": "0 * * * *", "daily": "0 0 * * *", "weekly": "0 0 * * 1"}
_FIELDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6))
_SHA = re.compile(r"[0-9a-f]{40}")


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(text: Any) -> float | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# ---------------------------------------------------------------- schedules


def parse_schedule(text: Any) -> tuple[frozenset[int], ...]:
    """``hourly``, ``daily`` (00:00 UTC), ``weekly`` (Monday 00:00 UTC) or a
    cron string ``MINUTE HOUR DAY MONTH WEEKDAY`` (``*``, ``N``, ``A-B``,
    ``*/N``, ``A-B/N`` and lists; weekday 0 is Sunday), in UTC.

    :raises ValueError: anything else."""
    if not isinstance(text, str):
        raise ValueError("a schedule is a string")
    fields = _NAMED.get(text.strip(), text).split()
    if len(fields) != 5:
        raise ValueError(
            "a schedule is hourly, daily, weekly or five cron fields "
            "(MINUTE HOUR DAY MONTH WEEKDAY)"
        )
    found = []
    for field, (low, high) in zip(fields, _FIELDS, strict=True):
        values: set[int] = set()
        for part in field.split(","):
            span, _, step_text = part.partition("/")
            step = (
                int(step_text) if step_text.isdigit() else (1 if not step_text else 0)
            )
            if span == "*":
                start, end = low, high
            elif re.fullmatch(r"\d+", span):
                start = end = int(span)
                end = high if step_text else end
            elif re.fullmatch(r"\d+-\d+", span):
                start, end = (int(item) for item in span.split("-"))
            else:
                raise ValueError(f"schedule field {field!r} is not cron")
            if step < 1 or start < low or end > high or start > end:
                raise ValueError(f"schedule field {field!r} is out of range")
            values |= set(range(start, end + 1, step))
        found.append(frozenset(values))
    return tuple(found)


def _matches(fields: Sequence[frozenset[int]], moment: datetime) -> bool:
    weekday = (moment.weekday() + 1) % 7  # cron: 0 is Sunday
    return (
        moment.minute in fields[0]
        and moment.hour in fields[1]
        and moment.day in fields[2]
        and moment.month in fields[3]
        and weekday in fields[4]
    )


def schedule_due(text: str, last: float | None, now: float) -> bool:
    """Whether a minute of the schedule fell in ``(last, now]``. ``last``
    ``None`` (first sight) is never due: the next scheduled minute is."""
    if last is None or now <= last:
        return False
    fields = parse_schedule(text)
    minute = int(last // 60) + 1
    end = int(now // 60)
    if end - minute > 60 * 24 * 62:  # a long gap: the schedule surely passed
        return True
    while minute <= end:
        if _matches(fields, datetime.fromtimestamp(minute * 60, UTC)):
            return True
        minute += 1
    return False


# ---------------------------------------------------------------- refs


def resolve_ref(
    ref: str,
    *,
    tags: Mapping[str, str],
    branches: Mapping[str, str],
    tag_ok: Callable[[str], bool],
    branch: str | None,
) -> tuple[str | None, str | None, str] | None:
    """``(tag, branch, commit)`` a hand-started build of ``ref`` publishes:
    a tag of the release (``tag_ok``), its followed ``branch`` (its head),
    or a full commit (the release's tag at it when there is one, else no
    tag); ``None`` when ``ref`` is none of them."""
    if ref in tags and tag_ok(ref):
        return ref, None, str(tags[ref])
    if branch is not None and ref == branch and ref in branches:
        return None, branch, str(branches[ref])
    if _SHA.fullmatch(ref):
        tagged = sorted(
            name for name, sha in tags.items() if sha == ref and tag_ok(name)
        )
        if tagged:
            return tagged[-1], None, ref
        if branch is not None and branches.get(branch) == ref:
            return None, branch, ref
        return None, None, ref
    return None


def branch_contains(
    url: str,
    branch: str,
    commit: str,
    mirrors: Path,
    run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
) -> bool:
    """Whether ``commit`` is on ``branch`` of ``url`` (its head or an
    ancestor), fetched into the build host's mirror with its own Git access:
    a build host publishes a branch's pointer only for a commit of it."""
    from piceli.infra.builder_serve import _slug, fetch_env, fetch_urls

    mirror = mirrors / f"{_slug(url)}.git"
    ref = f"refs/heads/{branch}"
    for source in fetch_urls(url):
        run(
            ["git", "--git-dir", str(mirror), "fetch", "--quiet", source,
             f"+{ref}:{ref}"],
            check=False, capture_output=True, timeout=600, env=fetch_env(),
        )  # fmt: skip
        found = run(
            ["git", "--git-dir", str(mirror), "merge-base", "--is-ancestor",
             commit, ref],
            check=False, capture_output=True,
        )  # fmt: skip
        if found.returncode == 0:
            return True
    return False


# ---------------------------------------------------------------- progress


class Progress:
    """What a build host reports while it builds one wanted build: its stage
    and log tail (``build-progress``, at each stage and at most every
    :data:`PROGRESS_SECONDS` while it logs), and whether the build is still
    wanted (``piceli gitops cancel`` drops it): :meth:`stage` raises
    :class:`Cancelled` then, so the host stops before its next stage.

    :param channel: The controller's channel (``add_request``,
        ``read_status``).
    """

    def __init__(
        self,
        channel: Any,
        build: str,
        host: str,
        *,
        clock: Callable[[], float],
        tail: Callable[[], Iterable[str]] = lambda: (),
    ) -> None:
        self.channel = channel
        self.build = build
        self.host = host
        self.clock = clock
        self.tail = tail
        self.current: str | None = None
        self._sent = 0.0

    def report(self, force: bool = False) -> None:
        """Send the stage and log tail (never raises: progress is a view)."""
        now = self.clock()
        if not force and now - self._sent < PROGRESS_SECONDS:
            return
        self._sent = now
        from piceli.gitops.state import build_progress_request

        text = "\n".join(self.tail())[-PROGRESS_TAIL:]
        try:
            self.channel.add_request(
                *build_progress_request(
                    self.build, self.host, self.current or "fetch", text, _iso(now)
                )
            )
        except Exception:  # a report never fails the build
            pass

    def wanted(self) -> bool:
        """Whether the controller still wants this build (``True`` when the
        status cannot be read: a read failure never stops a build)."""
        try:
            status = self.channel.read_status() or {}
        except Exception:
            return True
        entries = (status.get("controller") or {}).get("builds_wanted")
        if not isinstance(entries, list):
            return True
        return any(
            isinstance(item, Mapping)
            and item.get("id") == self.build
            and not item.get("cancelled")
            for item in entries
        )

    def stage(self, name: str) -> None:
        """Entering stage ``name``. :raises Cancelled: the build was cancelled."""
        if not self.wanted():
            raise Cancelled(self.build)
        self.current = name
        self.report(force=True)


class Cancelled(Exception):
    """The controller no longer wants the build (``piceli gitops cancel``)."""

    def __init__(self, build: str) -> None:
        super().__init__(f"build {build} was cancelled")
        self.build = build


# ---------------------------------------------------------------- records


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _text(value: Any, limit: int = 300) -> str | None:
    return value[:limit] if isinstance(value, str) and value else None


def _trigger(text: Any, kind: Any = None, by: Any = None) -> dict[str, Any]:
    """A record's ``trigger``: its kind (:data:`TRIGGERS`), who asked, the text."""
    detail = _text(text, 120)
    if kind not in TRIGGERS:
        first = (detail or "").split(" ", 1)[0]
        kind = first if first in TRIGGERS else ("sync" if detail else None)
    return {"kind": kind, "by": _text(by, 120), "detail": detail}


def _stages(found: Any) -> list[dict[str, Any]]:
    """A receipt's ``{name: {started_at, finished_at, seconds}}`` as the
    record's ordered stages (the controller's ``approval`` and ``queue``
    waits as ``approval-required`` and ``queued``)."""
    names = {"approval": "approval-required", "queue": "queued", **HOST_STAGES}
    items = []
    for name, value in _dict(found).items():
        stage = names.get(str(name), str(name))
        if stage not in STAGES:
            continue
        item = _dict(value)
        items.append(
            {
                "stage": stage,
                "started_at": _text(item.get("started_at"), 40),
                "finished_at": _text(item.get("finished_at"), 40),
                "seconds": item.get("seconds")
                if isinstance(item.get("seconds"), int | float)
                else None,
            }
        )
    return sorted(items, key=lambda item: STAGES.index(item["stage"]))


def _seconds(started: Any, finished: Any, now: float) -> float | None:
    begin = _epoch(started)
    if begin is None:
        return None
    end = _epoch(finished)
    return round(max(0.0, (end if end is not None else now) - begin), 3)


def _nix_stage(record: Mapping[str, Any], progress: Mapping[str, Any]) -> str:
    state = record.get("state")
    if state == "published":
        return "published"
    if state in ("failed", "rejected"):
        return str(state)
    if state == "cancelled" or record.get("reason") == CANCELLED:
        return "cancelled"
    if state in ("approval-required", "pending"):
        return "approval-required"
    if state == "publishing":
        return HOST_STAGES.get(str(progress.get("stage")), "fetching-inputs")
    return "queued"


def _record(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": None, "kind": None, "target": None, "trigger": _trigger(None),
        "commit": None, "tag": None, "branch": None, "host": None,
        "stage": "queued", "stages": [], "log_tail": None, "log_complete": False,
        "requested_at": None, "started_at": None, "finished_at": None,
        "seconds": None, "plan_digest": None, "plan": None, "reason": None,
        "images": [], "running_on": [], "host_switch": None,
        "actions": {"approve": False, "retry": False, "cancel": False},
    }  # fmt: skip
    return base | fields


def _nix_records(
    status: Mapping[str, Any], history: Mapping[str, Any], now: float
) -> list[dict[str, Any]]:
    wanted = {
        str(item.get("id")): item
        for item in _dict(status.get("controller")).get("builds_wanted") or ()
        if isinstance(item, Mapping)
    }
    found = []
    releases = _dict(status.get("nix_releases"))
    for name, value in sorted(releases.items()):
        record = _dict(value)
        if record.get("state") in ("idle", "baseline", None) and not record.get(
            "plan_digest"
        ):
            continue
        entry = _dict(wanted.get(str(record.get("build_id"))))
        progress = _dict(entry.get("progress"))
        stage = _nix_stage(record, progress)
        failure = _dict(record.get("failure"))
        claimed = _dict(record.get("claimed")) or _dict(entry.get("claimed"))
        stages = _stages(record.get("stages"))
        if stage not in DONE and progress.get("stage"):
            stages.append(
                {"stage": stage, "started_at": _text(progress.get("at"), 40),
                 "finished_at": None, "seconds": None}
            )  # fmt: skip
        plan = _dict(record.get("plan")) or None
        found.append(
            _record(
                id=f"{name}@{record.get('requested_at')}",
                kind="nix-publish",
                target=name,
                trigger=_trigger(
                    record.get("trigger"),
                    record.get("trigger_kind"),
                    record.get("requested_by"),
                ),
                commit=record.get("commit"),
                tag=record.get("tag"),
                branch=(plan or {}).get("branch"),
                host=_dict(record.get("published")).get("host")
                or failure.get("host")
                or claimed.get("host")
                or record.get("builder"),
                stage=stage,
                stages=stages,
                log_tail=_text(failure.get("log_tail"), 8000)
                or _text(progress.get("log_tail"), 8000),
                requested_at=record.get("requested_at"),
                started_at=record.get("started_at"),
                finished_at=record.get("finished_at"),
                seconds=record.get("seconds")
                if stage in DONE
                else _seconds(record.get("requested_at"), None, now),
                plan_digest=record.get("plan_digest"),
                plan=plan,
                reason=record.get("reason"),
                actions={
                    "approve": stage == "approval-required",
                    "retry": stage in DONE,
                    "cancel": stage not in DONE,
                },
            )
        )
        current = found[-1]["id"]
        runs = _dict(_dict(history.get("nix_releases")).get(name)).get("runs") or []
        for run in runs:
            run = _dict(run)
            if run.get("id") == current:
                continue
            failure_tail = _text(run.get("log_tail"), 8000)
            found.append(
                _record(
                    id=run.get("id"),
                    kind="nix-publish",
                    target=name,
                    trigger=_trigger(
                        run.get("trigger"),
                        run.get("trigger_kind"),
                        run.get("requested_by"),
                    ),
                    commit=run.get("commit"),
                    tag=run.get("tag"),
                    branch=run.get("branch"),
                    host=run.get("host") or run.get("builder"),
                    stage=run.get("state")
                    if run.get("state") in DONE
                    else ("cancelled" if run.get("reason") == CANCELLED else "failed"),
                    stages=_stages(run.get("stages")),
                    log_tail=failure_tail,
                    requested_at=run.get("requested_at"),
                    started_at=run.get("started_at"),
                    finished_at=run.get("finished_at"),
                    seconds=run.get("seconds"),
                    plan_digest=run.get("plan_digest"),
                    reason=run.get("reason"),
                    actions={"approve": False, "retry": True, "cancel": False},
                )
            )
    return found


def _image_records(
    status: Mapping[str, Any], history: Mapping[str, Any], now: float
) -> list[dict[str, Any]]:
    found = []
    controller = _dict(status.get("controller"))
    envs = _dict(status.get("envs"))
    live: set[str] = set()
    for entry in controller.get("builds_wanted") or ():
        entry = _dict(entry)
        if entry.get("kind") == "nix-publish":
            continue
        owner = str(entry.get("owner") or "")
        target = str(_dict(envs.get(owner)).get("group") or owner) or None
        progress = _dict(entry.get("progress"))
        failure = _dict(entry.get("failed"))
        claimed = _dict(entry.get("claimed"))
        stage = (
            "failed"
            if failure
            else HOST_STAGES.get(str(progress.get("stage")), "building")
            if claimed
            else "queued"
        )
        commits = _dict(entry.get("commits"))
        record = _dict(envs.get(owner))
        live.add(owner)
        found.append(
            _record(
                id=f"images:{entry.get('id')}",
                kind="images",
                target=target,
                trigger=_trigger(_dict(record.get("in_progress")).get("trigger")),
                commit=next(iter(sorted(commits.values())), None),
                host=failure.get("host") or claimed.get("host"),
                stage=stage,
                stages=[
                    {
                        "stage": "queued",
                        "started_at": entry.get("requested_at"),
                        "finished_at": claimed.get("at"),
                        "seconds": None,
                    },
                    *(
                        [
                            {
                                "stage": stage,
                                "started_at": progress.get("at") or claimed.get("at"),
                                "finished_at": None,
                                "seconds": None,
                            }
                        ]
                        if claimed
                        else []
                    ),
                ],
                log_tail=_text(failure.get("log_tail"), 8000)
                or _text(progress.get("log_tail"), 8000),
                requested_at=entry.get("requested_at"),
                started_at=claimed.get("at"),
                seconds=_seconds(entry.get("requested_at"), None, now),
                reason=_text(failure.get("reason")),
                images=list(entry.get("images") or ()),
                actions={
                    "approve": False,
                    "retry": bool(failure),
                    "cancel": not failure,
                },
            )
        )
    for entry in controller.get("cluster_builds") or ():
        # 0.42.0: an in-cluster build Job that runs (the controller looks at
        # it once per poll).
        entry = _dict(entry)
        owner = str(entry.get("owner") or "")
        target = str(_dict(envs.get(owner)).get("group") or owner) or None
        live.add(owner)
        found.append(
            _record(
                id=f"images:{entry.get('id')}",
                kind="images",
                target=target,
                trigger=_trigger(
                    _dict(_dict(envs.get(owner)).get("in_progress")).get("trigger")
                ),
                host="cluster",
                stage="building",
                stages=[
                    {
                        "stage": "building",
                        "started_at": entry.get("started_at"),
                        "finished_at": None,
                        "seconds": None,
                    }
                ],
                requested_at=entry.get("started_at"),
                started_at=entry.get("started_at"),
                seconds=_seconds(entry.get("started_at"), None, now),
                images=list(entry.get("images") or ()),
                actions={"approve": False, "retry": False, "cancel": True},
            )
        )
    for name, value in sorted(_dict(history.get("envs")).items()):
        for run in (_dict(value).get("runs") or ())[:20]:
            run = _dict(run)
            builds = _dict(run.get("builds"))
            built = [str(item) for item in run.get("built") or () if item != "mirror"]
            if not built or not builds:
                continue
            times = [_dict(builds.get(item)) for item in built if builds.get(item)]
            started = min((t.get("started_at") or "" for t in times), default="")
            finished = max((t.get("finished_at") or "" for t in times), default="")
            failed = any(t.get("state") == "failed" for t in times)
            components = _dict(run.get("components"))
            # The image's provenance, else (0.31.0) the build's own host.
            hosts = sorted(
                {
                    str(
                        _dict(_dict(components.get(item)).get("provenance")).get("host")
                        or _dict(builds.get(item)).get("host")
                    )
                    for item in built
                    if _dict(_dict(components.get(item)).get("provenance")).get("host")
                    or _dict(builds.get(item)).get("host")
                }
            )
            commits = [
                _dict(components.get(item)).get("commit")
                for item in built
                if _dict(components.get(item)).get("commit")
            ]
            found.append(
                _record(
                    id=f"images:{name}@{run.get('started_at')}",
                    kind="images",
                    target=name,
                    trigger=_trigger(run.get("trigger")),
                    commit=commits[0] if commits else None,
                    host=", ".join(hosts) or None,
                    stage="failed" if failed else "published",
                    stages=[
                        {
                            "stage": "building",
                            "started_at": started or None,
                            "finished_at": finished or None,
                            "seconds": _seconds(started, finished, now),
                        },
                    ],
                    requested_at=run.get("started_at"),
                    started_at=started or None,
                    finished_at=finished or None,
                    seconds=_seconds(started, finished, now),
                    reason=_text(run.get("reason")) if failed else None,
                    images=built,
                    actions={"approve": False, "retry": True, "cancel": False},
                )
            )
    return found


def _train_records(status: Mapping[str, Any]) -> list[dict[str, Any]]:
    found = []
    for offer in _dict(status.get("controller")).get("prebuilt") or ():
        offer = _dict(offer)
        found.append(
            _record(
                id=f"train:{offer.get('component')}:{offer.get('key')}",
                kind="train-handoff",
                target=_text(offer.get("component")),
                trigger=_trigger(f"train {offer.get('train') or ''}".strip(), "train"),
                host=_text(offer.get("host")),
                stage="published",
                stages=[
                    {
                        "stage": "published",
                        "started_at": offer.get("at"),
                        "finished_at": offer.get("at"),
                        "seconds": 0.0,
                    }
                ],
                requested_at=offer.get("at"),
                finished_at=offer.get("at"),
                images=[offer.get("image")] if offer.get("image") else [],
            )
        )
    return found


def _running(build: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """The targets (``piceli targets`` rows) that run ``build`` now.

    A NixHost runs a publish only once its own agent reports the revision
    as its current generation with its checks passed: the controller's
    view of a host is only what it published, not what the host switched
    to (ops#42)."""
    commit, tag = build.get("commit"), build.get("tag")
    if build.get("stage") != "published" or not commit:
        return []
    found = []
    for row in rows:
        revision = _dict(row.get("revision"))
        commits = _dict(revision.get("commits"))
        if build.get("kind") == "nix-publish":
            if row.get("kind") != "nix-host" or row.get("name") != build.get("target"):
                continue
            if not _switched(row):
                continue
            if revision.get("commit") == commit or (tag and revision.get("tag") == tag):
                found.append(str(row.get("name")))
        elif row.get("kind") == "environment" and (
            revision.get("commit") == commit or commit in commits.values()
        ):
            found.append(str(row.get("name")))
    return found


def _switched(row: Mapping[str, Any]) -> bool:
    """A NixHost row told by the host's agent: deployed, checks passed."""
    facts = _dict(row.get("facts"))
    return (
        facts.get("revision") == "host-agent"
        and row.get("state") == "deployed"
        and _dict(row.get("checks")).get("state") == "passed"
    )


def _host_switch(build: Mapping[str, Any]) -> str | None:
    """A published NixRelease's place on its host: ``switched`` once the
    host reports it (checks passed), else ``not-switched-yet``."""
    if build.get("kind") != "nix-publish" or build.get("stage") != "published":
        return None
    return "switched" if build.get("running_on") else "not-switched-yet"


def records(
    status: Mapping[str, Any] | None,
    history: Mapping[str, Any] | None = None,
    *,
    now: float,
    target: str | None = None,
    state: str | None = None,
    hosts: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """``piceli.builds.v1``: every build the controller's ``status`` and
    ``history`` know, newest first, each joined with the targets that run
    it now; only ``target``'s, or those in ``state`` (a stage, ``active``
    for the unfinished ones), when given.

    ``hosts`` is :func:`piceli.targets.read_hosts`'s result: a NixHost
    runs a publish only when its own agent says so; without it, every
    published NixRelease is ``not-switched-yet``."""
    from piceli.targets import build as targets

    if not isinstance(status, Mapping):
        return {"schema": SCHEMA, "builds": [], "stages": list(STAGES)}
    history = history if isinstance(history, Mapping) else {}
    rows = targets(dict(status), hosts=hosts).get("targets") or []
    items = [
        *_nix_records(status, history, now),
        *_image_records(status, history, now),
        *_train_records(status),
    ]
    for item in items:
        item["running_on"] = _running(item, rows)
        item["host_switch"] = _host_switch(item)
    if target is not None:
        items = [item for item in items if item.get("target") == target]
    if state is not None:
        items = [
            item
            for item in items
            if (state == "active" and item["stage"] not in DONE)
            or item["stage"] == state
        ]
    active = [item for item in items if item["stage"] not in DONE]
    done = [item for item in items if item["stage"] in DONE]
    done.sort(key=lambda item: str(item.get("requested_at") or ""), reverse=True)
    active.sort(key=lambda item: str(item.get("requested_at") or ""), reverse=True)
    return {"schema": SCHEMA, "builds": [*active, *done], "stages": list(STAGES)}
