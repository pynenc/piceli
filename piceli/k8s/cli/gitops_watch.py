"""Following a GitOps controller from this machine (0.28.0).

- ``piceli gitops alarms [--follow]``: the controller's alarms
  (:mod:`piceli.gitops.alarms`) as they stand, and with ``--follow`` each
  new one as a desktop notification while the command runs.
- :func:`wait_rollout`: what ``piceli gitops enable --wait`` runs after the
  install: progress lines until every environment runs its revision with
  its checks passed, or the first failure.

Exit codes of the waits, for scripts: :data:`EXIT_OK` (0) all good,
:data:`EXIT_FAILED` (1) a failure, :data:`EXIT_TIMEOUT` (2) the time ran
out.

Importing this module is side-effect free.
"""

from __future__ import annotations

import time
from collections.abc import Callable
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

#: Exit codes of ``--wait`` (``gitops enable``, ``promote --from``).
EXIT_OK, EXIT_FAILED, EXIT_TIMEOUT = 0, 1, 2
ALARMS_SCHEMA = "piceli.gitops-alarms.v1"
#: How long an environment may wait before it is an alarm, when the
#: controller declares no ``Alarms`` (seconds).
DEFAULT_WAITING = 600


def parse_timeout(text: str) -> int:
    """``--timeout 15m`` in seconds (refuses with ``gitops-request-invalid``)."""
    from piceli.gitops import GitOpsError
    from piceli.gitops.config import parse_duration

    try:
        return parse_duration(text)
    except (GitOpsError, TypeError):
        reject("gitops-request-invalid", "--timeout must be like 900, 30s, 15m or 1h")


def say_failure(failure: dict[str, Any]) -> None:
    """The first failure of a wait: where, the reason code, the stage, the
    detail and the log tail."""
    where = failure.get("env") or "the composition"
    say(
        f"{where}: failed"
        + (f" in stage {failure['stage']}" if failure.get("stage") else "")
        + f" ({failure.get('reason')})"
        + (f" at {str(failure['commit'])[:12]}" if failure.get("commit") else "")
    )
    if failure.get("detail"):
        say(f"  {failure['detail']}")
    if failure.get("log_tail"):
        say("  log tail:")
        for line in str(failure["log_tail"]).splitlines():
            say(f"    {line}")


def wait_rollout(
    channel: Any,
    *,
    timeout: int,
    interval: float,
    since_poll: str | None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[int, dict[str, Any]]:
    """Wait until every environment runs its revision with its checks
    passed; ``(exit code, body)`` (``piceli.gitops-rollout-wait.v1``)."""
    from piceli.gitops import GitOpsError
    from piceli.gitops.wait import ROLLOUT_SCHEMA, rollout

    started = time.monotonic()
    shown: dict[str, tuple[str, float]] = {}
    state: str | None = None
    body: dict[str, Any] = {}
    unread: str | None = None
    while True:
        try:
            document = channel.read_status()
            unread = None
        except GitOpsError as error:  # the controller may be restarting
            if error.code != unread:
                say(f"  cannot read the controller's status ({error.code}); retrying")
            document, unread = None, error.code
        state, body = rollout(document, since_poll=since_poll)
        waited = time.monotonic() - started
        for name, view in body["envs"].items():
            last = shown.get(name)
            # A line when the state changes, and once a minute while waiting.
            if last is None or last[0] != view["base"] or waited - last[1] >= 60:
                say(f"{name}: {view['text']}")
                shown[name] = (view["base"], waited)
        if state is not None or waited >= timeout:
            break
        sleep(min(interval, max(0.0, timeout - waited)))
    result: dict[str, Any] = {
        "schema": ROLLOUT_SCHEMA,
        "state": state or "timed-out",
        "envs": body.get("envs") or {},
        "failure": body.get("failure"),
        "waited_seconds": round(time.monotonic() - started, 1),
    }
    if state == "deployed":
        say(f"all environments run their revision ({len(result['envs'])})")
        return EXIT_OK, result
    if state == "failed":
        say_failure(result["failure"])
        return EXIT_FAILED, result
    waiting = [n for n, v in result["envs"].items() if v.get("state") != "done"]
    say(
        f"timed out after {timeout}s [gitops-wait-timeout]"
        + (f"; still waiting: {', '.join(waiting)}" if waiting else "")
        + ("" if body.get("fresh") else "; the controller has not polled since")
    )
    result["reason"] = "gitops-wait-timeout"
    return EXIT_TIMEOUT, result


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
