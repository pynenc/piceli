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
) -> None:
    """Pin the newest commit an environment verified in flake.lock, commit,
    tag and push (plan, then --approve DIGEST)."""
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
        emit_json(document)
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
    if as_json:
        emit_json(document)
