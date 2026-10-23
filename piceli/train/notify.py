"""Notifications when a train ends: a command, a webhook, the desktop.

Declare them on the train, ``Train(..., notify=[...])``:

- :meth:`Notify.command`: runs ``argv`` with the run's summary
  (``piceli.train.summary.v1``, :mod:`piceli.train.summary`) as JSON on
  standard input;
- :meth:`Notify.webhook`: ``POST``\\ s the summary as JSON to a URL (given,
  or read from an environment variable so a secret URL stays out of the
  spec), with headers read from environment variables;
- :meth:`Notify.desktop`: a desktop notification with the summary's
  headline (``osascript`` on macOS, ``notify-send`` elsewhere; nothing when
  neither exists).

``when="failure"`` notifies only for a run that did not succeed.
Notifications run once the run's result is written, one after the other,
each within its ``timeout``. A failing notifier never changes the run's
result: it is reported as one warning line with ``train-notify-failed``
and recorded in the run record's ``notify`` list.

Importing this module is side-effect free.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import KW_ONLY, dataclass
from typing import Any, ClassVar, Literal
from urllib.parse import urlsplit

from piceli.train.model import TrainError
from piceli.train.summary import headline

#: Seconds a notifier may take by default.
DEFAULT_TIMEOUT = 10.0
_HEADER = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~-]{1,128}")
_ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")


def _invalid(message: str) -> TrainError:
    return TrainError("train-invalid", message)


@dataclass(frozen=True)
class Notify:
    """One notification sent when a run ends. Build with :meth:`command`,
    :meth:`webhook` or :meth:`desktop`.

    :param when: ``"always"`` (default) or ``"failure"`` (only a run whose
        result is not ``ok``).
    :param timeout: Seconds the notifier may take.
    """

    kind: ClassVar[str] = "notify"

    _: KW_ONLY
    when: Literal["always", "failure"] = "always"
    timeout: float = DEFAULT_TIMEOUT

    def __post_init__(self) -> None:
        if self.when not in {"always", "failure"}:
            raise _invalid('notify when must be "always" or "failure"')
        if not isinstance(self.timeout, int | float) or not 0 < self.timeout <= 300:
            raise _invalid("notify timeout must be between 0 and 300 seconds")
        object.__setattr__(self, "timeout", float(self.timeout))

    @staticmethod
    def command(
        argv: Sequence[str],
        *,
        when: Literal["always", "failure"] = "always",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> CommandNotify:
        """Run ``argv`` (in the caller's directory and environment) with the
        summary JSON on standard input; exit status 0 is success."""
        return CommandNotify(argv=tuple(argv), when=when, timeout=timeout)

    @staticmethod
    def webhook(
        url: str | None = None,
        *,
        url_env: str | None = None,
        headers_env: Mapping[str, str] | None = None,
        when: Literal["always", "failure"] = "always",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> WebhookNotify:
        """``POST`` the summary JSON to ``url`` (or the URL in the environment
        variable ``url_env``). ``headers_env`` maps a header name to the
        environment variable holding its value (``{"Authorization":
        "TRAIN_HOOK_TOKEN"}``); values never enter the spec or the output.
        A 2xx response is success."""
        return WebhookNotify(
            url=url,
            url_env=url_env,
            headers_env=tuple(sorted((headers_env or {}).items())),
            when=when,
            timeout=timeout,
        )

    @staticmethod
    def desktop(
        *,
        when: Literal["always", "failure"] = "always",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> DesktopNotify:
        """A desktop notification with the summary's headline: ``osascript``
        on macOS, ``notify-send`` elsewhere; skipped when neither exists."""
        return DesktopNotify(when=when, timeout=timeout)

    def wanted(self, summary: Mapping[str, Any]) -> bool:
        return self.when == "always" or summary.get("ok") is not True

    def label(self) -> str:
        return self.kind

    def send(self, summary: Mapping[str, Any]) -> str:
        """Send; returns ``sent`` or ``skipped`` (raises on failure)."""
        raise NotImplementedError

    def _fields(self) -> dict[str, Any]:
        return {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "when": self.when,
            "timeout": self.timeout,
            **self._fields(),
        }


@dataclass(frozen=True)
class CommandNotify(Notify):
    """:meth:`Notify.command`."""

    kind: ClassVar[str] = "command"

    argv: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        super().__post_init__()
        if isinstance(self.argv, str) or not all(
            isinstance(item, str) for item in self.argv
        ):
            raise _invalid("Notify.command argv must be a list of strings")
        if not self.argv or not self.argv[0]:
            raise _invalid("Notify.command argv is empty")
        object.__setattr__(self, "argv", tuple(self.argv))

    def label(self) -> str:
        return f"command {os.path.basename(self.argv[0])}"

    def send(self, summary: Mapping[str, Any]) -> str:
        done = subprocess.run(
            list(self.argv),
            input=json.dumps(summary, sort_keys=True).encode(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=self.timeout,
            check=False,
            start_new_session=True,
        )
        if done.returncode != 0:
            raise OSError(f"exit {done.returncode}")
        return "sent"

    def _fields(self) -> dict[str, Any]:
        return {"argv": list(self.argv)}


def _check_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise _invalid("Notify.webhook url must be an http:// or https:// URL")


@dataclass(frozen=True)
class WebhookNotify(Notify):
    """:meth:`Notify.webhook`."""

    kind: ClassVar[str] = "webhook"

    url: str | None = None
    url_env: str | None = None
    headers_env: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        super().__post_init__()
        if (self.url is None) == (self.url_env is None):
            raise _invalid("Notify.webhook needs one of url or url_env")
        if self.url is not None:
            if not isinstance(self.url, str):
                raise _invalid("Notify.webhook url must be a string")
            _check_url(self.url)
        if self.url_env is not None and not (
            isinstance(self.url_env, str) and _ENV.fullmatch(self.url_env)
        ):
            raise _invalid("Notify.webhook url_env must name an environment variable")
        pairs = tuple(self.headers_env)
        for pair in pairs:
            if (
                not isinstance(pair, tuple)
                or len(pair) != 2
                or not isinstance(pair[0], str)
                or not _HEADER.fullmatch(pair[0])
                or not isinstance(pair[1], str)
                or not _ENV.fullmatch(pair[1])
            ):
                raise _invalid(
                    "Notify.webhook headers_env maps header names to environment "
                    "variable names"
                )
        object.__setattr__(self, "headers_env", tuple(sorted(pairs)))

    def label(self) -> str:
        if self.url_env:
            return f"webhook ${self.url_env}"
        return f"webhook {urlsplit(self.url or '').hostname}"

    def send(self, summary: Mapping[str, Any]) -> str:
        from urllib3 import PoolManager, Timeout

        url = self.url or os.environ.get(self.url_env or "", "").strip()
        if not url:
            raise OSError("url unset")
        _check_url(url)
        headers = {"Content-Type": "application/json"}
        for name, variable in self.headers_env:
            value = os.environ.get(variable)
            if value is None:
                raise OSError("header unset")
            headers[name] = value
        pool = PoolManager()
        try:
            response = pool.request(
                "POST",
                url,
                body=json.dumps(summary, sort_keys=True).encode(),
                headers=headers,
                timeout=Timeout(total=self.timeout),
                retries=False,
                redirect=False,
            )
        finally:
            pool.clear()
        if not 200 <= response.status < 300:
            raise OSError(f"status {response.status}")
        return "sent"

    def _fields(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "url_env": self.url_env,
            "headers_env": dict(self.headers_env),
        }


def desktop_argv(
    title: str, body: str, *, platform: str | None = None
) -> list[str] | None:
    """The command showing a desktop notification here, or ``None``.

    The text goes in as arguments, never into a script: ``osascript`` reads
    them with ``on run argv``.
    """
    platform = platform or sys.platform
    if platform == "darwin":
        tool = shutil.which("osascript")
        if tool is None:
            return None
        return [
            tool,
            "-e",
            "on run argv",
            "-e",
            "display notification (item 2 of argv) with title (item 1 of argv)",
            "-e",
            "end run",
            title,
            body,
        ]
    tool = shutil.which("notify-send")
    return None if tool is None else [tool, "--app-name=piceli", title, body]


@dataclass(frozen=True)
class DesktopNotify(Notify):
    """:meth:`Notify.desktop`."""

    kind: ClassVar[str] = "desktop"

    def send(self, summary: Mapping[str, Any]) -> str:
        verdict = "succeeded" if summary.get("ok") else "needs attention"
        argv = desktop_argv(
            f"piceli train {summary.get('train')} {verdict}", headline(summary)
        )
        if argv is None:
            return "skipped"
        done = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=self.timeout,
            check=False,
        )
        if done.returncode != 0:
            raise OSError(f"exit {done.returncode}")
        return "sent"


_KINDS: Mapping[str, type[Notify]] = {
    "command": CommandNotify,
    "webhook": WebhookNotify,
    "desktop": DesktopNotify,
}


def notify_from_dict(data: Mapping[str, Any]) -> Notify:
    """The notifier :meth:`Notify.to_dict` wrote."""
    kind = _KINDS.get(str(data.get("kind")))
    if kind is None:
        raise _invalid(f"unknown notify kind {data.get('kind')!r}")
    common: dict[str, Any] = {
        "when": data.get("when", "always"),
        "timeout": data.get("timeout", DEFAULT_TIMEOUT),
    }
    if kind is CommandNotify:
        return CommandNotify(argv=tuple(data.get("argv") or ()), **common)
    if kind is WebhookNotify:
        return WebhookNotify(
            url=data.get("url"),
            url_env=data.get("url_env"),
            headers_env=tuple(sorted((data.get("headers_env") or {}).items())),
            **common,
        )
    return DesktopNotify(**common)


def send_all(
    notifiers: Sequence[Notify],
    summary: Mapping[str, Any],
    warn: Callable[[str], None],
) -> list[dict[str, Any]]:
    """Send every wanted notification; one outcome per notifier. Never raises."""
    outcomes: list[dict[str, Any]] = []
    for index, notifier in enumerate(notifiers):
        label = notifier.label()
        if not notifier.wanted(summary):
            outcomes.append(
                {"notify": index, "kind": notifier.kind, "state": "not-wanted"}
            )
            continue
        try:
            state = notifier.send(summary)
        except Exception as error:  # a notifier never changes the run's result
            why = (
                "timeout"
                if isinstance(error, subprocess.TimeoutExpired | TimeoutError)
                else type(error).__name__
            )
            warn(f"warning: notification {label} failed ({why}) [train-notify-failed]")
            outcomes.append(
                {
                    "notify": index,
                    "kind": notifier.kind,
                    "state": "failed",
                    "reason": "train-notify-failed",
                }
            )
            continue
        outcomes.append({"notify": index, "kind": notifier.kind, "state": state})
    return outcomes
