"""Alarms of a composition's GitOps controller (0.28.0).

Declared on the controller, with the train's notifiers
(:class:`~piceli.train.notify.Notify`)::

    from piceli.infra import Alarms, Controller, Notify

    Controller(
        on="node-1",
        alarms=Alarms(
            targets=[
                Notify.ntfy("https://ntfy.example.org/deploys"),
                Notify.webhook(url_env="ALARM_WEBHOOK_URL"),
                Notify.desktop(),
            ],
            secret="piceli-alarms",
            waiting_after="10m",
        ),
    )

After every poll the controller compares what its status says with the
alarms it already sent and sends what is new (:func:`observe`,
:func:`changes`):

- ``env-failed``: an environment failed (its step failed, or failed twice
  in a row and retries), or its checks fail on a running release
  (``degraded``);
- ``env-waiting``: an environment waits longer than ``waiting_after`` for a
  build host (``build-waiting``) or an approval (``approval-required``);
- ``nix-release-failed`` and ``nix-release-waiting``: a ``NixRelease``
  publish failed, or no build host took it within ``waiting_after``;
- ``composition-failed``: the composition's own repository did not import
  at its commit, so nothing deploys;
- ``controller-restarted``: the controller runs a new Piceli version.

Each alarm is sent once: the alarms already sent are kept in the
controller's state (they survive a restart). When the condition ends, one
``resolved`` message is sent (``staging recovered``). ``ntfy`` and webhook
targets are sent by the controller in the cluster; a URL or token that is a
secret is named by environment variable (``url_env=``, ``token_env=``,
``headers_env=``) and comes from the Kubernetes Secret ``secret``, mounted
into the controller as those variables, never in its arguments, logs, state
or status. ``Notify.desktop()`` cannot come from the cluster:
``piceli gitops alarms --follow`` on your machine reads the controller's
status and raises the same alarms as desktop notifications.

A failed send never stops a poll: it is logged with ``gitops-alarm-failed``
and listed under ``controller.alarms.failures`` in the status.

Every function here but :class:`AlarmSender` is pure. Importing this module
is side-effect free.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

ALARM_SCHEMA = "piceli.alarm.v1"
#: Every event an alarm can be about.
EVENTS = (
    "env-failed",
    "env-waiting",
    "nix-release-failed",
    "nix-release-waiting",
    "composition-failed",
    "controller-restarted",
)
#: The reason code of a failed send.
FAILED_CODE = "gitops-alarm-failed"
#: Failed sends the status keeps.
MAX_FAILURES = 10
#: Attempts in a row after which a retrying environment counts as failing.
FAILING_ATTEMPTS = 2
_SECRET = re.compile(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?")
_BUILD_WAITING = "build-waiting"
_RESERVED = frozenset({"HOME", "TMPDIR", "PATH"})


def AlarmsError(message: str) -> ValueError:
    """An ``Alarms(...)`` declaration refused: ``cluster-invalid``."""
    from piceli.infra.cluster import ClusterError

    return ClusterError("cluster-invalid", message)


@dataclass(frozen=True)
class Alarms:
    """Where the GitOps controller sends its alarms (0.28.0).

    :param targets: :meth:`Notify.ntfy <piceli.train.notify.Notify.ntfy>`,
        :meth:`Notify.webhook <piceli.train.notify.Notify.webhook>` (sent by
        the controller) and :meth:`Notify.desktop
        <piceli.train.notify.Notify.desktop>` (raised by ``piceli gitops
        alarms --follow`` on your machine). ``when="failure"`` skips the
        ``resolved`` and ``controller-restarted`` messages.
    :param waiting_after: How long an environment or a ``NixRelease`` may
        wait for a build host or an approval before it is an alarm
        (``10m``, ``1h``).
    :param secret: A Secret in the controller's namespace holding the
        environment variables the targets name (``url_env=``,
        ``token_env=``, ``headers_env=``): each is set from the Secret's key
        of the same name.
    """

    targets: Sequence[Any] = ()
    waiting_after: str = "10m"
    secret: str | None = None
    _seconds: int = field(default=0, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        from piceli.gitops import GitOpsError
        from piceli.gitops.config import parse_duration
        from piceli.train.notify import CommandNotify, Notify

        if isinstance(self.targets, Notify) or not isinstance(self.targets, Sequence):
            raise AlarmsError("Alarms(targets=) is a list of Notify objects")
        targets = tuple(self.targets)
        if not all(isinstance(item, Notify) for item in targets):
            raise AlarmsError("Alarms(targets=) is a list of Notify objects")
        if any(isinstance(item, CommandNotify) for item in targets):
            raise AlarmsError(
                "Alarms(targets=) takes Notify.ntfy, Notify.webhook and "
                "Notify.desktop: the controller runs no command"
            )
        object.__setattr__(self, "targets", targets)
        try:
            seconds = parse_duration(self.waiting_after)
        except (GitOpsError, TypeError):
            raise AlarmsError("Alarms(waiting_after=) must be like 10m or 1h") from None
        if seconds < 60:
            raise AlarmsError("Alarms(waiting_after=) must be at least 1m")
        object.__setattr__(self, "_seconds", seconds)
        if self.secret is not None and not (
            isinstance(self.secret, str) and _SECRET.fullmatch(self.secret)
        ):
            raise AlarmsError("Alarms(secret=) must name a Secret")
        reserved = [
            name
            for name in self.variables
            if name in _RESERVED or name.startswith(("PICELI_", "KUBERNETES_"))
        ]
        if reserved:
            raise AlarmsError(
                "the controller sets " + ", ".join(reserved) + " itself: name the "
                "targets' variables otherwise (ALARM_WEBHOOK_URL)"
            )
        if self.variables and self.secret is None:
            raise AlarmsError(
                "the targets read " + ", ".join(self.variables) + ": name the "
                "Secret that holds them with Alarms(secret=)"
            )

    @property
    def waiting_seconds(self) -> int:
        return self._seconds

    @property
    def variables(self) -> tuple[str, ...]:
        """The environment variables the targets read (the Secret's keys)."""
        found: dict[str, None] = {}
        for item in self.targets:
            found.update(dict.fromkeys(item.variables()))
        return tuple(found)

    def to_dict(self) -> dict[str, Any]:
        """The declaration as data: variable and Secret names, never values."""
        return {
            "targets": [item.to_dict() for item in self.targets],
            "waiting_after": self.waiting_after,
            "secret": self.secret,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Alarms:
        from piceli.train.model import TrainError
        from piceli.train.notify import notify_from_dict

        if not isinstance(value, Mapping):
            raise AlarmsError("alarms is Alarms(...)")
        try:
            targets = [notify_from_dict(item) for item in value.get("targets") or ()]
        except TrainError as error:
            raise AlarmsError(str(error)) from None
        return cls(
            targets=targets,
            waiting_after=str(value.get("waiting_after") or "10m"),
            secret=value.get("secret"),
        )


# ---------------------------------------------------------------- events


@dataclass(frozen=True)
class Alarm:
    """One alarm: an event about a subject, firing or resolved."""

    event: str
    subject: str
    reason: str
    next: str
    fingerprint: str = ""
    detail: str | None = None
    stage: str | None = None
    commit: str | None = None
    since: str | None = None
    state: str = "firing"

    @property
    def key(self) -> str:
        return f"{self.event}:{self.subject}"

    @property
    def ok(self) -> bool:
        return self.state == "resolved" or self.event == "controller-restarted"

    @property
    def title(self) -> str:
        if self.state == "resolved":
            return f"piceli: {self.subject} " + (
                "recovered" if self.event.endswith("failed") else "no longer waiting"
            )
        return {
            "env-failed": f"piceli: {self.subject} failed",
            "env-waiting": f"piceli: {self.subject} waiting",
            "nix-release-failed": f"piceli: nix release {self.subject} failed",
            "nix-release-waiting": f"piceli: nix release {self.subject} waiting",
            "composition-failed": "piceli: composition does not import",
            "controller-restarted": "piceli: controller restarted",
        }.get(self.event, f"piceli: {self.subject}")

    @property
    def text(self) -> str:
        """The message: what, where, the reason code and the next command."""
        commit = f" at {self.commit[:12]}" if self.commit else ""
        if self.state == "resolved":
            what = "recovered" if self.event.endswith("failed") else "no longer waiting"
            return f"{self.subject} {what} (was {self.reason}). Next: {self.next}"
        detail = f": {self.detail}" if self.detail else ""
        if self.event == "env-failed":
            stage = f" in stage {self.stage}" if self.stage else ""
            return (
                f"{self.subject} failed{stage} ({self.reason}){commit}{detail}. "
                f"Next: {self.next}"
            )
        if self.event in ("env-waiting", "nix-release-waiting"):
            on = "a build host" if self.reason == _BUILD_WAITING else "an approval"
            what = "nix release " if self.event == "nix-release-waiting" else ""
            return (
                f"{what}{self.subject} waits for {on} since {self.since} "
                f"({self.reason}){commit}. Next: {self.next}"
            )
        if self.event == "nix-release-failed":
            return (
                f"nix release {self.subject} failed ({self.reason}){commit}{detail}. "
                f"Next: {self.next}"
            )
        if self.event == "composition-failed":
            return (
                f"the composition does not import ({self.reason}){commit}{detail}; "
                f"nothing deploys. Next: {self.next}"
            )
        return f"{self.detail} ({self.reason}). Next: {self.next}"

    def payload(self, composition: str | None, at: str | None) -> dict[str, Any]:
        """The JSON a webhook receives (``piceli.alarm.v1``)."""
        return {
            "schema": ALARM_SCHEMA,
            "event": self.event,
            "state": self.state,
            "ok": self.ok,
            "subject": self.subject,
            "reason": self.reason,
            "stage": self.stage,
            "commit": self.commit,
            "since": self.since,
            "detail": self.detail,
            "next": self.next,
            "title": self.title,
            "message": self.text,
            "composition": composition,
            "at": at,
        }


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _epoch(text: Any) -> float | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        return (
            datetime.fromisoformat(text.replace("Z", "+00:00"))
            .astimezone(UTC)
            .timestamp()
        )
    except ValueError:
        return None


def _short(text: Any, limit: int = 160) -> str | None:
    if not isinstance(text, str) or not text.strip():
        return None
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 3] + "..."


def _failing_checks(record: Mapping[str, Any]) -> str | None:
    from piceli.gitops.wait import failed_checks

    found = [
        f"{item['name']} ({item['detail']})"
        if item.get("detail")
        else str(item["name"])
        for item in failed_checks(record)
    ]
    return ("failing checks: " + ", ".join(found)) if found else None


def _env_alarms(
    name: str, record: Mapping[str, Any], now: float, waiting: float
) -> list[Alarm]:
    state, reason = record.get("state"), record.get("reason")
    commit = record.get("commit") if isinstance(record.get("commit"), str) else None
    status = "piceli gitops status"
    if record.get("health") == "degraded":
        return [
            Alarm(
                "env-failed", name, str(reason or "pipeline-checks-failed"), status,
                fingerprint=f"degraded:{commit}", detail=_failing_checks(record),
                commit=commit, stage="checks",
            )
        ]  # fmt: skip
    failing = state == "failed" or (
        state == "retrying"
        and reason not in (None, _BUILD_WAITING)
        and int(record.get("attempts") or 0) >= FAILING_ATTEMPTS
    )
    if failing:
        failure = _dict(record.get("failure"))
        lines = str(failure.get("log_tail") or "").strip().splitlines()
        detail = _failing_checks(record) or (_short(lines[-1]) if lines else None)
        if state == "retrying":
            detail = f"attempt {record.get('attempts')}, retrying" + (
                f"; {detail}" if detail else ""
            )
        return [
            Alarm(
                "env-failed", name, str(reason or "gitops-env-failed"), status,
                fingerprint=f"{reason}:{commit}", detail=detail, commit=commit,
                stage=record.get("failed_stage"),
            )
        ]  # fmt: skip
    if reason == _BUILD_WAITING and state == "retrying":
        since = record.get("waiting_since")
        began = _epoch(since)
        if began is not None and now - began >= waiting:
            return [
                Alarm(
                    "env-waiting", name, _BUILD_WAITING, "piceli gitops builds",
                    fingerprint=f"{_BUILD_WAITING}:{since}", since=since,
                    commit=commit,
                )
            ]  # fmt: skip
    if state == "approval-required":
        since = record.get("approval_required_since")
        began = _epoch(since)
        if began is not None and now - began >= waiting:
            return [
                Alarm(
                    "env-waiting", name, "approval-required",
                    f"piceli gitops approve {name} {record.get('plan_hash')}",
                    fingerprint=f"approval:{record.get('plan_hash')}", since=since,
                    commit=commit,
                )
            ]  # fmt: skip
    return []


def _release_alarms(
    name: str, record: Mapping[str, Any], now: float, waiting: float
) -> list[Alarm]:
    state = record.get("state")
    commit = record.get("commit") if isinstance(record.get("commit"), str) else None
    if state == "failed":
        failure = _dict(record.get("failure"))
        host = failure.get("host")
        lines = str(failure.get("log_tail") or "").strip().splitlines()
        tail = _short(lines[-1]) if lines else None
        detail = "; ".join(
            item
            for item in (
                f"build host {host}" if host else None,
                str(failure.get("reason")) if failure.get("reason") else None,
                tail,
            )
            if item
        )
        return [
            Alarm(
                "nix-release-failed", name, str(record.get("reason") or "gitops-nix-release-failed"),
                "piceli gitops builds --state failed",
                fingerprint=f"{record.get('tag')}:{commit}:{record.get('finished_at')}",
                detail=detail or None, commit=commit,
            )
        ]  # fmt: skip
    if state in ("waiting-build", "approval-required"):
        since = (
            record.get("approval_required_since")
            if state == "approval-required"
            else None
        ) or record.get("requested_at")
        began = _epoch(since)
        if began is not None and now - began >= waiting:
            approval = state == "approval-required"
            return [
                Alarm(
                    "nix-release-waiting", name,
                    "approval-required" if approval else _BUILD_WAITING,
                    str(record.get("approve_command") or f"piceli gitops approve {name}")
                    if approval else "piceli gitops builds",
                    fingerprint=f"{state}:{record.get('tag')}:{commit}", since=since,
                    commit=commit,
                )
            ]  # fmt: skip
    return []


def observe(status: Mapping[str, Any], now: float, waiting: float) -> dict[str, Alarm]:
    """The alarms whose condition holds in ``status`` (a GitOps status
    document) at ``now``, by :attr:`Alarm.key`."""
    found: list[Alarm] = []
    controller = _dict(status.get("controller"))
    repo = _dict(controller.get("composition_repo"))
    if repo.get("failed"):
        found.append(
            Alarm(
                "composition-failed", str(repo.get("name") or "composition"),
                str(repo.get("error") or "composition-invalid"), "piceli gitops status",
                fingerprint=str(repo.get("failed")), detail=_short(repo.get("error_detail")),
                commit=str(repo.get("failed")),
            )
        )  # fmt: skip
    for name, record in sorted(_dict(status.get("envs")).items()):
        if isinstance(record, Mapping):
            found += _env_alarms(name, record, now, waiting)
    for name, record in sorted(_dict(status.get("nix_releases")).items()):
        if isinstance(record, Mapping):
            found += _release_alarms(name, record, now, waiting)
    return {item.key: item for item in found}


def _subjects(status: Mapping[str, Any]) -> set[str]:
    return {
        *(f"env:{name}" for name in _dict(status.get("envs"))),
        *(f"nix:{name}" for name in _dict(status.get("nix_releases"))),
    }


def changes(
    memory: dict[str, Any],
    status: Mapping[str, Any],
    now: float,
    waiting: float,
) -> list[Alarm]:
    """The alarms to send now, updating ``memory`` (``{"open": {...},
    "version": ...}``, kept between polls and restarts).

    A new alarm, or one whose occurrence changed (another commit, another
    reason), fires once; an open alarm whose condition ended is sent once
    as ``resolved``, unless its subject is gone (a branch torn down).
    A new controller version than the one remembered fires
    ``controller-restarted``.
    """
    current = observe(status, now, waiting)
    opened: dict[str, Any] = _dict(memory.get("open"))
    out: list[Alarm] = []
    for key, alarm in current.items():
        known = _dict(opened.get(key))
        if known.get("fingerprint") == alarm.fingerprint:
            continue
        out.append(alarm)
        opened[key] = {
            "event": alarm.event,
            "subject": alarm.subject,
            "reason": alarm.reason,
            "fingerprint": alarm.fingerprint,
            "since": alarm.since,
            "next": alarm.next,
        }
    present = _subjects(status)
    for key in sorted(set(opened) - set(current)):
        known = _dict(opened.pop(key))
        event, subject = str(known.get("event")), str(known.get("subject"))
        kind = "nix:" if event.startswith("nix-") else "env:"
        if event.startswith(("env-", "nix-")) and f"{kind}{subject}" not in present:
            continue  # the subject is gone: nothing recovered
        out.append(
            Alarm(
                event, subject, str(known.get("reason")), "piceli gitops status",
                state="resolved",
            )
        )  # fmt: skip
    memory["open"] = opened
    version = _dict(status.get("controller")).get("version")
    if isinstance(version, str) and version:
        before = memory.get("version")
        if isinstance(before, str) and before and before != version:
            out.append(
                Alarm(
                    "controller-restarted", "controller", "controller-upgraded",
                    "piceli gitops status", fingerprint=version,
                    detail=f"the controller runs piceli {version} (was {before})",
                )
            )  # fmt: skip
        memory["version"] = version
    return out


# ---------------------------------------------------------------- sending


@dataclass
class AlarmSender:
    """Sends a poll's alarms to the targets the controller sends itself
    (ntfy, webhooks); ``Notify.desktop()`` is left to ``piceli gitops
    alarms --follow``. Never raises: a failed send is logged and remembered."""

    alarms: Alarms
    log: Callable[[str], None] = lambda _m: None

    @property
    def targets(self) -> list[Any]:
        from piceli.train.notify import DesktopNotify

        return [
            item for item in self.alarms.targets if not isinstance(item, DesktopNotify)
        ]

    def run(
        self,
        memory: dict[str, Any],
        status: Mapping[str, Any],
        now: float,
        at: str,
    ) -> list[Alarm]:
        """Find and send this poll's alarms; returns them."""
        from piceli.train.notify import deliver_all

        found = changes(memory, status, now, float(self.alarms.waiting_seconds))
        composition = _dict(status.get("controller")).get("composition")
        failures = list(memory.get("failures") or ())
        for alarm in found:
            self.log(f"alarm {alarm.key} {alarm.state} ({alarm.reason})")
            outcomes = deliver_all(
                self.targets,
                alarm.payload(composition, at),
                self.log,
                message=(alarm.title, alarm.text),
                code=FAILED_CODE,
            )
            for outcome in outcomes:
                if outcome.get("state") == "failed":
                    failures.append(
                        {
                            "target": self.targets[outcome["notify"]].label(),
                            "alarm": alarm.key,
                            "state": alarm.state,
                            "reason": FAILED_CODE,
                            "why": outcome.get("why"),
                            "at": at,
                        }
                    )
                elif outcome.get("state") == "sent":
                    memory["sent"] = int(memory.get("sent") or 0) + 1
                    memory["last_sent_at"] = at
        memory["failures"] = failures[-MAX_FAILURES:]
        return found


def status_view(alarms: Alarms, memory: Mapping[str, Any]) -> dict[str, Any]:
    """``controller.alarms`` of the status: the targets (labels), the
    waiting limit, the open alarms, how many were sent and the failed sends."""
    return {
        "targets": [item.label() for item in alarms.targets],
        "waiting_after_seconds": alarms.waiting_seconds,
        "open": [
            {
                "alarm": key,
                "event": entry.get("event"),
                "subject": entry.get("subject"),
                "reason": entry.get("reason"),
                "since": entry.get("since"),
            }
            for key, entry in sorted(_dict(memory.get("open")).items())
        ],
        "sent": int(memory.get("sent") or 0),
        "last_sent_at": memory.get("last_sent_at"),
        "failures": list(memory.get("failures") or ()),
    }
