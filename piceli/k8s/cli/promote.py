"""``piceli promote HOST --from ENV``: pin a commit a GitOps environment
verified (0.25.0; ``piceli promote`` without ``--from`` asks the controller
to deploy a branch's commit, :func:`piceli.k8s.cli.gitops.promote`).

See :mod:`piceli.infra.promote` (the plan and its application) and
:mod:`piceli.infra.promotion` (the gate). :func:`gated_release` and
:func:`read_history` are shared with ``piceli dev nix build --publish
--release HOST``, the hand-run publish the gate also covers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

from piceli.cli_contract import EXIT_APPROVAL, emit_json, fail, reject, say


def gated_release(composition_path: Path, host: str) -> tuple[Any, Any]:
    """``(ReleaseTarget, Composition)`` of the gated NixRelease of ``host``."""
    from piceli.cli_contract import describe_user_error
    from piceli.errors import ERRORS
    from piceli.infra.composition import load_composition

    try:
        composition = load_composition(composition_path)
    except Exception as error:  # the module raised while importing
        code = getattr(error, "code", None)
        if isinstance(code, str) and code in ERRORS:
            reject(code, str(error))
        reject(
            "composition-invalid",
            f"importing {composition_path} failed: {describe_user_error(error)}",
        )
    target = next(
        (item for item in composition.nix_releases if item.name == host), None
    )
    if target is None:
        reject("promote-invalid", f"the composition declares no NixRelease of {host}")
    if target.gate is None:
        reject(
            "promote-invalid",
            f"NixRelease {host} has no gate=Promotion(input=, environment=)",
        )
    return target, composition


def read_history(
    composition: Any,
    *,
    kubeconfig: Path | None,
    context: str | None,
    state_dir: Path | None,
    namespace: str,
) -> dict[str, Any]:
    """The controller's ``piceli.gitops-history.v1``, as ``gitops status``
    reaches the controller: ``--state-dir``, ``--kubeconfig``/``--context``,
    else the composition's cluster's credential profile. Fails closed:
    anything unreadable refuses (``promotion-history-unreachable``)."""
    from piceli.gitops import GitOpsError
    from piceli.gitops.history import SCHEMA
    from piceli.gitops.state import ConfigMapChannel, DirectoryChannel

    def unreachable(why: str) -> Any:
        reject(
            "promotion-history-unreachable",
            f"the history of the composition's controller could not be read: {why}",
        )

    document: Any = None
    try:
        if state_dir is not None:
            if kubeconfig is not None:
                unreachable("give --state-dir or --kubeconfig, not both")
            document = DirectoryChannel(state_dir).read_history()
        else:
            if kubeconfig is None:
                if getattr(composition, "cluster", None) is None:
                    unreachable(
                        "the composition declares no cluster; give --kubeconfig"
                    )
                from piceli.profiles import ProfileError, resolve

                try:
                    found = resolve(composition.cluster.credentials)
                except (ProfileError, ValueError):
                    unreachable("the cluster's credential profile is unknown here")
                kubeconfig, context = found.kubeconfig, found.context
            if not context:
                unreachable("--kubeconfig needs --context")
            from piceli.gitops.install import connect

            assert kubeconfig is not None and context is not None
            with connect(kubeconfig, context) as api:
                document = ConfigMapChannel(api.client, namespace).read_history()
    except (GitOpsError, OSError, ValueError) as error:
        unreachable(type(error).__name__)
    except Exception as error:  # the API client's errors: never a pass
        unreachable(type(error).__name__)
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        unreachable("no history published")
    return document


def promote_host(
    host: str,
    source_env: str,
    *,
    composition: Path,
    repo: Path,
    bump: str,
    approve: str | None,
    kubeconfig: Path | None,
    context: str | None,
    state_dir: Path | None,
    namespace: str,
    as_json: bool,
    wait: dict[str, Any] | None = None,
    machines: str | None = None,
) -> None:
    """Pin the newest commit an environment verified in flake.lock, commit,
    tag and push (plan, then --approve DIGEST). ``wait`` (0.28.0, ``--wait``):
    ``{"timeout", "interval"}``, then wait until the host runs the tag with
    its checks passed (exit 1: a failure, 2: timeout). ``machines`` (0.31.0,
    ``--machines``): the module (``MODULE[:ATTR]``) declaring the
    ``Infrastructure`` of the host's server, when the composition does not
    import it."""
    from piceli.infra import promote as promotion_plan
    from piceli.infra.promotion import PromotionError

    target, found_composition = gated_release(composition, host)
    gate = target.gate
    if source_env != gate.environment:
        reject(
            "promote-invalid",
            f"NixRelease {host} is gated on environment {gate.environment}, not "
            f"{source_env}",
        )
    history = read_history(
        found_composition, kubeconfig=kubeconfig, context=context,
        state_dir=state_dir, namespace=namespace,
    )  # fmt: skip
    try:
        found = promotion_plan.plan(
            host=host, pattern=target.pattern, gate=gate, history=history,
            repo=repo, bump=bump,
        )  # fmt: skip
    except PromotionError as error:
        reject(error.code, str(error), **error.detail)
    body = found.get("plan")
    command = (
        f"piceli promote {host} --from {source_env} --composition {composition} "
        f"--repo {repo}"
        + (f" --bump {bump}" if bump != "patch" else "")
        + (f" --machines {machines}" if machines is not None else "")
        + (f" --state-dir {state_dir}" if state_dir is not None else "")
        + (
            f" --kubeconfig {kubeconfig} --context {context}"
            if kubeconfig is not None
            else ""
        )
    )
    document: dict[str, Any] = {"schema": promotion_plan.RESULT_SCHEMA, **found}
    document.pop("lock", None)
    if body is None:  # current, already tagged
        say(f"{host}: {gate.input} already pins {found['pinned']['commit'][:12]}; "
            f"tag {found['tag']} points at it")  # fmt: skip
        if wait is None:
            emit_json(document)
            return
        _wait_and_emit(
            document, host, str(found["tag"]), composition, found_composition,
            wait, kubeconfig=kubeconfig, context=context, state_dir=state_dir,
            namespace=namespace, machines=machines,
        )  # fmt: skip
        return
    digest = found["digest"]
    document["approve_command"] = f"{command} --approve {digest}"
    if approve is None:
        pinned = body["pinned"]
        change = body.get("change")
        if change is None:
            say(f"{host}: {gate.input} already pins {pinned['commit'][:12]}; no tag "
                "of the host points at it")  # fmt: skip
            say(
                f"  approving tags {body['base'][:12]} {body['tag']} and pushes the tag"
            )
        else:
            say(f"{host}: pin {gate.input} {str(change['from'])[:12]} -> "
                f"{change['to'][:12]} ({change['method']})")  # fmt: skip
            say(f"  verified on {pinned['environment']} by run "
                f"{pinned['verified_run']} at {pinned['verified_at']}")  # fmt: skip
            say(f"  commit: {body['commit_message'].splitlines()[0]}")
        say(f"  tag {body['tag']} (after {body['previous_tag'] or 'none'}); push to "
            f"{body['remote']} {body['branch']}")  # fmt: skip
        say("approve with:")
        say(f"  {document['approve_command']}")
        emit_json(document)
        if found["state"] == "current":
            return
        raise typer.Exit(EXIT_APPROVAL)
    if approve != digest:
        reject(
            "promote-plan-changed",
            "the promotion plan changed since it was approved; review it again",
        )
    try:
        result = promotion_plan.apply(found, repo, say=say)
    except PromotionError as error:
        fail(error.code, str(error), **error.detail)
    document.update(state="promoted", **result)
    document.pop("approve_command", None)
    say(f"{host}: promoted {document['tag']} at {document['commit'][:12]}")
    if wait is None:
        if as_json:
            emit_json(document)
        return
    _wait_and_emit(
        document, host, str(document["tag"]), composition, found_composition,
        wait, kubeconfig=kubeconfig, context=context, state_dir=state_dir,
        namespace=namespace, machines=machines,
    )  # fmt: skip


def _wait_and_emit(
    document: dict[str, Any],
    host: str,
    tag: str,
    composition: Path,
    found_composition: Any,
    wait: dict[str, Any],
    *,
    kubeconfig: Path | None,
    context: str | None,
    state_dir: Path | None,
    namespace: str,
    machines: str | None = None,
) -> None:
    code, result = wait_host_release(
        host,
        tag,
        composition=composition,
        machines=machines,
        controller=lambda: _release_record(
            found_composition,
            host,
            kubeconfig=kubeconfig,
            context=context,
            state_dir=state_dir,
            namespace=namespace,
        ),
        timeout=wait["timeout"],
        interval=wait["interval"],
    )
    emit_json({**document, "wait": result})
    if code:
        raise typer.Exit(code)


def _release_record(
    composition: Any,
    host: str,
    *,
    kubeconfig: Path | None,
    context: str | None,
    state_dir: Path | None,
    namespace: str,
) -> dict[str, Any] | None:
    """The controller's ``nix_releases.<host>`` (``None``: unreadable; the
    host's own status still decides the wait)."""
    from piceli.gitops.state import ConfigMapChannel, DirectoryChannel

    try:
        if state_dir is not None:
            document = DirectoryChannel(state_dir).read_status()
        else:
            if kubeconfig is None:
                from piceli.profiles import resolve

                found = resolve(composition.cluster.credentials)
                kubeconfig, context = found.kubeconfig, found.context
            from piceli.gitops.install import connect

            assert kubeconfig is not None and context is not None
            with connect(kubeconfig, context) as api:
                document = ConfigMapChannel(api.client, namespace).read_status()
    except Exception:  # unreadable: the host decides
        return None
    record = ((document or {}).get("nix_releases") or {}).get(host)
    return dict(record) if isinstance(record, dict) else None


