"""``piceli targets``: one status view of every target (0.25.0).

Every GitOps environment and every NixHost, with its deployed commit or
tag, the product commit it pins, its checks and its state, in one table
(stderr) or one ``piceli.targets.v1`` object (``--json``). The controller
is read once, as ``piceli gitops status`` reads it (by default the
composition's home cluster, with its ``piceli login`` profile, as ``gitops
enable`` finds it); the hosts over SSH, in
parallel, each within ``--timeout``. One unreachable source never fails
the view (:mod:`piceli.targets`).

Importing this module is side-effect free.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from piceli.cli_contract import EXIT_FAILED, emit_json, reject, say
from piceli.k8s.cli.gitops import (
    ContextOption,
    JsonOption,
    KubeconfigOption,
    NamespaceOption,
    StateDirOption,
    TransportOption,
)


def _controller(
    state_dir: Path | None,
    kubeconfig: Path | None,
    context: str | None,
    namespace: str,
    transport: str,
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """``(gitops status body, reason, detail)``: the body, or why not."""
    from piceli.gitops import GitOpsError
    from piceli.k8s.cli.gitops import open_channel, status_body

    try:
        with open_channel(state_dir, kubeconfig, context, namespace, transport) as ch:
            return status_body(ch, namespace), None, None
    except GitOpsError as error:
        return None, error.code, str(error)
    except Exception as error:  # an unreachable cluster never fails the view
        return None, "gitops-cluster-failed", type(error).__name__


def _home(module: Any) -> tuple[Any, Path | None, str | None, str | None]:
    """``(cluster, kubeconfig, context, why not)`` of the composition's home
    cluster, from its ``piceli login`` profile, as ``gitops enable`` finds it."""
    from piceli.infra import CompositionError
    from piceli.infra.composition import composition_from
    from piceli.k8s.cli.gitops import home_credentials
    from piceli.profiles import ProfileError

    try:
        cluster = composition_from(module).cluster
    except CompositionError:
        return None, None, None, None
    if cluster is None:
        return None, None, None, None
    try:
        kubeconfig, context = home_credentials(cluster)
    except ProfileError as error:
        return cluster, None, None, error.code
    except ValueError:
        return cluster, None, None, "profile-invalid"
    return cluster, kubeconfig, context, None


def _hosts(
    hosts: dict[str, Any], infra_values: list[Any], timeout: float
) -> tuple[dict[str, Any], dict[str, str | None]]:
    """Each declared host's agent status (in parallel), and its address."""
    from functools import partial

    from piceli.hosts.model import HostError
    from piceli.hosts.remote import read_status
    from piceli.infra import Infrastructure
    from piceli.infra.machines.model import InfraError
    from piceli.k8s.cli.host import ssh_for
    from piceli.targets import read_hosts

    infras = [value for value in infra_values if isinstance(value, Infrastructure)]
    reads: dict[str, Any] = {}
    failed: dict[str, Any] = {}
    where: dict[str, str | None] = {}
    for name, host in sorted(hosts.items()):
        infra = next((i for i in infras if host.server in i.servers), None)
        try:
            ssh, known = ssh_for(host, infra)
        except (HostError, InfraError) as error:
            failed[name] = {"reason": error.code, "detail": str(error)}
            where[name] = None
            continue
        where[name] = ssh.address
        reads[name] = partial(read_status, ssh, known_hosts=known, timeout=timeout)
    return {**read_hosts(reads, timeout + 5), **failed}, where


def collect(
    composition: str | None,
    infra: str | None,
    kubeconfig: Path | None = None,
    context: str | None = None,
    namespace: str = "piceli-system",
    state_dir: Path | None = None,
    transport: str = "https",
    timeout: float = 20.0,
) -> tuple[dict[str, Any], dict[str, Any] | None, str | None, str | None]:
    """``(piceli.targets.v1 document, controller body, reason, detail)``:
    what ``piceli targets`` shows (also ``piceli trains --composition``)."""
    from piceli.k8s.cli.host import load_module_of
    from piceli.targets import build, declared

    values: list[Any] = []
    infra_values: list[Any] = []
    for ref in (composition, infra):
        if ref is not None:
            loaded = list(vars(load_module_of(ref)).values())
            values += loaded
            if ref == infra:
                infra_values = loaded
    found = declared(values)
    body, reason, detail = None, "gitops-target-required", "no controller named"
    if composition is not None and kubeconfig is None and state_dir is None:
        # The home cluster's controller, with its `piceli login` profile.
        home, kubeconfig, context, missing = _home(load_module_of(composition))
        if missing is not None:
            reason = missing
            detail = (
                f"no credential profile {home.credentials} for cluster {home.name}: "
                f"run piceli login {home.credentials} --kubeconfig FILE --context NAME"
            )
    if kubeconfig is not None or state_dir is not None:
        body, reason, detail = _controller(
            state_dir, kubeconfig, context, namespace, transport
        )
    hosts, where = _hosts(found["hosts"], infra_values or values, timeout)
    document = build(
        body, controller_reason=reason, controller_detail=detail,
        targets=found, hosts=hosts, where=where,
    )  # fmt: skip
    return document, body, reason, detail


def targets(
    composition: Annotated[
        str | None,
        typer.Option(
            "--composition",
            help="MODULE (or file.py) declaring the environments and NixReleases",
        ),
    ] = None,
    infra: Annotated[
        str | None,
        typer.Option(
            "--infra",
            help="MODULE declaring NixHosts and the Infrastructure of their "
            "servers (applied addresses, pinned host keys)",
        ),
    ] = None,
    kubeconfig: KubeconfigOption = None,
    context: ContextOption = None,
    namespace: NamespaceOption = "piceli-system",
    state_dir: StateDirOption = None,
    transport: TransportOption = "https",
    timeout: Annotated[
        float,
        typer.Option("--timeout", help="Seconds to wait for each host", min=1),
    ] = 20.0,
    as_json: JsonOption = False,
) -> None:
    """Every environment and NixHost: commit or tag, pinned product commit, checks, state."""
    from piceli.targets import exit_code, table

    if composition is None and infra is None and kubeconfig is None and not state_dir:
        reject(
            "targets-input-required",
            "name the targets: --composition MODULE, --infra MODULE, or the "
            "controller (--kubeconfig FILE --context NAME, or --state-dir)",
        )
    document, body, reason, detail = collect(
        composition,
        infra,
        kubeconfig,
        context,
        namespace,
        state_dir,
        transport,
        timeout,
    )
    for line in table(document):
        say(line)
    if body is None:
        say(f"controller not read: {detail} [{reason}]")
    if as_json:
        emit_json(document)
    if exit_code(document):
        raise typer.Exit(EXIT_FAILED)


def register(root: typer.Typer) -> None:
    root.command("targets")(targets)
