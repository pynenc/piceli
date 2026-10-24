"""``piceli env up|down BRANCH --cluster infra.py:CLUSTER``: a preview, now (0.28.0).

With ``--cluster`` (or a local controller's ``--state-dir``) instead of
``--pipeline``, ``env up`` and ``env down`` ask a composition's GitOps
controller: they record a ``preview`` request (ConfigMap
``piceli-gitops-requests``), which wakes the controller within a few
seconds (see :mod:`piceli.infra.previews`). ``--wait`` then follows the
request in the published status: ``env up`` ends when the branch runs its
head with its checks passed and prints the preview's URL once (exit 0),
reports the failure (exit 1) or the timeout (``gitops-wait-timeout``,
exit 2), as ``piceli gitops enable --wait`` does.

Importing this module is side-effect free.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer

from piceli.cli_contract import EXIT_OK, emit_json, reject, say

EXIT_FAILED = 1
EXIT_TIMEOUT = 2


def wait_preview(
    channel: Any,
    branch: str,
    request: str,
    *,
    action: str,
    timeout: int,
    interval: float,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[int, dict[str, Any]]:
    """Follow one preview request until it is answered; ``(exit code, body)``."""
    from piceli.gitops import GitOpsError
    from piceli.gitops.wait import PREVIEW_SCHEMA, preview

    started = time.monotonic()
    shown: str | None = None
    unread: str | None = None
    state: str | None = None
    body: dict[str, Any] = {}
    while True:
        try:
            document = channel.read_status()
            unread = None
        except GitOpsError as error:  # the controller may be restarting
            if error.code != unread:
                say(f"  cannot read the controller's status ({error.code}); retrying")
            document, unread = None, error.code
        state, body = preview(document, branch, request, action)
        line = str(body.get("base") or body.get("text"))
        if line != shown and state is None:
            say(f"{branch}: {body.get('text')}")
            shown = line
        waited = time.monotonic() - started
        if state is not None or waited >= timeout:
            break
        sleep(min(interval, max(0.0, timeout - waited)))
    result = {
        "schema": PREVIEW_SCHEMA,
        **{k: v for k, v in body.items() if k != "base"},
        "state": state or "timed-out",
        "waited_seconds": round(time.monotonic() - started, 1),
    }
    if state == "ready":
        say(f"{branch}: {body.get('text')}")
        if result.get("url"):
            say(f"preview ready: {result['url']}")
        else:
            say(
                "preview ready (no URL declared: Environment.per_branch(url=...)); "
                f"reach it with piceli access {branch} --cluster infra.py:CLUSTER"
            )
        return EXIT_OK, result
    if state == "removed":
        say(f"preview of {branch} removed")
        return EXIT_OK, result
    if state == "failed":
        say(
            f"{branch}: failed ({result.get('reason')})"
            + (f": {result['detail']}" if result.get("detail") else "")
        )
        failure = result.get("failure") or {}
        if failure.get("log_tail"):
            say("  log tail:")
            for item in str(failure["log_tail"]).splitlines():
                say(f"    {item}")
        return EXIT_FAILED, result
    say(f"timed out after {timeout}s [gitops-wait-timeout]: {body.get('text')}")
    result["reason"] = "gitops-wait-timeout"
    return EXIT_TIMEOUT, result


def run(
    branch: str,
    *,
    action: str,
    cluster: str | None,
    state_dir: Path | None,
    namespace: str,
    transport: str,
    wait: bool,
    timeout: str,
    interval: float,
    refused: list[str],
) -> None:
    """Record the request and, with ``--wait``, follow it (exits)."""
    from piceli.gitops import GitOpsError
    from piceli.gitops.state import preview_request
    from piceli.k8s.cli.gitops import _channel, _guard
    from piceli.k8s.cli.gitops_watch import parse_timeout

    if refused:
        reject(
            "gitops-request-invalid",
            f"{', '.join(refused)} only apply with --pipeline; the controller "
            "deploys the branch's head with its own images",
        )
    if (cluster is None) == (state_dir is None):
        say("piceli: name the cluster with --cluster infra.py:CLUSTER (or --state-dir)")
        reject("gitops-target-required")
    seconds = parse_timeout(timeout)
    try:
        key, body = preview_request(branch, action=action)
    except GitOpsError as error:
        reject(error.code, str(error))
    kubeconfig: Path | None = None
    context: str | None = None
    if cluster is not None:
        from piceli.k8s.cli.env_stop import credentials_of

        kubeconfig, context = credentials_of(cluster)
    with _channel(state_dir, kubeconfig, context, namespace, transport) as channel:
        with _guard():
            channel.add_request(key, body)
        say(
            f"preview {action} of {branch} recorded; the controller acts within "
            "seconds"
            + ("" if wait else " (follow it with --wait or piceli gitops status)")
        )
        requested = {
            "state": "requested",
            "request": key,
            "branch": branch,
            "action": action,
        }
        if not wait:
            emit_json(requested)
            return
        code, result = wait_preview(
            channel,
            branch,
            key,
            action=action,
            timeout=seconds,
            interval=interval,
        )
    emit_json(result)
    if code:
        raise typer.Exit(code)
