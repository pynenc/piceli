"""From contracts to a deployable pipeline: one environment of a composition.

:func:`check_needs` refuses an environment whose components need something
it does not provide (``component-need-unmet``); :func:`environment_app` turns
the environment's components (their contracts at the environment's commits,
and the image each one runs) into a typed :class:`piceli.App`;
:func:`environment_pipeline` wraps it in a :class:`piceli.Pipeline` whose
``EnvConfig`` holds exactly this environment, so ``env_up`` plans, approves
and applies it like any 0.14 environment (namespace, placement, quota,
isolation of branch environments).

Mapping (see ``docs/components.md``):

- a component is a Deployment, or a StatefulSet with one ``ClaimTemplate``
  per ``retained`` volume (claims are never pruned: data outlives the
  workload); other volumes are RAM-backed scratch (``emptyDir``);
- ``ports`` become named container ports and a Service named after the
  component; ``health.ready`` its readiness probe; ``health.check`` and
  ``upgrade_check`` its pre-rollout checks;
- ``settings`` become environment variables (``log_level`` →
  ``LOG_LEVEL``), the composition's settings overriding the contract's;
- a ``secret:NAME`` need mounts the Secret at ``/run/secrets/NAME``.

Importing this module is side-effect free.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from piceli.envs.model import BranchEnvironments, EnvConfig, Environment
from piceli.infra import Component, CompositionError
from piceli.infra.composition import Composition, EnvItem
from piceli.infra.contract import ComponentContract

SECRETS_DIR = "/run/secrets"


def env_var(key: str) -> str:
    """``log_level`` → ``LOG_LEVEL``."""
    return re.sub(r"[^A-Za-z0-9]+", "_", key).strip("_").upper()


def check_needs(
    composition: Composition,
    env: EnvItem,
    contracts: Mapping[str, ComponentContract],
) -> None:
    """Every required need of the environment's components is met.

    A ``component:NAME`` (or bare ``NAME``) need is met when the environment
    runs that component; ``secret:NAME`` when the environment lists the
    Secret in ``secrets=``. A need ending in ``?`` is optional.

    :raises CompositionError: ``component-need-unmet``.
    """
    running = {item.name for item in composition.stack_of(env)}
    for name in sorted(contracts):
        for need in contracts[name].needs:
            if need.optional:
                continue
            met = (
                need.name in running
                if need.kind == "component"
                else need.name in env.secrets
            )
            if not met:
                where = "stack" if need.kind == "component" else "secrets"
                raise CompositionError(
                    "component-need-unmet",
                    f"environment {env.name!r}: {name!r} needs {need}, which its "
                    f"{where} does not provide",
                    details={
                        "environment": env.name,
                        "component": name,
                        "need": str(need),
                    },
                )


def settings_of(
    component: Component, contract: ComponentContract, env: EnvItem
) -> dict[str, str]:
    """Contract settings, then the composition's (component, then environment)."""
    merged = dict(contract.settings)
    merged.update(component.settings)
    merged.update(env.settings.get(component.name, {}))
    return {env_var(key): value for key, value in sorted(merged.items())}


def _probe(contract: ComponentContract) -> Any:
    from piceli.app import Probe

    ports = dict(contract.ports)
    health = contract.health
    if health.ready_command is not None:
        return Probe.exec(health.ready_command)
    if health.ready is None:
        return None
    method, target = health.ready
    if method == "TCP":
        return Probe.tcp(ports[target])
    port = ports.get("http", next(iter(ports.values())))
    return Probe.http(target, port)


