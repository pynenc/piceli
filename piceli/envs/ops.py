"""Bring branch environments up and down, seed them, list them.

Every function takes the declared :class:`~piceli.pipeline.Pipeline` (with
``envs=EnvConfig(...)``) and a branch name. Changes follow the plan-hash
approval: without ``approve`` (or an owner policy that allows it) a function
returns ``{"state": "approval-required", "env_hash": ...}`` and changes
nothing; ``approve=<env_hash>`` runs exactly that plan.

Importing this module is side-effect free.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from piceli.envs.model import (
    ENV_BRANCH_ANNOTATION,
    ENV_BRANCH_LABEL,
    ENV_NAME_LABEL,
    ENV_OF_LABEL,
    BranchEnv,
    EnvConfig,
    EnvError,
    EnvStatus,
)
from piceli.pipeline.errors import PipelineError

if TYPE_CHECKING:
    from piceli.envs.cluster import EnvCluster
    from piceli.pipeline.model import Pipeline

RESULT_SCHEMA = "piceli.env-result.v1"
PLAN_SCHEMA = "piceli.env-plan.v1"
LIST_SCHEMA = "piceli.envs.v1"
Say = Callable[[str], None]


def _quiet(_line: str) -> None:
    return None


def _now(now: datetime | None = None) -> str:
    moment = now or datetime.now(UTC)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _hash(body: Mapping[str, Any]) -> str:
    text = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


# ------------------------------------------------------------------ mapping


def env_config(pipeline: Pipeline) -> EnvConfig:
    """The pipeline's :class:`EnvConfig` (``env-not-configured`` without one)."""
    config = getattr(pipeline, "envs", None)
    if config is None:
        raise EnvError(
            "env-not-configured",
            f"pipeline {pipeline.name!r} declares no envs=EnvConfig(...)",
        )
    if pipeline.needs_environment:
        raise EnvError(
            "env-not-configured",
            "per-branch environments need a pipeline with one target (select "
            "the environment in a module attribute: pipeline.for_environment(name))",
        )
    return config


def main_namespace(pipeline: Pipeline) -> str:
    config = env_config(pipeline)
    declared = config.named(config.main_branch)
    if declared is not None:
        return declared.namespace
    return config.main_namespace or pipeline.target.namespace


def _implicit_main(config: EnvConfig, name: str) -> bool:
    """The 0.13 main environment (``main_namespace``), not a declared one."""
    return config.is_main(name) and config.named(name) is None


def namespace_for(pipeline: Pipeline, branch: str) -> str:
    """The namespace of ``branch`` (see :meth:`EnvConfig.namespace_for`).

    :raises EnvError: ``env-not-configured``, ``env-branch-not-allowed``
        (the branch matches no ``branches`` pattern), ``env-branch-invalid``.
    """
    config = env_config(pipeline)
    if not config.allows(branch):
        raise EnvError(
            "env-branch-not-allowed",
            f"branch {branch!r} matches none of the environment patterns "
            f"{list(config.branches)}",
        )
    return config.namespace_for(branch, main_namespace(pipeline))


_DIGEST_ONLY = re.compile(r"sha256:[0-9a-f]{64}")


def pull_reference(pipeline: Pipeline, image: str, digest: str) -> str:
    """Where the nodes pull build ``image`` with ``digest`` from (the delivery's registry)."""
    from piceli.pipeline.compose import registry_prefix
    from piceli.pipeline.model import NodeLoopbackRegistry, Registry

    strategy = pipeline.deliver
    if isinstance(strategy, NodeLoopbackRegistry):
        prefix = strategy.repository or pipeline.app.name
        return f"127.0.0.1:{strategy.port}/{prefix}/{image}@{digest}"
    if isinstance(strategy, Registry):
        from piceli.artifacts.registry import RegistryTarget

        host = (
            strategy.node_registry or RegistryTarget.parse(strategy.url + "/x").registry
        )
        prefix = registry_prefix(strategy.url)
        return f"{host}/{prefix + '/' if prefix else ''}{image}@{digest}"
    raise EnvError(
        "env-image-invalid",
        f"image {image!r} is given by digest only and the pipeline has no "
        "registry delivery to pull it from; give repository@sha256:…",
    )


def _entry_reference(pipeline: Pipeline, name: str, entry: Any) -> str:
    if isinstance(entry, str):
        if _DIGEST_ONLY.fullmatch(entry):
            return pull_reference(pipeline, name, entry)
        return entry
    if isinstance(entry, Mapping):
        delivered = entry.get("delivered")
        if isinstance(delivered, Mapping) and isinstance(
            delivered.get("pull_ref"), str
        ):
            return str(delivered["pull_ref"])
        if isinstance(entry.get("pull_ref"), str):
            return str(entry["pull_ref"])
        digest = entry.get("manifest_digest") or entry.get("digest")
        if isinstance(digest, str) and _DIGEST_ONLY.fullmatch(digest):
            return pull_reference(pipeline, name, digest)
    raise EnvError(
        "env-image-invalid", f"image {name!r} has no digest or pull reference"
    )