def release_failure(record: dict[str, Any] | None, tag: str) -> dict[str, Any] | None:
    """The failure of the controller's publish of ``tag``, if it failed."""
    if not record or record.get("tag") != tag:
        return None
    if record.get("state") not in ("failed", "rejected", "cancelled"):
        return None
    failure = record.get("failure") or {}
    refusal = record.get("refusal") or {}
    host = failure.get("host")
    detail = "; ".join(
        str(item)
        for item in (
            f"build host {host}" if host else None,
            failure.get("reason"),
            refusal.get("message"),
        )
        if item
    )
    tail = str(failure.get("log_tail") or "").rstrip("\n").splitlines()[-20:]
    return {
        "env": record.get("name") or None,
        "reason": record.get("reason") or "gitops-nix-release-failed",
        "stage": "publish",
        "commit": record.get("commit"),
        "detail": detail or None,
        "log_tail": "\n".join(tail) or None,
    }


def _release_text(record: dict[str, Any] | None, tag: str) -> str | None:
    if not record or record.get("tag") != tag:
        return None
    state = record.get("state")
    claimed = (record.get("claimed") or {}).get("host")
    return {
        "approval-required": "the publish waits for approval: "
        + str(record.get("approve_command") or "piceli gitops approve"),
        "waiting-build": "publishing (waiting for a build host)",
        "publishing": f"publishing (build host {claimed})" if claimed else "publishing",
        "published": "published; the host has not run it yet",
    }.get(str(state), str(state))


