"""``piceli env stop ENV`` and ``piceli env start ENV``: a named environment, on request.

A composition's named environment (``Environment(...)`` in ``infra.py``) is
stopped by the GitOps controller: its workloads run no replicas (claims,
objects and namespace kept) and it is neither planned nor deployed until it
is started again. Both commands only record a request (``stop`` or
``start`` in the ConfigMap ``piceli-gitops-requests``); the controller acts
on its next poll and ``piceli gitops status`` shows the environment
``stopped`` with ``reason: requested``.

The composition can also declare ``Environment(stopped=True)``: the
declaration wins, so ``env start`` of a declared stop is refused
(``gitops-env-stop-declared``) here when the composition says so, and by the
controller otherwise.

The cluster is named with ``--cluster infra.py:CLUSTER`` (its ``piceli
login`` credentials profile, never the current context), or a local
controller with ``--state-dir``.

Importing this module is side-effect free.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from piceli.cli_contract import emit_json, reject, say

EnvArgument = Annotated[
    str,
    typer.Argument(help="The named environment (Environment(...) in the composition)"),
]
ClusterOption = Annotated[
    str | None,
    typer.Option(
        "--cluster",
        help="The composition's Cluster (infra.py:CLUSTER); its credentials "
        "profile reaches the controller",
        show_default=False,
    ),
]
NamespaceOption = Annotated[
    str, typer.Option("--namespace", help="The controller's namespace")
]
StateDirOption = Annotated[
    Path | None,
    typer.Option(
        "--state-dir",
        help="A local controller's state directory instead of a cluster",
    ),
]
TransportOption = Annotated[
    str,
    typer.Option("--transport", help="https, or loopback-http for a local test API"),
]


def _environment(cluster: str, env: str) -> Any:
    """The named environment ``env`` of the composition holding ``cluster``."""
    from piceli.app.render import load_target
    from piceli.envs.model import Environment
    from piceli.infra import Cluster, CompositionError
    from piceli.infra.composition import load_composition

    file, sep, _attr = cluster.rpartition(":")
    if not sep or not file.endswith(".py"):
        reject("cluster-invalid", "--cluster takes infra.py:CLUSTER")
    try:
        declared = load_target(cluster, Path.cwd())
        composition = load_composition(file, Path.cwd())
    except (CompositionError, ImportError, AttributeError, OSError, ValueError):
        say(f"piceli: {cluster} is not a composition's Cluster")
        reject("cluster-invalid")
    if not isinstance(declared, Cluster):
        say(f"piceli: {cluster} is not a piceli.infra.Cluster")
        reject("cluster-invalid")
    found = next(
        (
            item
            for item in composition.environments
            if isinstance(item, Environment) and item.name == env
        ),
        None,
    )
    if found is None:
        say(f"piceli: the composition declares no named environment {env!r}")
        reject("env-not-found")
    return declared, found


def _request(
    env: str,
    *,
    start: bool,
    cluster: str | None,
    namespace: str,
    state_dir: Path | None,
    transport: str,
) -> None:
    from piceli.gitops import GitOpsError
    from piceli.gitops.state import stop_request

    if (cluster is None) == (state_dir is None):
        say("piceli: name the cluster with --cluster infra.py:CLUSTER (or --state-dir)")
        reject("gitops-target-required")
    kubeconfig: Path | None = None
    context: str | None = None
    if cluster is not None:
        from piceli.profiles import ProfileError, resolve

        declared, environment = _environment(cluster, env)
        if start and environment.stopped:
            say(
                f"piceli: {env} is declared Environment(stopped=True); remove the "
                "declaration to start it"
            )
            reject("gitops-env-stop-declared", env=env)
        try:
            credentials = resolve(declared.credentials)
        except ProfileError as error:
            say(f"piceli: {error}")
            reject(error.code)
        kubeconfig, context = credentials.kubeconfig, credentials.context
    try:
        key, body = stop_request(env, start=start, via="cli")
    except GitOpsError as error:
        reject(error.code, str(error))
    from piceli.k8s.cli.gitops import _channel, _guard

    with _channel(state_dir, kubeconfig, context, namespace, transport) as channel:
        with _guard():
            channel.add_request(key, body)
    verb = "start" if start else "stop"
    say(
        f"{verb} of {env} recorded; the controller "
        + (
            "scales it back on its next poll (and deploys its revision when it moved)"
            if start
            else "scales it to zero on its next poll and keeps its claims"
        )
    )
    emit_json({"state": "requested", "request": key, "env": env, "action": verb})


def stop(
    env: EnvArgument,
    cluster: ClusterOption = None,
    namespace: NamespaceOption = "piceli-system",
    state_dir: StateDirOption = None,
    transport: TransportOption = "https",
) -> None:
    """Ask the GitOps controller to stop named environment ENV (no replicas, claims kept).

    While stopped the controller neither plans nor deploys it; `piceli env
    start ENV` ends the stop. Records a request; the controller acts on its
    next poll.
    """
    _request(
        env,
        start=False,
        cluster=cluster,
        namespace=namespace,
        state_dir=state_dir,
        transport=transport,
    )


def start(
    env: EnvArgument,
    cluster: ClusterOption = None,
    namespace: NamespaceOption = "piceli-system",
    state_dir: StateDirOption = None,
    transport: TransportOption = "https",
) -> None:
    """Ask the GitOps controller to start named environment ENV again.

    Scales its workloads back and deploys its current revision when it moved
    while stopped (with its usual approval). Refused for an environment the
    composition declares stopped (gitops-env-stop-declared).
    """
    _request(
        env,
        start=True,
        cluster=cluster,
        namespace=namespace,
        state_dir=state_dir,
        transport=transport,
    )