def receipt_images(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """The image table of a build receipt, pushed record or plain mapping.

    Accepts ``{"images": {name: ref | {digest, pull_ref?}}}`` (an ``env
    push`` record, or a plain table), a host-build receipt
    (``outputs.images.<name>`` with ``digest`` and ``delivered.pull_ref``) and
    a cluster build receipt (``delivered.<platform>.<name>.pull_ref``; the
    first of ``platforms``).
    """
    delivered = receipt.get("delivered")
    if isinstance(delivered, Mapping) and delivered:
        platforms = [p for p in receipt.get("platforms") or () if p in delivered]
        table = delivered[platforms[0] if platforms else next(iter(delivered))]
        if isinstance(table, Mapping):
            return dict(table)
    outputs = receipt.get("outputs")
    if isinstance(outputs, Mapping) and isinstance(outputs.get("images"), Mapping):
        return dict(outputs["images"])
    table = receipt.get("images")
    if isinstance(table, str):
        try:
            table = json.loads(table)
        except ValueError:
            table = None
    if isinstance(table, Mapping):
        return dict(table)
    raise EnvError("env-image-invalid", "the build receipt has no image table")


def _images(
    pipeline: Pipeline,
    digests: Mapping[str, str] | None,
    build_receipt: Mapping[str, Any] | None,
) -> dict[str, str]:
    from piceli.pipeline.model import pinned

    table: dict[str, Any] = {}
    if build_receipt is not None:
        table.update(receipt_images(build_receipt))
    table.update(dict(digests or {}))
    images = {
        str(name): _entry_reference(pipeline, str(name), entry)
        for name, entry in table.items()
    }
    for name, reference in images.items():
        if not pinned(reference):
            raise EnvError(
                "env-image-invalid",
                f"image {name!r} is not pinned by digest (repository@sha256:<64 hex>)",
            )
    return dict(sorted(images.items()))


def env_pipeline(
    pipeline: Pipeline,
    branch: str,
    *,
    digests: Mapping[str, str] | None = None,
    build_receipt: Mapping[str, Any] | None = None,
    api_endpoints: tuple[tuple[str, int], ...] = (),
) -> Pipeline:
    """The pipeline of one environment.

    For a branch: its namespace, its own state directory
    (``<state_dir>/branches/<namespace>``), no build, delivery or restore
    points (images come from ``digests`` or a ``build_receipt``: ``{"images":
    {name: reference}}``), and the isolation of :mod:`piceli.envs.isolation`
    at render time. For the main branch: the pipeline itself, or, with
    images, the same pipeline using them instead of building.

    :raises EnvError: ``env-not-configured``, ``env-branch-not-allowed``,
        ``env-branch-invalid``, ``env-image-invalid``.
    """
    from piceli.pipeline.compose import mirror_references, pin_alias

    config = env_config(pipeline)
    if not config.allows(branch):
        raise EnvError(
            "env-branch-not-allowed",
            f"branch {branch!r} matches none of the environment patterns "
            f"{list(config.branches)}",
        )
    main_ns = main_namespace(pipeline)
    namespace = config.namespace_for(branch, main_ns)
    main = _implicit_main(config, branch)
    fixed = config.named(branch)
    images = _images(pipeline, digests, build_receipt)
    if main and not images:
        return pipeline
    alias = pin_alias(pipeline) if pipeline.deliver is not None else None
    node = pipeline.target.nodes.get(alias) if alias is not None else None
    placement: dict[str, Any] = {}
    if fixed is not None:
        placement = {
            "stack": fixed.stack,
            "on_nodes": fixed.on_nodes,
            "quota": fixed.quota,
            "environment": fixed.name,
            "prebuilt": bool(images),
        }
    elif not main:
        placement = {"stack": config.branch_stack, "on_nodes": config.branch_nodes}
    env = BranchEnv(
        branch=branch,
        namespace=namespace,
        config=config,
        main_namespace=main_ns,
        isolated=not config.is_fixed(branch),
        images=images,
        pin_node=node.name if node is not None else None,
        mirrors=mirror_references(pipeline) if pipeline.deliver is not None else {},
        api_endpoints=tuple(api_endpoints),
        **placement,
    )
    derived = copy.copy(pipeline)
    derived.branch_env = env
    if fixed is not None:
        # A named environment deploys like main into its own namespace: it
        # builds unless images are given, keeps restore points (in main's
        # store, so branches can seed from it) and its own local state.
        if images:
            derived.builds = ()
            derived.deliver = None
        target = copy.copy(pipeline.target)
        object.__setattr__(target, "namespace", namespace)
        object.__setattr__(target, "namespace_uid", None)
        derived._target = target
        derived.state_dir = pipeline.state_dir / "environments" / fixed.name
        if pipeline.restore_points is not None:
            derived.restore_points = pipeline.restore_points.model_copy(
                update={"directory": str(pipeline.restore_point_directory)}
            )
        return derived
    derived.builds = ()
    derived.deliver = None
    if not main:
        target = copy.copy(pipeline.target)
        object.__setattr__(target, "namespace", namespace)
        object.__setattr__(target, "namespace_uid", None)
        derived._target = target
        derived.state_dir = pipeline.state_dir / "branches" / namespace
        derived.restore_points = None
    return derived


# ------------------------------------------------------------------ cluster


def default_cluster(pipeline: Pipeline) -> EnvCluster:
    """An :class:`EnvCluster` through the target's explicit kubeconfig and context."""
    from piceli.cli_contract import error_code
    from piceli.envs.cluster import EnvCluster
    from piceli.k8s.ops.provider_factory import api_client_from_kubeconfig

    target = pipeline.target
    try:
        client = api_client_from_kubeconfig(
            target.kubeconfig.absolute(),
            target.context,
            transport=target.transport,  # type: ignore[arg-type]
            exec_policy=target.exec_policy(),
        )
    except (OSError, ValueError) as error:
        raise EnvError(
            error_code(error, "env-cluster-unavailable"), str(error)
        ) from None
    return EnvCluster(client, request_seconds=float(target.request_seconds))


def branch_label(branch: str) -> str:
    """The ``piceli.io/env-branch`` label value (the slug ``env push`` uses too)."""
    from piceli.artifacts.cluster_build import slug

    return slug(branch, 63)


def _owned(namespace: Mapping[str, Any] | None, app: str, branch: str) -> bool:
    """This app's environment of ``branch``, or one ``piceli env push`` created for it.

    ``env push`` creates the namespace before the first ``env up`` with the
    Piceli managed-by label and the branch label only; ``env up`` then adopts
    it (adds ``piceli.io/env-of``).
    """
    labels = ((namespace or {}).get("metadata") or {}).get("labels") or {}
    if labels.get(ENV_OF_LABEL) == app:
        return True
    return (
        ENV_OF_LABEL not in labels
        and labels.get("app.kubernetes.io/managed-by") == "piceli"
        and (
            labels.get(ENV_BRANCH_LABEL) == branch_label(branch)
            or labels.get(GITOPS_ENV_LABEL) == _gitops_label(branch)
        )
    )


#: The label the GitOps controller puts on a namespace it prepares.
GITOPS_ENV_LABEL = "piceli.io/gitops-env"


def _gitops_label(branch: str) -> str:
    """The controller's ``piceli.io/gitops-env`` value for ``branch``."""
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", branch)[:63].strip("-._")
    return value or "branch"


def _adopted(namespace: Mapping[str, Any] | None) -> bool:
    labels = ((namespace or {}).get("metadata") or {}).get("labels") or {}
    return ENV_OF_LABEL in labels


def _branch_of(namespace: Mapping[str, Any]) -> str | None:
    annotations = (namespace.get("metadata") or {}).get("annotations") or {}
    value = annotations.get(ENV_BRANCH_ANNOTATION)
    return value if isinstance(value, str) else None


def _pushed(record: Mapping[str, Any] | None, namespace: Mapping[str, Any]) -> str:
    """When an env was last pushed (its record, else the namespace's creation)."""
    value = (record or {}).get("pushed_at")
    if isinstance(value, str) and value:
        return value
    return str((namespace.get("metadata") or {}).get("creationTimestamp") or "")


def plan_budget(
    cluster: Any, pipeline: Pipeline, namespace: str
) -> list[dict[str, Any]]:
    """The branch environments to stop so ``namespace`` fits in ``max_envs``.

    Running environments other than ``namespace``, least recently pushed
    first. Empty when ``namespace`` already runs or the budget has room.
    """
    config = env_config(pipeline)
    running: list[dict[str, Any]] = []
    for item in cluster.namespaces(pipeline.name):
        name = str((item.get("metadata") or {}).get("name"))
        record = cluster.record(name) or {}
        if record.get("state") == "stopped":
            continue
        if name == namespace:
            return []
        running.append(
            {
                "namespace": name,
                "branch": _branch_of(item) or record.get("branch"),
                "pushed_at": _pushed(record, item),
            }
        )
    excess = len(running) - config.max_envs + 1
    if excess <= 0:
        return []
    running.sort(key=lambda entry: (entry["pushed_at"], entry["namespace"]))
    return running[:excess]


def stop_env(
    cluster: Any, namespace: str, *, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Scale every Deployment and StatefulSet of ``namespace`` to zero; record it."""
    record = dict(cluster.record(namespace) or {})
    stopped = list(record.get("stopped") or ())
    known = {item.get("workload") for item in stopped}
    for workload in cluster.workloads(namespace):
        kind = str(workload.get("kind"))
        name = str((workload.get("metadata") or {}).get("name"))
        replicas = (workload.get("spec") or {}).get("replicas")
        replicas = 1 if replicas is None else int(replicas)
        if not replicas:
            continue
        cluster.scale(namespace, kind, name, 0)
        if f"{kind}/{name}" not in known:
            stopped.append(
                {
                    "workload": f"{kind}/{name}",
                    "kind": kind,
                    "name": name,
                    "replicas": replicas,
                }
            )
    record.update({"state": "stopped", "stopped_at": _now(now), "stopped": stopped})
    cluster.write_record(namespace, record)
    return stopped


def _start_stopped(
    cluster: Any, namespace: str, record: Mapping[str, Any]
) -> list[str]:
    """Scale back what the budget stopped (the release may keep replicas at zero)."""
    started = []
    live = {
        f"{item.get('kind')}/{(item.get('metadata') or {}).get('name')}": item
        for item in cluster.workloads(namespace)
    }
    for item in record.get("stopped") or ():
        workload = live.get(str(item.get("workload")))
        if workload is None or (workload.get("spec") or {}).get("replicas"):
            continue
        cluster.scale(namespace, item["kind"], item["name"], int(item["replicas"]))
        started.append(str(item["workload"]))
    return started


# ------------------------------------------------------------------- seeding


def _latest_point(
    pipeline: Pipeline, namespace: str
) -> tuple[Path, dict[str, Any]] | None:
    """The newest verified restore point of ``namespace`` in the main pipeline's store."""
    from piceli.restore.store import points

    root = pipeline.restore_point_directory
    for _directory, record in points(root):
        if record.get("state") == "verified" and record.get("namespace") == namespace:
            return root, record
    return None


def _seed_source(pipeline: Pipeline, branch: str, source: str) -> dict[str, Any]:
    config = env_config(pipeline)
    if config.is_fixed(branch):
        raise EnvError(
            "env-main-protected",
            "the claims of main and of named environments are never seeded or "
            "overwritten by env commands",
        )
    if not config.is_fixed(source):
        raise EnvError(
            "env-seed-no-restore-point",
            f"only the main branch ({config.main_branch}) and named environments "
            "keep restore points to seed from",
        )
    source_ns = config.namespace_for(source, main_namespace(pipeline))
    found = _latest_point(pipeline, source_ns)
    if found is None:
        raise EnvError(
            "env-seed-no-restore-point",
            f"there is no verified restore point of namespace {source_ns}; take one "
            "with a deploy of main (restore_points=RestorePoints())",
        )
    root, record = found
    from piceli.restore.store import RECORD, file_sha256

    return {
        "source": source,
        "namespace": source_ns,
        "point": record["id"],
        "record_sha256": file_sha256(root / str(record["id"]) / RECORD),
        "root": root,
    }


def _run_seed(
    pipeline: Pipeline,
    derived: Pipeline,
    seed: Mapping[str, Any],
    *,
    restore_cluster: Any = None,
    say: Say = _quiet,
    plan_only: bool = False,
) -> dict[str, Any]:
    from piceli.restore.model import RestorePointError, RestorePoints
    from piceli.restore.runner import plan_restore, restore

    cluster = restore_cluster
    if cluster is None:
        from piceli.pipeline.backend import Backend

        cluster = Backend().restore_cluster(derived.target, say=say)
    settings = pipeline.restore_points or RestorePoints()
    try:
        planned = plan_restore(
            cluster,
            Path(seed["root"]),
            str(seed["point"]),
            target=derived.target.identity(),
            source_namespace=str(seed["namespace"]),
        )
        if plan_only:
            return planned
        return restore(
            cluster,
            Path(seed["root"]),
            planned,
            image=settings.image,
            timeout_seconds=settings.timeout_seconds,
            run_as_user=settings.run_as_user,
            say=say,
        )
    except RestorePointError as error:
        raise EnvError(
            error.code, str(error), failed=error.failed, details=error.details
        ) from None
    finally:
        if restore_cluster is None:
            cluster.close()


def seed_env(
    pipeline: Pipeline,
    branch: str,
    source: str = "main",
    *,
    approve: str | None = None,
    approve_if_policy: bool = False,
    cluster: Any = None,
    restore_cluster: Any = None,
    say: Say = _quiet,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Restore ``source``'s latest restore point into ``branch``'s claims.

    Plans the restore (read-only; its ``restore_hash`` is the approval);
    with the approval (or ``approve_if_policy`` and ``EnvConfig.auto_approve``)
    stops the env's writers, empties and restores each claim, verifies it
    in the cluster and starts the writers again. Never writes main's claims.

    :raises EnvError: ``env-main-protected``, ``env-seed-no-restore-point``,
        ``env-not-found``, ``env-plan-changed`` or a ``restore-point-*`` code.
    """
    config = env_config(pipeline)
    source = source or config.seed_from or config.main_branch
    seed = _seed_source(pipeline, branch, source)
    derived = env_pipeline(pipeline, branch)
    envs = cluster or default_cluster(pipeline)
    try:
        if envs.namespace(derived.target.namespace) is None:
            raise EnvError(
                "env-not-found",
                f"branch {branch!r} has no environment; run piceli env up first",
            )
        planned = _run_seed(
            pipeline,
            derived,
            seed,
            restore_cluster=restore_cluster,
            say=say,
            plan_only=True,
        )
        body = {
            "schema": RESULT_SCHEMA,
            "branch": branch,
            "namespace": derived.target.namespace,
            "source": {k: seed[k] for k in ("source", "namespace", "point")},
            "claims": [item["claim"] for item in planned["claims"]],
            "writers": list(planned["writers"]),
            "env_hash": planned["restore_hash"],
        }
        if approve is None and not (approve_if_policy and config.auto_approve):
            return {**body, "state": "approval-required"}
        if approve is not None and approve != planned["restore_hash"]:
            raise EnvError(
                "env-plan-changed",
                "the approved hash is not the current seed plan; plan again",
            )
        receipt = _run_seed(
            pipeline, derived, seed, restore_cluster=restore_cluster, say=say
        )
        record = dict(envs.record(derived.target.namespace) or {})
        record["seeded_from"] = {**body["source"], "at": _now(now)}
        envs.write_record(derived.target.namespace, record)
        return {**body, "state": "seeded", "receipt": receipt}
    finally:
        if cluster is None:
            envs.close()


# ---------------------------------------------------------------------- up


def env_up(
    pipeline: Pipeline,
    branch: str,
    *,
    commit: str | None = None,
    digests: Mapping[str, str] | None = None,
    build_receipt: Mapping[str, Any] | None = None,
    approve: str | None = None,
    approve_if_policy: bool = False,
    plan_only: bool = False,
    wait: bool = False,
    seed_from: str | None = None,
    cluster: Any = None,
    runner_options: Mapping[str, Any] | None = None,
    say: Say = _quiet,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Deploy ``branch`` into its environment (created when absent).

    Plans the environment: namespace to create, branch environments the
    budget stops, the deploy's combined plan and the seed; its ``env_hash``
    is the approval. Runs with ``approve=<env_hash>``, or with
    ``approve_if_policy`` when ``EnvConfig.auto_approve`` allows a branch or
    the pipeline's ``auto_approve`` policy allows the release plan (the only
    way for the main branch, which is never stopped or created).

    ``wait=True`` refuses (``env-budget-full``) instead of stopping another
    environment when the budget is full. ``seed_from`` (or
    ``EnvConfig.seed_from`` for a new environment) restores that branch's
    latest restore point into the new claims after the deploy.

    Returns ``{"schema": "piceli.env-result.v1", "state": ...}`` with state
    ``planned``, ``approval-required`` or ``ready``.

    :raises EnvError: registered ``env-*`` codes; :class:`PipelineError` from
        the deploy itself.
    """
    from piceli.pipeline.compose import model_fingerprint
    from piceli.pipeline.runner import PipelineRunner

    config = env_config(pipeline)
    namespace = namespace_for(pipeline, branch)
    implicit = _implicit_main(config, branch)
    named = config.named(branch)
    main = config.is_fixed(branch)
    envs = cluster or default_cluster(pipeline)
    try:
        live = envs.namespace(namespace)
        if implicit and live is None:
            raise EnvError(
                "env-main-namespace-missing",
                f"the main namespace {namespace} does not exist; env commands "
                "never create it",
            )
        if not main and live is not None:
            if not _owned(live, pipeline.name, branch):
                raise EnvError(
                    "env-namespace-not-managed",
                    f"namespace {namespace} exists and is not an environment of "
                    f"{pipeline.name}",
                )
            if _branch_of(live) not in {None, branch}:
                raise EnvError(
                    "env-namespace-collision",
                    f"namespace {namespace} belongs to another branch with the "
                    "same name once slugged; rename the branch",
                )
        pushed: dict[str, Any] | None = None
        if not digests and build_receipt is None and not main and live is not None:
            pushed = envs.pushed(namespace, branch)
            if pushed is not None:
                build_receipt = pushed
                commit = commit or pushed.get("commit")
        derived = env_pipeline(
            pipeline,
            branch,
            digests=digests,
            build_receipt=build_receipt,
            api_endpoints=envs.api_endpoints()
            if config.allow_api and not main and not config.is_fixed(branch)
            else (),
        )
        if getattr(derived, "prune", None) is None:
            # An environment converges on its declaration: what an earlier
            # release created and this one no longer declares is deleted in
            # the same plan (claims, Secrets and retained objects are kept).
            derived = copy.copy(derived)
            derived.prune = True
        stop = [] if main else plan_budget(envs, pipeline, namespace)
        if stop and wait:
            raise EnvError(
                "env-budget-full",
                f"{config.max_envs} branch environment(s) already run; the next "
                f"to stop would be {stop[0]['namespace']}",
                details={"would_stop": stop},
            )
        source = seed_from or (config.seed_from if live is None and not main else None)
        seed = _seed_source(pipeline, branch, source) if source else None
        runner = PipelineRunner(derived, say=say, **dict(runner_options or {}))
        fresh = live is None
        combined = None
        if not fresh:
            with runner.locked(), runner.sources():
                combined = runner.plan("checks")
        env_plan = {
            "schema": PLAN_SCHEMA,
            "app": pipeline.name,
            "branch": branch,
            "namespace": namespace,
            "main": config.is_main(branch),
            **({"environment": named.name} if named is not None else {}),
            "create_namespace": fresh,
            "adopt_namespace": not fresh and not main and not _adopted(live),
            "stop": [item["namespace"] for item in stop],
            "seed": None
            if seed is None
            else {
                k: seed[k] for k in ("source", "namespace", "point", "record_sha256")
            },
            "images": dict(derived.branch_env.images) if derived.branch_env else {},
            # A new namespace has nothing to plan against yet: the approval
            # covers the rendered model, and after the namespace is created
            # the deploy may only create objects (env-plan-changed otherwise).
            "combined_hash": None if combined is None else combined.combined_hash,
            **({"model": model_fingerprint(derived)} if fresh else {}),
        }
        env_hash = _hash(env_plan)
        body: dict[str, Any] = {
            "schema": RESULT_SCHEMA,
            **{k: v for k, v in env_plan.items() if k != "schema"},
            "env_hash": env_hash,
            # The hash the GitOps controller asks the owner to approve.
            "plan_hash": env_hash,
            "commit": commit,
            **({"deploy": combined.to_dict()} if combined is not None else {}),
        }
        if plan_only:
            return {**body, "state": "planned"}
        by_config = config.auto_approve and not main
        if approve is not None:
            if approve != env_hash:
                raise EnvError(
                    "env-plan-changed",
                    "the approved hash is not the environment's current plan; plan again",
                )
        elif not approve_if_policy:
            return {**body, "state": "approval-required"}
        elif not by_config and (stop or pipeline.auto_approve is None):
            return {
                **body,
                "state": "approval-required",
                "reason": "approval-policy-exceeded",
            }
        decision = None
        if approve is None and not by_config and combined is not None:
            decision = runner.policy_decision(combined)
            if not decision.allowed:
                return {
                    **body,
                    "state": "approval-required",
                    "reason": "approval-policy-exceeded",
                    "policy": decision.to_dict(),
                }
        stopped = {}
        for item in stop:
            say(
                f"budget: stopping environment {item['namespace']} (least recently pushed)"
            )
            stopped[item["namespace"]] = stop_env(envs, item["namespace"], now=now)
        labels = {ENV_OF_LABEL: pipeline.name, ENV_BRANCH_LABEL: branch_label(branch)}
        if fresh and named is not None:
            # Not labelled env-of: a named environment is never counted,
            # stopped or listed as a branch environment.
            envs.create_namespace(namespace, {ENV_NAME_LABEL: named.name}, {})
        elif fresh:
            envs.create_namespace(namespace, labels, {ENV_BRANCH_ANNOTATION: branch})
        elif env_plan["adopt_namespace"]:
            envs.label_namespace(namespace, labels, {ENV_BRANCH_ANNOTATION: branch})
        previous = dict(envs.record(namespace) or {})
        record = {
            **previous,
            "branch": branch,
            "namespace": namespace,
            "commit": commit,
            "build": "pushed"
            if pushed is not None
            else ("prebuilt" if env_plan["images"] else "pipeline"),
            "images": env_plan["images"],
            "pushed_at": _now(now),
            "created_at": previous.get("created_at") or _now(now),
            "deploy": "deploying",
            "state": "running",
        }
        envs.write_record(namespace, record)
        with runner.locked(), runner.sources():
            try:
                if combined is None:
                    combined = runner.plan("checks")
                    _only_creates(combined)
                    if approve is None and not by_config:
                        decision = runner.policy_decision(combined)
                        if not decision.allowed:
                            raise EnvError(
                                "approval-policy-exceeded",
                                "the new environment's deploy is outside the owner's "
                                "approval policy: " + ", ".join(decision.violations),
                                details={"policy": decision.to_dict()},
                            )
                result = runner.execute(
                    combined, combined.combined_hash, policy=decision
                )
            except PipelineError:
                envs.write_record(namespace, {**record, "deploy": "failed"})
                raise
        started = (
            _start_stopped(envs, namespace, previous) if previous.get("stopped") else []
        )
        record.update({"deploy": result.get("state"), "stopped": []})
        record.pop("stopped_at", None)
        envs.write_record(namespace, record)
        seeded = None
        if seed is not None:
            say(
                f"seeding {namespace} from {seed['namespace']} restore point {seed['point']}"
            )
            seeded = _run_seed(pipeline, derived, seed, say=say)
            record["seeded_from"] = {
                **{k: seed[k] for k in ("source", "namespace", "point")},
                "at": _now(now),
            }
            envs.write_record(namespace, record)
        return {
            **body,
            "state": "ready",
            "result": result,
            "stopped": stopped,
            "started": started,
            **({"seed": {**body["seed"], "receipt": seeded}} if seeded else {}),
        }
    finally:
        if cluster is None:
            envs.close()


def _only_creates(combined: Any) -> None:
    """A deploy into a namespace created just now may only create objects."""
    release = combined.stages.get("plan") or {}
    other = sorted(
        {
            str(item.get("operation"))
            for item in release.get("changes") or ()
            if item.get("operation") not in {"create", "no-op"}
        }
    )
    if other:
        raise EnvError(
            "env-plan-changed",
            "the new environment's deploy would "
            + ", ".join(other)
            + " objects; plan again and review",
        )


# -------------------------------------------------------------------- down


def env_down(
    pipeline: Pipeline,
    branch: str,
    *,
    approve: str | None = None,
    approve_if_policy: bool = False,
    cluster: Any = None,
    say: Say = _quiet,
) -> dict[str, Any]:
    """Delete ``branch``'s environment: its claims, its namespace, its volumes.

    Refuses the main branch and its namespace always (``env-main-protected``),
    and a namespace that is not this app's environment
    (``env-namespace-not-managed``). The plan lists the claims and the
    volumes bound to them; its ``env_hash`` is the approval. Also removes the
    branch's local state directory. An absent environment is ``absent``.
    """
    config = env_config(pipeline)
    main_ns = main_namespace(pipeline)
    if config.is_fixed(branch):
        raise EnvError(
            "env-main-protected",
            "the environments of main and of named environments are never deleted",
        )
    namespace = config.namespace_for(branch, main_ns)
    if namespace == main_ns:
        raise EnvError("env-main-protected", "the main namespace is never deleted")
    envs = cluster or default_cluster(pipeline)
    try:
        live = envs.namespace(namespace)
        body: dict[str, Any] = {
            "schema": RESULT_SCHEMA,
            "branch": branch,
            "namespace": namespace,
        }
        state_dir = pipeline.state_dir / "branches" / namespace
        if live is None:
            # Gone already (deleted by hand, or an earlier teardown that did
            # not finish): its local state goes too.
            if state_dir.is_dir():
                shutil.rmtree(state_dir, ignore_errors=True)
            return {**body, "state": "absent"}
        if not _owned(live, pipeline.name, branch) or _branch_of(live) not in {
            None,
            branch,
        }:
            raise EnvError(
                "env-namespace-not-managed",
                f"namespace {namespace} is not {pipeline.name}'s environment of "
                f"branch {branch!r}; nothing is deleted",
            )
        claims = sorted(
            str((item.get("metadata") or {}).get("name"))
            for item in envs.claims(namespace)
        )
        volumes = sorted(
            str((item.get("metadata") or {}).get("name"))
            for item in envs.volumes()
            if ((item.get("spec") or {}).get("claimRef") or {}).get("namespace")
            == namespace
        )
        # Cluster-scoped objects the release made for this environment
        # (labelled with its namespace, marked as Piceli's).
        cluster_objects = [
            f"{kind}/{name}" for kind, name in envs.cluster_objects(namespace)
        ]
        delete: dict[str, Any] = {"claims": claims, "volumes": volumes}
        if cluster_objects:
            delete["cluster_objects"] = cluster_objects
        plan = {
            "schema": PLAN_SCHEMA,
            "app": pipeline.name,
            "branch": branch,
            "namespace": namespace,
            "uid": (live.get("metadata") or {}).get("uid"),
            "delete": delete,
        }
        body.update(
            {
                "delete": {**delete, "cluster_objects": cluster_objects},
                "env_hash": _hash(plan),
            }
        )
        if approve is None and not (approve_if_policy and config.auto_approve):
            return {
                **body,
                "state": "approval-required",
                **({"reason": "approval-policy-exceeded"} if approve_if_policy else {}),
            }
        if approve is not None and approve != body["env_hash"]:
            raise EnvError(
                "env-plan-changed",
                "the approved hash is not the environment's current teardown plan",
            )
        for claim in claims:
            envs.delete_claim(namespace, claim)
        for item in cluster_objects:
            kind, _, name = item.partition("/")
            envs.delete_cluster_object(kind, name)
        say(f"deleting namespace {namespace} ({len(claims)} claim(s))")
        envs.delete_namespace(namespace)
        left: list[str] = []
        for volume in volumes:
            try:
                envs.delete_volume(volume)
            except EnvError as error:
                # Deleting volumes is the owner's opt-in (delete_volumes);
                # without it they stay and are reported, not retried.
                if "denied" not in (error.details or {}):
                    raise
                left.append(volume)
        if left:
            say(
                f"kept {len(left)} volume(s) the controller may not delete: "
                + ", ".join(left)
            )
            body["volumes_left"] = left
        if state_dir.is_dir():
            shutil.rmtree(state_dir, ignore_errors=True)
        return {**body, "state": "removed"}
    finally:
        if cluster is None:
            envs.close()


# -------------------------------------------------------------------- stop


def env_stop(
    pipeline: Pipeline,
    branch: str,
    *,
    approve: str | None = None,
    approve_if_policy: bool = False,
    reason: str = "idle",
    named: bool = False,
    cluster: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Scale ``branch``'s environment to zero and keep it (the next ``env up`` starts it).

    The plan lists the workloads to scale; its ``env_hash`` is the approval.
    ``approve_if_policy`` runs it when the owner declared
    ``EnvConfig(idle_stop=...)`` or ``auto_approve=True`` (the GitOps
    controller's idle stop). Never main's; a named environment's only with
    ``named=True`` (the GitOps controller, for a declared
    ``Environment(stopped=True)`` or a ``piceli env stop`` request: the
    owner's decision, so ``approve_if_policy`` runs it), and only in a
    namespace Piceli deployed it to (its environment record).

    :raises EnvError: ``env-main-protected``, ``env-namespace-not-managed``,
        ``env-plan-changed``.
    """
    config = env_config(pipeline)
    by_owner = named and config.named(branch) is not None
    if config.is_fixed(branch) and not by_owner:
        raise EnvError(
            "env-main-protected",
            "main and named environments are never stopped by env commands",
        )
    namespace = config.namespace_for(branch, main_namespace(pipeline))
    envs = cluster or default_cluster(pipeline)
    try:
        live = envs.namespace(namespace)
        body: dict[str, Any] = {
            "schema": RESULT_SCHEMA,
            "branch": branch,
            "namespace": namespace,
        }
        if live is None:
            return {**body, "state": "absent"}
        if not _stoppable(envs, live, namespace, pipeline.name, branch, by_owner):
            raise EnvError(
                "env-namespace-not-managed",
                f"namespace {namespace} is not {pipeline.name}'s environment of "
                f"branch {branch!r}; nothing is stopped",
            )
        record = envs.record(namespace) or {}
        if record.get("state") == "stopped":
            return {**body, "state": "stopped", "stopped": []}
        workloads = sorted(
            f"{item.get('kind')}/{(item.get('metadata') or {}).get('name')}"
            for item in envs.workloads(namespace)
        )
        plan = {
            "schema": PLAN_SCHEMA,
            "app": pipeline.name,
            "branch": branch,
            "namespace": namespace,
            "uid": (live.get("metadata") or {}).get("uid"),
            "stop": workloads,
        }
        body.update({"stop": workloads, "env_hash": _hash(plan)})
        allowed = config.idle_stop is not None or config.auto_approve or by_owner
        if approve is None and not (approve_if_policy and allowed):
            return {
                **body,
                "state": "approval-required",
                **({"reason": "approval-policy-exceeded"} if approve_if_policy else {}),
            }
        if approve is not None and approve != body["env_hash"]:
            raise EnvError(
                "env-plan-changed",
                "the approved hash is not the environment's current stop plan",
            )
        stopped = stop_env(envs, namespace, now=now)
        record = dict(envs.record(namespace) or {})
        record["stop_reason"] = reason
        envs.write_record(namespace, record)
        return {**body, "state": "stopped", "stopped": stopped}
    finally:
        if cluster is None:
            envs.close()


def _stoppable(
    envs: Any,
    live: Mapping[str, Any],
    namespace: str,
    app: str,
    branch: str,
    named: bool,
) -> bool:
    """Whether Piceli may scale ``namespace``'s workloads for ``branch``.

    A branch environment's namespace is this app's (labels); a named one's
    is the one Piceli deployed it to: labelled with its name, or holding its
    environment record.
    """
    if not named:
        return _owned(live, app, branch) and _branch_of(live) in {None, branch}
    labels = (live.get("metadata") or {}).get("labels") or {}
    if labels.get(ENV_NAME_LABEL) == branch:
        return True
    record = envs.record(namespace)
    return isinstance(record, Mapping) and record.get("branch") == branch


def env_start(
    pipeline: Pipeline,
    branch: str,
    *,
    cluster: Any = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Scale a stopped environment back to the replicas it had (``env_stop`` recorded them).

    The GitOps controller's ``piceli env start`` (or a removed
    ``Environment(stopped=True)``) of a named environment; a deploy of a new
    revision follows separately. Claims and objects were kept, so nothing is
    created. ``state``: ``started``, ``running`` (it was not stopped) or
    ``absent`` (no namespace).

    :raises EnvError: ``env-namespace-not-managed``.
    """
    config = env_config(pipeline)
    namespace = config.namespace_for(branch, main_namespace(pipeline))
    envs = cluster or default_cluster(pipeline)
    try:
        live = envs.namespace(namespace)
        body: dict[str, Any] = {
            "schema": RESULT_SCHEMA,
            "branch": branch,
            "namespace": namespace,
        }
        if live is None:
            return {**body, "state": "absent", "started": []}
        named = config.named(branch) is not None
        if not _stoppable(envs, live, namespace, pipeline.name, branch, named):
            raise EnvError(
                "env-namespace-not-managed",
                f"namespace {namespace} is not {pipeline.name}'s environment of "
                f"branch {branch!r}; nothing is started",
            )
        record = dict(envs.record(namespace) or {})
        if record.get("state") != "stopped" and not record.get("stopped"):
            return {**body, "state": "running", "started": []}
        started = _start_stopped(envs, namespace, record)
        record.update({"state": "running", "stopped": [], "started_at": _now(now)})
        record.pop("stopped_at", None)
        record.pop("stop_reason", None)
        envs.write_record(namespace, record)
        return {**body, "state": "started", "started": started}
    finally:
        if cluster is None:
            envs.close()


# -------------------------------------------------------------------- list


def _health(
    record: Mapping[str, Any], workloads: list[dict[str, Any]]
) -> tuple[str, list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    for item in workloads:
        spec = item.get("spec") or {}
        status = item.get("status") or {}
        desired = spec.get("replicas")
        desired = 1 if desired is None else int(desired)
        ready = int(status.get("readyReplicas") or 0)
        rows.append(
            {
                "workload": f"{item.get('kind')}/{(item.get('metadata') or {}).get('name')}",
                "replicas": desired,
                "ready": ready,
            }
        )
    rows.sort(key=lambda row: row["workload"])
    if record.get("state") == "stopped":
        return "stopped", rows
    if not rows:
        return "unknown", rows
    healthy = all(
        row["replicas"] > 0 and row["ready"] >= row["replicas"] for row in rows
    )
    return ("healthy" if healthy else "degraded"), rows


def _status(
    cluster: Any,
    branch: str,
    namespace: str,
    live: Mapping[str, Any] | None,
    *,
    main: bool,
    now: datetime,
) -> EnvStatus:
    if live is None:
        return EnvStatus(branch, namespace, main, "absent", "unknown")
    try:
        record = cluster.record(namespace) or {}
        health, rows = _health(record, cluster.workloads(namespace))
    except EnvError:
        record, health, rows = {}, "unknown", []
    created = str(
        (live.get("metadata") or {}).get("creationTimestamp") or ""
    ) or record.get("created_at")
    started = _parse(created)
    return EnvStatus(
        branch=str(record.get("branch") or branch),
        namespace=namespace,
        main=main,
        state="stopped" if record.get("state") == "stopped" else "running",
        health=health,
        commit=record.get("commit"),
        build=record.get("build"),
        deploy=record.get("deploy"),
        created_at=created,
        pushed_at=record.get("pushed_at"),
        age_seconds=None
        if started is None
        else max(0, int((now - started).total_seconds())),
        workloads=rows,
    )


def list_envs(
    pipeline: Pipeline, *, cluster: Any = None, now: datetime | None = None
) -> list[EnvStatus]:
    """Every environment: main, the named ones (declared order), then branches (read-only)."""
    import dataclasses

    from piceli.gitops.state import env_status

    config = env_config(pipeline)
    moment = now or datetime.now(UTC)
    main_ns = main_namespace(pipeline)
    envs = cluster or default_cluster(pipeline)
    try:
        try:
            main_live = envs.namespace(main_ns)
        except EnvError:
            main_live = {"metadata": {"name": main_ns}}
        reader = getattr(envs, "gitops_status", None)
        document = reader() if callable(reader) else None
        found = []
        if config.named(config.main_branch) is None:
            found.append(
                _status(
                    envs, config.main_branch, main_ns, main_live, main=True, now=moment
                )
            )
        fixed_spaces = {main_ns}
        for declared in config.environments:
            fixed_spaces.add(declared.namespace)
            try:
                live = envs.namespace(declared.namespace)
            except EnvError:
                live = {"metadata": {"name": declared.namespace}}
            status = _status(
                envs,
                declared.name,
                declared.namespace,
                live,
                main=config.is_main(declared.name),
                now=moment,
            )
            found.append(dataclasses.replace(status, branch=declared.name, fixed=True))
        branches = []
        for item in envs.namespaces(pipeline.name):
            name = str((item.get("metadata") or {}).get("name"))
            if name in fixed_spaces:
                continue
            branch = _branch_of(item) or name
            branches.append(_status(envs, branch, name, item, main=False, now=moment))
        shown = {status.branch for status in [*found, *branches]}
        for branch, entry in sorted(((document or {}).get("envs") or {}).items()):
            if branch in shown or not isinstance(entry, Mapping):
                continue
            # Known to the controller only (pending, building, awaiting approval).
            namespace = str(entry.get("namespace") or "")
            branches.append(EnvStatus(branch, namespace, False, "absent", "unknown"))
        return [
            dataclasses.replace(status, gitops=env_status(document, status.branch))
            for status in found + sorted(branches, key=lambda status: status.namespace)
        ]
    finally:
        if cluster is None:
            envs.close()


# ------------------------------------------------------------ access, logs


def free_port() -> int:
    """A loopback port no process listens on now (the OS picks it)."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def env_access_target(
    pipeline: Pipeline, branch: str, *, port: Callable[[], int] = free_port
) -> Any:
    """The :class:`~piceli.k8s.access.AccessTarget` of one environment.

    The main branch keeps its declared local ports; a branch environment's
    forwards get free local ports, so several environments are reachable at
    once. Pure apart from probing free ports; reads no cluster.
    """
    import dataclasses

    from piceli.k8s.access import access_target_of

    config = env_config(pipeline)
    derived = env_pipeline(pipeline, branch)
    resolved = access_target_of(derived, f"{pipeline.name} environment {branch}")
    if config.is_fixed(branch):
        return resolved
    shortcuts = tuple(
        item.model_copy(update={"local_port": port()}) for item in resolved.shortcuts
    )
    return dataclasses.replace(resolved, shortcuts=shortcuts)