def _host_failure(current: dict[str, Any], reason: str) -> dict[str, Any]:
    attempt = current.get("last_attempt") or {}
    stages = [s for s in attempt.get("stages") or () if s.get("state") == "failed"]
    checks = [
        str(c.get("name"))
        for c in attempt.get("checks") or ()
        if c.get("state") != "passed"
    ]
    tail = attempt.get("log_tail") or attempt.get("tail")
    return {
        "env": current.get("host"),
        "reason": reason,
        "stage": stages[0].get("name") if stages else None,
        "commit": attempt.get("commit"),
        "detail": ("failing checks: " + ", ".join(checks)) if checks else None,
        "log_tail": "\n".join(str(tail).splitlines()[-20:]) if tail else None,
    }


def wait_host_release(
    host: str,
    tag: str,
    *,
    composition: Path,
    controller: Any,
    timeout: int,
    interval: float,
    sleep: Any = None,
    machines: str | None = None,
) -> tuple[int, dict[str, Any]]:
    """Wait until ``host`` runs ``tag`` with its checks passed: ``(exit
    code, body)``. Reads the controller's publish of the tag (a failed
    publish is the first failure) and the host's status over SSH (as
    ``piceli host wait``)."""
    import time

    from piceli.hosts.model import HostError
    from piceli.hosts.remote import outcome, read_status
    from piceli.k8s.cli.gitops_watch import (
        EXIT_FAILED,
        EXIT_OK,
        EXIT_TIMEOUT,
        say_failure,
    )

    sleep = sleep or time.sleep
    ssh, known = release_ssh(host, composition, machines)
    started = time.monotonic()
    body: dict[str, Any] = {"schema": "piceli.promote-wait.v1", "host": host,
                            "tag": tag, "target": ssh.address}  # fmt: skip
    last: tuple[str, float] | None = None
    unread: str | None = None
    while True:
        waited = time.monotonic() - started
        record = controller()
        failure = release_failure(record, tag)
        text = _release_text(record, tag)
        if failure is None:
            try:
                current = read_status(ssh, known_hosts=known)
                unread = None
            except HostError as error:
                if error.code != unread:
                    say(
                        f"  cannot read {ssh.address}'s status ({error.code}); retrying"
                    )
                current, unread = {}, error.code
            state, reason = outcome(current, tag) if current else ("waiting", None)
            if state == "deployed":
                say(f"{host}: running {tag}, checks passed")
                return EXIT_OK, {**body, "state": "deployed",
                                 "waited_seconds": round(waited, 1)}  # fmt: skip
            if state == "failed":
                failure = _host_failure(current, str(reason))
                failure["env"] = host
            elif current.get("in_progress"):
                text = f"deploying {tag}"
        if failure is not None:
            say_failure(failure)
            return EXIT_FAILED, {**body, "state": "failed", "failure": failure,
                                 "waited_seconds": round(waited, 1)}  # fmt: skip
        line = text or "waiting for the publish"
        if last is None or last[0] != line or waited - last[1] >= 60:
            from piceli.gitops.wait import duration

            say(f"{host}: {line} {duration(waited)}".rstrip())
            last = (line, waited)
        if waited >= timeout:
            say(f"timed out after {timeout}s [gitops-wait-timeout]")
            return EXIT_TIMEOUT, {**body, "state": "timed-out",
                                  "reason": "gitops-wait-timeout",
                                  "waited_seconds": round(waited, 1)}  # fmt: skip
        sleep(min(interval, max(0.0, timeout - waited)))