def environment_app(
    composition: Composition,
    env: EnvItem,
    contracts: Mapping[str, ComponentContract],
    images: Mapping[str, str],
) -> Any:
    """The typed app of ``env``: one workload (and Service) per component.

    :param contracts: Each component's contract at the environment's commit.
    :param images: Each component's image, pinned by digest.
    """
    from piceli.app import (
        App,
        ClaimTemplate,
        ContainerPort,
        MemoryVolume,
        SecretVolume,
        Security,
        ServicePort,
        UpgradeCheck,
    )

    check_needs(composition, env, contracts)
    app = App(composition.name)
    workloads: dict[str, Any] = {}
    for component in composition.stack_of(env):
        contract = contracts[component.name]
        volumes: dict[str, Any] = {}
        for volume in contract.volumes:
            if volume.retained:
                volumes[volume.path] = ClaimTemplate(volume.name, size=volume.size)
            else:
                volumes[volume.path] = MemoryVolume(
                    name=volume.name, size_limit=volume.size
                )
        for need in contract.needs:
            if need.kind == "secret" and need.name in env.secrets:
                volumes[f"{SECRETS_DIR}/{need.name}"] = SecretVolume(
                    need.name, default_mode=0o440
                )
        image = contract.image
        fields: dict[str, Any] = {
            "image": images[component.name],
            "env": settings_of(component, contract, env),
            "ports": [ContainerPort(port=n, name=p) for p, n in contract.ports],
            "ready": _probe(contract),
            "volumes": volumes or None,
            "security": Security(
                run_as_non_root=image.uid != 0 or None,
                run_as_user=image.uid,
                run_as_group=image.gid,
                fs_group=image.gid,
                allow_privilege_escalation=False,
            ),
            "labels": {"piceli.io/component": component.name},
        }
        if component.source is None and image.cmd is not None:
            fields["command"] = list(image.cmd)
        if contract.retained:
            workload = app.stateful_set(
                component.name, headless=False, service_name=component.name, **fields
            )
        else:
            workload = app.deployment(component.name, **fields)
        workloads[component.name] = workload
        if contract.ports:
            app.service(
                workload,
                ports=[
                    ServicePort(port=n, target_port=p, name=p)
                    for p, n in contract.ports
                ],
            )
        if contract.health.check is not None or contract.upgrade_check is not None:
            app.pre_rollout(
                workload,
                contract.health.check,
                upgrade=None
                if contract.upgrade_check is None
                else UpgradeCheck(contract.upgrade_check),
            )
    for component in composition.stack_of(env):
        for need in contracts[component.name].needs:
            if need.kind == "component" and need.name in workloads:
                app.depends(workloads[component.name], on=workloads[need.name])
    return app


def env_config(composition: Composition, env: EnvItem) -> EnvConfig:
    """The ``EnvConfig`` of one environment of the composition."""
    if isinstance(env, BranchEnvironments):
        return env.env_config()
    single = Environment(
        env.name,
        namespace=env.namespace,
        on_nodes=env.on_nodes,
        quota=env.quota,
        auto_approve=env.auto_approve,
    )
    prefix = (composition.name + "-")[:39].rstrip("-") + "-"
    if not re.fullmatch(r"[a-z]", prefix[0]):
        prefix = "env-" + prefix
    return EnvConfig(
        prefix=prefix[:40],
        main_branch=env.name,
        branches=(env.name,),
        environments=[single],
    )


def environment_pipeline(
    composition: Composition,
    env: EnvItem,
    contracts: Mapping[str, ComponentContract],
    images: Mapping[str, str],
    *,
    kubeconfig: Path,
    context: str,
    state_dir: Path,
    transport: str = "https",
) -> Any:
    """The pipeline that deploys ``env`` (images given, nothing to build)."""
    from piceli.app import App
    from piceli.approval_policy import ApprovalPolicy
    from piceli.pipeline.model import Pipeline, Target

    # No contracts: a pipeline for env_down/env_stop (nothing to render).
    app = (
        environment_app(composition, env, contracts, images)
        if contracts
        else App(composition.name)
    )
    namespace = env.namespace if isinstance(env, Environment) else env.prefix + "main"
    target = Target(
        kubeconfig,
        context=context,
        namespace=namespace,
        transport=transport,
    )
    return Pipeline(
        app,
        target,
        envs=env_config(composition, env),
        auto_approve=ApprovalPolicy() if env.auto_approve else None,
        state_dir=state_dir / env.name,
    )
