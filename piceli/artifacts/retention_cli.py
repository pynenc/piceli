"""``piceli artifacts retention``: report, and with approval delete, unkept manifests.

Without ``--delete`` it only reads the registry (and, for the live digests,
the cluster's pod specs) and prints the report with the plan hash. ``--delete``
without ``--approve`` prints the plan and exits 3; ``--delete --approve HASH``
deletes exactly the manifests of a plan that still hashes to ``HASH``.

``--cluster REF`` names the in-cluster registry (a ``piceli.infra.Cluster``,
a composition module, a pipeline delivering to ``Registry.in_cluster`` or
that value): the registry, its port-forward, the cluster read (every
namespace) and the listing of every manifest it stores are derived from it,
``--keep`` defaults to 0 and tagged manifests no receipt mentions are
collectable too. What runs, every workload's rollback target and what Piceli
itself uses (controller, builder, UI) are kept; everything else goes.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from pathlib import Path
from typing import Any

from piceli.artifacts.delivery import write_receipt
from piceli.artifacts.delivery_inputs import DeliveryInputError, discover_tool
from piceli.artifacts.registry import (
    RegistryCredentials,
    RegistryEndpoint,
    RegistryError,
    RegistryTarget,
    docker_config_credentials,
)
from piceli.artifacts.registry_delivery import RegistryForward, supervised_forward
from piceli.artifacts.registry_ref import (
    ClusterAccess,
    RegistryRefError,
    cluster_access,
    load_registry_ref,
)
from piceli.artifacts.registry_storage import StorageReadError
from piceli.artifacts.retention import (
    LiveWorkloads,
    RetentionError,
    RetentionGrant,
    RetentionPolicy,
    StableTarget,
    collect_inventory,
    default_client,
    delete_collectable,
    load_releases,
    parse_size,
    plan_retention,
    read_live_pods,
    read_pins,
)
from piceli.errors import ERRORS


def add_retention_command(sub: Any) -> None:
    """``artifacts retention``: keep the last releases, pinned and live digests."""
    cmd = sub.add_parser(
        "retention",
        help="report (and with --delete --approve, delete) manifests no release, pin or workload needs",
    )
    cmd.add_argument(
        "--cluster",
        help="the in-cluster registry: MODULE:ATTR of a piceli.infra.Cluster (or "
        "a composition module infra.py, a Pipeline delivering to "
        "Registry.in_cluster, or that value); derives --to, the forward and the "
        "cluster read, and lists every stored manifest",
    )
    cmd.add_argument("--env", help="with --cluster: the pipeline's environment")
    cmd.add_argument(
        "--to", help="oci://host[:port]/prefix to inspect (unless --cluster)"
    )
    cmd.add_argument(
        "--receipts",
        type=Path,
        action="append",
        default=[],
        help="publish or delivery receipts, JSON Lines journals or directories of them",
    )
    cmd.add_argument(
        "--keep",
        type=int,
        default=None,
        help="releases kept (minimum; default 3, with --cluster 0); 0 keeps "
        "only what is live, a rollback target or pinned (needs the cluster read)",
    )
    cmd.add_argument(
        "--budget", help="keep more releases, newest first, up to e.g. 10GiB"
    )
    cmd.add_argument("--pin", action="append", default=[], help="a digest to keep")
    cmd.add_argument("--pin-file", type=Path, help="digests to keep, one per line")
    cmd.add_argument(
        "--collect-unledgered",
        action="store_true",
        help="also collect tagged manifests no receipt mentions",
    )
    cmd.add_argument(
        "--repository",
        action="append",
        default=[],
        help="a repository to inspect (registries without a catalog)",
    )
    cmd.add_argument("--kubeconfig", type=Path, help="reads live pods; with --context")
    cmd.add_argument("--context")
    cmd.add_argument(
        "--live-namespace", action="append", default=[], help="default: all namespaces"
    )
    cmd.add_argument(
        "--live-file", type=Path, help="digests in use, one per line or JSON"
    )
    cmd.add_argument("--via-forward", help="service/NAME, deployment/NAME or pod/NAME")
    cmd.add_argument("--namespace", help="namespace of the forward target")
    cmd.add_argument("--forward-remote-port", type=int, default=5000)
    cmd.add_argument("--kubectl", type=Path)
    cmd.add_argument("--kubectl-sha256")
    cmd.add_argument(
        "--allow-exec",
        action="store_true",
        help="allow the context's exec credential plugin (GKE, EKS, AKS, OIDC)",
    )
    cmd.add_argument("--exec-sha256", help="expected sha256:<hex> of the exec plugin")
    cmd.add_argument(
        "--transport",
        default="https",
        choices=("https", "loopback-http"),
        help="https, or loopback-http for a local test API",
    )
    cmd.add_argument("--delete", action="store_true")
    cmd.add_argument("--approve", help="the plan hash printed without --approve")
    credentials = cmd.add_mutually_exclusive_group()
    credentials.add_argument("--credentials", type=Path)
    credentials.add_argument("--docker-config", type=Path)
    cmd.add_argument("--ca-file", type=Path)
    cmd.add_argument("--out", type=Path, help="write the report or receipt here")
    cmd.add_argument("--timeout", type=float, default=120)


def _live(
    args: argparse.Namespace, seams: dict[str, Any], access: ClusterAccess | None
) -> LiveWorkloads:
    parts: list[LiveWorkloads] = []
    if access is not None:
        reader = seams.get("pod_reader", read_live_pods)
        try:
            pods = reader(
                access.kubeconfig.absolute(),
                access.context,
                args.live_namespace or None,
                transport=access.transport,
                exec_policy=access.exec_policy,
            )
        except Exception as error:  # any cluster failure: the inventory is unknown
            raise RetentionError(code="retention-live-unknown") from error
        parts.append(LiveWorkloads.from_objects(pods))
    elif args.kubeconfig is not None or args.context is not None:
        if args.kubeconfig is None or not args.context:
            raise RetentionError(code="retention-invalid")
        reader = seams.get("pod_reader", read_live_pods)
        try:
            pods = reader(
                args.kubeconfig.absolute(), args.context, args.live_namespace or None
            )
        except Exception as error:  # any cluster failure: the inventory is unknown
            raise RetentionError(code="retention-live-unknown") from error
        parts.append(LiveWorkloads.from_objects(pods))
    if args.live_file is not None:
        try:
            parts.append(LiveWorkloads.from_file(args.live_file))
        except OSError as error:
            raise RetentionError(code="retention-invalid") from error
    return LiveWorkloads.merge(parts)


def read_stored_manifests(access: ClusterAccess, registry: Any) -> set[tuple[str, str]]:
    """Every manifest the in-cluster registry stores (``pods/exec`` in its pod)."""
    from piceli.artifacts.registry_storage import read_stored_manifests as read
    from piceli.k8s.ops.provider_factory import api_client_from_kubeconfig

    try:
        client = api_client_from_kubeconfig(
            access.kubeconfig.absolute(),
            access.context,
            transport=access.transport,  # type: ignore[arg-type]
            exec_policy=access.exec_policy,
        )
    except Exception:
        raise StorageReadError("registry-storage-unreadable") from None
    try:
        return read(client, registry.namespace, registry.name)
    finally:
        client.close()


@contextmanager
def cluster_forward(
    registry: Any, access: ClusterAccess, args: argparse.Namespace
) -> Iterator[int]:
    """A supervised ``kubectl port-forward`` to the registry Service; the local port."""
    from piceli.pipeline.backend import free_port

    port = free_port()
    forward = RegistryForward(
        namespace=registry.namespace,
        target=f"service/{registry.name}",
        remote_port=registry.port,
        kubeconfig=access.kubeconfig.absolute(),
        kubectl=discover_tool("kubectl", args.kubectl, args.kubectl_sha256),
        context=access.context,
    )
    with supervised_forward(forward, port):
        yield port


def _cluster_mode(args: argparse.Namespace) -> tuple[Any, ClusterAccess]:
    """The registry ``--cluster`` names and how to reach its cluster."""
    from piceli.k8s.ops.exec_credentials import ExecPolicy

    if args.to is not None or args.via_forward is not None or args.repository:
        raise RetentionError(code="retention-invalid")
    found = load_registry_ref(args.cluster, args.env)
    access = cluster_access(
        found,
        kubeconfig=args.kubeconfig,
        context=args.context,
        transport=args.transport,
        exec_policy=ExecPolicy(args.allow_exec, args.exec_sha256),
    )
    return found.registry, access


def _garbage_collect(registry: Any) -> dict[str, Any]:
    """The registry's garbage collector for this registry: dry run, then run."""
    from piceli.artifacts.retention import GARBAGE_COLLECT

    head = (
        "kubectl --kubeconfig FILE --context NAME "
        f"-n {registry.namespace} exec deploy/{registry.name} -- "
        "registry garbage-collect"
    )
    config = "/etc/distribution/config.yml"
    return {
        **GARBAGE_COLLECT,
        "commands": [f"{head} --dry-run {config}", f"{head} {config}"],
        "when": "no cluster build Job is running and nothing pushes",
    }