def _declared_values(composition: Path, kind: type) -> list[Any]:
    """The ``kind`` values of the composition module and of every module it
    imported from its directory (``machines.py`` beside ``infra.py``)."""
    import sys

    directory = composition.resolve().parent
    found: dict[int, Any] = {}
    for module in list(sys.modules.values()):
        file = getattr(module, "__file__", None)
        if not file:
            continue
        try:
            inside = Path(file).resolve().is_relative_to(directory)
        except (OSError, ValueError):
            continue
        if not inside or "site-packages" in Path(file).parts:
            continue
        for value in list(vars(module).values()):
            if isinstance(value, kind):
                found[id(value)] = value
    return list(found.values())


def release_ssh(
    host: str, composition: Path, machines: str | None
) -> tuple[Any, Path | None]:
    """``(SshTarget, known_hosts)`` of the gated NixRelease's host (0.31.0).

    The host is the ``NixRelease``'s own ``NixHost``; the ``Infrastructure``
    of its server (applied address, pinned host keys) is the one
    ``--machines`` names, else the one the composition or a module it
    imports from its directory declares. Without a declared NixRelease of
    ``host``, ``piceli host``'s rules apply (an SSH target)."""
    from piceli.hosts.model import HostError
    from piceli.infra import Infrastructure
    from piceli.infra.machines.model import InfraError
    from piceli.infra.nix_releases import NixRelease
    from piceli.k8s.cli.host import _infrastructure, _target, ssh_for

    releases = {
        id(item.host): item.host
        for item in _declared_values(composition, NixRelease)
        if getattr(item.host, "name", None) == host
    }
    if len(releases) != 1:
        _found, ssh, known = _target(host, machines or str(composition))
        return ssh, known
    declared = next(iter(releases.values()))
    infra = None
    server = declared.server
    if not isinstance(server, str):
        if machines is not None:
            infra = _infrastructure(machines, server)
        else:
            found = {
                item.name: item
                for item in _declared_values(composition, Infrastructure)
                if server in item.servers
            }
            if len(found) > 1 or (not found and server.ssh.address is None):
                reject(
                    "infra-not-found",
                    f"the composition imports {len(found)} Infrastructure(s) with "
                    f"server {server.name}: give --machines MODULE[:ATTR]",
                )
            infra = next(iter(found.values()), None)
    try:
        return ssh_for(declared, infra)
    except (HostError, InfraError) as error:
        reject(error.code, str(error))
