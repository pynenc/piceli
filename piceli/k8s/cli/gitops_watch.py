"""Following a GitOps controller from this machine (0.28.0).

- ``piceli gitops alarms [--follow]``: the controller's alarms
  (:mod:`piceli.gitops.alarms`) as they stand, and with ``--follow`` each
  new one as a desktop notification while the command runs.

Importing this module is side-effect free.
"""

from __future__ import annotations

import time
from typing import Annotated, Any

import typer

from piceli.cli_contract import emit_json, reject, say
from piceli.k8s.cli.gitops import (
    ContextOption,
    JsonOption,
    KubeconfigOption,
    NamespaceOption,
    StateDirOption,
    TransportOption,
    _channel,
    _guard,
    app,
)

ALARMS_SCHEMA = "piceli.gitops-alarms.v1"
#: How long an environment may wait before it is an alarm, when the
#: controller declares no ``Alarms`` (seconds).
DEFAULT_WAITING = 600


def parse_timeout(text: str) -> int:
    """A duration (``10m``) in seconds (refuses with ``gitops-request-invalid``)."""
    from piceli.gitops import GitOpsError
    from piceli.gitops.config import parse_duration

    try:
        return parse_duration(text)
    except (GitOpsError, TypeError):
        reject("gitops-request-invalid", "a duration is like 900, 30s, 15m or 1h")


def _waiting_seconds(document: dict[str, Any], given: str | None) -> float:
    if given is not None:
        return float(parse_timeout(given))
    declared = ((document.get("controller") or {}).get("alarms") or {}).get(
        "waiting_after_seconds"
    )
    return float(declared) if isinstance(declared, int | float) else DEFAULT_WAITING


@app.command("alarms")
def alarms(
    kubeconfig: KubeconfigOption = None,
    context: ContextOption = None,
    namespace: NamespaceOption = "piceli-system",
    state_dir: StateDirOption = None,
    transport: TransportOption = "https",
    follow: Annotated[
        bool,
        typer.Option(
            "--follow",
            help="Keep reading the status and raise each new alarm as a desktop "
            "notification (Ctrl-C stops)",
        ),
    ] = False,
    interval: Annotated[
        float,
        typer.Option("--interval", help="Seconds between two status reads", min=1),
    ] = 30.0,
    waiting_after: Annotated[
        str | None,
        typer.Option(
            "--waiting-after",
            help="How long a wait for a build host or an approval may last "
            "(default: the controller's Alarms(waiting_after=), else 10m)",
        ),
    ] = None,
    desktop: Annotated[
        bool,
        typer.Option(
            "--desktop/--no-desktop", help="With --follow: raise desktop notifications"
        ),
    ] = True,
    polls: Annotated[
        int,
        typer.Option(
            "--polls",
            help="With --follow: stop after this many reads (0: never)",
            min=0,
        ),
    ] = 0,
    as_json: JsonOption = False,
) -> None:
    """The controller's alarms: failed or waiting environments, failed Nix
    releases, a composition that does not import, a new version. With
    --follow, each new one also as a desktop notification."""
    from piceli.gitops import GitOpsError
    from piceli.gitops.alarms import FAILED_CODE, changes
    from piceli.train.notify import DesktopNotify, deliver_all

    memory: dict[str, Any] = {}
    seen_failures: set[str] = set()
    found: list[dict[str, Any]] = []
    reads = 0
    unread: str | None = None
    with _channel(state_dir, kubeconfig, context, namespace, transport) as channel:
        while True:
            try:
                with _guard():
                    document = channel.read_status() or {}
                unread = None
            except typer.Exit:
                if not follow:
                    raise
                document = None
            except GitOpsError as error:  # pragma: no cover - _guard maps it
                document, unread = None, error.code
            reads += 1
            if document is not None:
                composition = (document.get("controller") or {}).get("composition")
                now = time.time()
                for alarm in changes(
                    memory, document, now, _waiting_seconds(document, waiting_after)
                ):
                    at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
                    payload = alarm.payload(composition, at)
                    found.append(payload)
                    say(
                        f"{'ok' if alarm.ok else 'ALARM'} {alarm.title.removeprefix('piceli: ')}: {alarm.text}"
                    )
                    if follow and desktop:
                        deliver_all(
                            [DesktopNotify()], payload, say,
                            message=(alarm.title, alarm.text), code=FAILED_CODE,
                        )  # fmt: skip
                sent = (document.get("controller") or {}).get("alarms") or {}
                for failure in sent.get("failures") or ():
                    key = f"{failure.get('at')}:{failure.get('target')}:{failure.get('alarm')}"
                    if key not in seen_failures:
                        seen_failures.add(key)
                        say(
                            f"  the controller could not send {failure.get('alarm')} "
                            f"to {failure.get('target')} at {failure.get('at')} "
                            f"[{FAILED_CODE}]"
                        )
                if not follow:
                    if not found:
                        say("no alarms")
                    if as_json:
                        emit_json(
                            {
                                "schema": ALARMS_SCHEMA,
                                "alarms": found,
                                "controller": sent or None,
                            }
                        )
                    return
            elif unread is None:
                say("  cannot read the controller's status; retrying")
                unread = "unread"
            if polls and reads >= polls:
                if as_json:
                    emit_json({"schema": ALARMS_SCHEMA, "alarms": found})
                return
            try:
                time.sleep(interval)
            except KeyboardInterrupt:  # pragma: no cover - interactive
                return