def _approve_command(args: argparse.Namespace, digest: str) -> str | None:
    if args.cluster is None:
        return None
    import shlex

    parts = ["piceli", "artifacts", "retention", "--cluster", args.cluster]
    single = {
        "--env": args.env,
        "--keep": args.keep,
        "--budget": args.budget,
        "--pin-file": args.pin_file,
        "--kubeconfig": args.kubeconfig,
        "--context": args.context,
    }
    repeated = {
        "--receipts": args.receipts,
        "--pin": args.pin,
        "--live-namespace": args.live_namespace,
    }
    for flag, value in single.items():
        if value is not None:
            parts += [flag, str(value)]
    for flag, values in repeated.items():
        for value in values:
            parts += [flag, str(value)]
    if args.transport != "https":
        parts += ["--transport", args.transport]
    if args.allow_exec:
        parts.append("--allow-exec")
    if args.exec_sha256:
        parts += ["--exec-sha256", args.exec_sha256]
    return " ".join([*map(shlex.quote, parts), "--delete", "--approve", digest])


def run_retention_command(args: argparse.Namespace, **seams: Any) -> int:
    """Report (exit 0), ask approval (3), or delete an approved plan (0, else 1)."""
    from piceli.artifacts.cli import rejection

    try:
        registry = access = None
        if args.cluster is not None:
            registry, access = _cluster_mode(args)
        elif args.to is None:
            raise RetentionError(code="retention-invalid")
        keep = args.keep if args.keep is not None else (0 if registry else 3)
        pins = set(args.pin)
        if args.pin_file is not None:
            pins |= read_pins(args.pin_file)
        policy = RetentionPolicy(
            keep=keep,
            budget=parse_size(args.budget) if args.budget is not None else None,
            pins=frozenset(pins),
            # Everything in the in-cluster registry that nothing keeps goes,
            # tagged or not.
            collect_unledgered=args.collect_unledgered or registry is not None,
        )
        if (
            registry is None
            and policy.keep == 0
            and (args.kubeconfig is None or not args.context)
        ):
            # Nothing but the live inventory protects a release: read it.
            raise RetentionError(code="retention-invalid")
        releases = load_releases(args.receipts)
        if args.approve is not None and not args.delete:
            raise RetentionError(code="retention-invalid")
        live = _live(args, seams, access)
        if args.delete and not live.known:
            raise RetentionError(code="retention-live-unknown")
        factory = seams.get("client_factory", default_client)
        if registry is not None and access is not None:
            return _run_cluster(args, registry, access, live, releases, policy, factory)
        target = RegistryTarget.parse(args.to)
        credentials = None
        if args.credentials is not None:
            credentials = RegistryCredentials.load(args.credentials.absolute())
        elif args.docker_config is not None:
            credentials = docker_config_credentials(
                args.docker_config.absolute(), target.registry
            )
        endpoint = target.endpoint(
            credentials=credentials,
            ca_file=args.ca_file.absolute() if args.ca_file else None,
            timeout=args.timeout,
        )
        forwarded: AbstractContextManager[None] = nullcontext()
        if args.via_forward is not None:
            if args.kubeconfig is None or not args.context or not args.namespace:
                raise DeliveryInputError("forward-options-incomplete")
            if target.port is None or target.tls:
                raise DeliveryInputError("forward-target-not-loopback")
            forward = RegistryForward(
                namespace=args.namespace,
                target=args.via_forward,
                remote_port=args.forward_remote_port,
                kubeconfig=args.kubeconfig.absolute(),
                kubectl=discover_tool("kubectl", args.kubectl, args.kubectl_sha256),
                context=args.context,
            )
            forwarded = seams.get("forwarder", supervised_forward)(forward, target.port)
        with forwarded:
            inventory = collect_inventory(
                target,
                endpoint,
                releases,
                repositories=args.repository,
                client_factory=factory,
                probes=live.probes(),
            )
            plan = plan_retention(target, inventory, releases, policy, live)
            report = {"state": "report", **plan.public(inventory)}
            return _decide(args, plan, inventory, report, endpoint, factory)
    except (RetentionError, DeliveryInputError, RegistryRefError) as error:
        return rejection(error.code if error.code in ERRORS else "retention-invalid")
    except RegistryError as error:
        return rejection(error.reason if error.reason in ERRORS else "registry-error")
    except (ValueError, KeyError, TypeError, OSError):
        return rejection("retention-invalid")


