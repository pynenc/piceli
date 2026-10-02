"""``piceli artifacts retention``: report, and with approval delete, unkept manifests.

Without ``--delete`` it only reads the registry (and, for the live digests,
the cluster's pod specs) and prints the report with the plan hash. ``--delete``
without ``--approve`` prints the plan and exits 3; ``--delete --approve HASH``
deletes exactly the manifests of a plan that still hashes to ``HASH``.
"""

from __future__ import annotations

import argparse
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import Any

from piceli.artifacts.delivery import write_receipt
from piceli.artifacts.delivery_inputs import DeliveryInputError, discover_tool
from piceli.artifacts.registry import (
    RegistryCredentials,
    RegistryError,
    RegistryTarget,
    docker_config_credentials,
)
from piceli.artifacts.registry_delivery import RegistryForward, supervised_forward
from piceli.artifacts.retention import (
    LiveWorkloads,
    RetentionError,
    RetentionGrant,
    RetentionPolicy,
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
    cmd.add_argument("--to", required=True, help="oci://host[:port]/prefix to inspect")
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
        default=3,
        help="releases kept (minimum); 0 keeps only what is live or pinned "
        "(needs --kubeconfig and --context)",
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
    cmd.add_argument("--delete", action="store_true")
    cmd.add_argument("--approve", help="the plan hash printed without --approve")
    credentials = cmd.add_mutually_exclusive_group()
    credentials.add_argument("--credentials", type=Path)
    credentials.add_argument("--docker-config", type=Path)
    cmd.add_argument("--ca-file", type=Path)
    cmd.add_argument("--out", type=Path, help="write the report or receipt here")
    cmd.add_argument("--timeout", type=float, default=120)


def _live(args: argparse.Namespace, seams: dict[str, Any]) -> LiveWorkloads:
    parts: list[LiveWorkloads] = []
    if args.kubeconfig is not None or args.context is not None:
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


def run_retention_command(args: argparse.Namespace, **seams: Any) -> int:
    """Report (exit 0), ask approval (3), or delete an approved plan (0, else 1)."""
    from piceli.artifacts.cli import rejection
    from piceli.cli_contract import EXIT_APPROVAL, emit_json, say

    try:
        target = RegistryTarget.parse(args.to)
        pins = set(args.pin)
        if args.pin_file is not None:
            pins |= read_pins(args.pin_file)
        policy = RetentionPolicy(
            keep=args.keep,
            budget=parse_size(args.budget) if args.budget is not None else None,
            pins=frozenset(pins),
            collect_unledgered=args.collect_unledgered,
        )
        if policy.keep == 0 and (args.kubeconfig is None or not args.context):
            # Nothing but the live inventory protects a release: read it.
            raise RetentionError(code="retention-invalid")
        releases = load_releases(args.receipts)
        if args.approve is not None and not args.delete:
            raise RetentionError(code="retention-invalid")
        live = _live(args, seams)
        if args.delete and not live.known:
            raise RetentionError(code="retention-live-unknown")
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
        factory = seams.get("client_factory", default_client)
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
            if not args.delete:
                return _emit(args, report, emit_json)
            if args.approve is None:
                emit_json({**report, "state": "approval-required"})
                say(
                    f"retention: {len(plan.collectable)} manifest(s) would be deleted; "
                    f"approve with --approve {plan.digest}"
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
        return _emit(args, receipt, emit_json)
    except (RetentionError, DeliveryInputError) as error:
        return rejection(error.code if error.code in ERRORS else "retention-invalid")
    except RegistryError as error:
        return rejection(error.reason if error.reason in ERRORS else "registry-error")
    except (ValueError, KeyError, TypeError, OSError):
        return rejection("retention-invalid")


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