def _run_cluster(
    args: argparse.Namespace,
    registry: Any,
    access: ClusterAccess,
    live: LiveWorkloads,
    releases: Any,
    policy: RetentionPolicy,
    factory: Any,
) -> int:
    """``--cluster``: list the stored manifests, forward, plan, maybe delete."""
    from piceli.cli_contract import say

    target = StableTarget(registry.host, registry.repository or "")
    listing: dict[str, Any] = {"read": True}
    stored: set[tuple[str, str]] = set()
    try:
        stored = read_stored_manifests(access, registry)
    except StorageReadError as error:
        listing = {"read": False, "reason": error.code}
        say(
            "retention: the registry's storage could not be listed "
            f"[{error.code}]; manifests nothing names (digest-only) are not "
            "found, so they stay. It needs pods/exec in "
            f"{registry.namespace}."
        )
    with cluster_forward(registry, access, args) as port:
        endpoint = RegistryEndpoint("127.0.0.1", port, False, timeout=args.timeout)
        inventory = collect_inventory(
            target,
            endpoint,
            releases,
            client_factory=factory,
            probes=live.probes(),
            stored=stored,
        )
        plan = plan_retention(target, inventory, releases, policy, live)
        report = {
            "state": "report",
            **plan.public(inventory),
            "storage_listing": listing,
            "garbage_collect": _garbage_collect(registry),
        }
        return _decide(args, plan, inventory, report, endpoint, factory, registry)


def _decide(
    args: argparse.Namespace,
    plan: Any,
    inventory: Any,
    report: dict[str, Any],
    endpoint: RegistryEndpoint,
    factory: Any,
    registry: Any = None,
) -> int:
    """Print the report, ask for approval, or delete the approved plan."""
    from piceli.cli_contract import EXIT_APPROVAL, emit_json, say

    if not args.delete:
        return _emit(args, report, emit_json)
    if args.approve is None:
        command = _approve_command(args, plan.digest)
        emit_json(
            {
                **report,
                "state": "approval-required",
                **({"approve_command": command} if command else {}),
            }
        )
        say(
            f"retention: {len(plan.collectable)} manifest(s) would be deleted; "
            + (
                f"approve with: {command}"
                if command
                else f"approve with --approve {plan.digest}"
            )
        )
        return EXIT_APPROVAL
    if args.approve != plan.digest:
        raise RetentionError(code="retention-not-approved")
    receipt = delete_collectable(
        plan,
        inventory,
        RetentionGrant(args.approve),
        endpoint,
        client_factory=factory,
    )
    if registry is not None:
        receipt["garbage_collect"] = _garbage_collect(registry)
        say(
            "retention: deleted; free the space with the registry's garbage "
            "collector (dry run first): " + receipt["garbage_collect"]["commands"][0]
        )
    return _emit(args, receipt, emit_json)


def _emit(args: argparse.Namespace, result: dict[str, Any], emit_json: Any) -> int:
    from piceli.cli_contract import say

    if args.out is not None:
        write_receipt(args.out, result)
    emit_json(result)
    if result["state"] in {"failed"}:
        reason = result["reason"] if result["reason"] in ERRORS else "registry-error"
        say(f"retention: failed [{reason}]")
        return 1
    return 0
