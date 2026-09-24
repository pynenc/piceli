"""Thin JSON CLI over artifact library calls. Preview does not instantiate clients."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from piceli.artifacts import BuildPlan, OciBuilder, SourcePin, inspect_oci
from piceli.artifacts.build_spec import (
    add_build_spec_commands,
    run_build_spec_command,
)
from piceli.artifacts.delivery import (
    ArchiveSource,
    DeliveryGrant,
    DockerImageSource,
    NodeDelivery,
    append_journal,
    write_receipt,
)
from piceli.artifacts.local_import import DockerLocalImporter, LocalImportGrant
from piceli.artifacts.node_transport import NodeTarget
from piceli.artifacts.process import (
    BuildCommand,
    ExecutionGrant,
    ProcessLimits,
    ToolPin,
)
from piceli.artifacts.registry import RegistryCredentials, RegistryTarget
from piceli.artifacts.registry_delivery import RegistryDelivery, RegistryForward
from piceli.k8s.ops.bounds import strict_json


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="piceli artifacts")
    sub = parser.add_subparsers(dest="command", required=True)
    for action in ("preview", "build"):
        cmd = sub.add_parser(action)
        cmd.add_argument("--plan", type=Path, required=True)
        if action == "build":
            cmd.add_argument("--source-root", type=Path, required=True)
            cmd.add_argument("--output", type=Path, required=True)
    cmd = sub.add_parser("pin")
    cmd.add_argument("--source-root", type=Path, required=True)
    cmd.add_argument("--public-file", required=True)
    for action in ("inspect", "import-local"):
        cmd = sub.add_parser(action)
        cmd.add_argument("--layout", type=Path, required=True)
        if action == "import-local":
            cmd.add_argument("--docker", type=Path, required=True)
            cmd.add_argument("--docker-sha256", required=True)
            cmd.add_argument("--socket", type=Path, required=True)
            cmd.add_argument("--approve-digest", required=True)
    for action in ("preview-command", "execute-command"):
        cmd = sub.add_parser(action)
        cmd.add_argument("--spec", type=Path, required=True)
        if action == "execute-command":
            cmd.add_argument("--source-root", type=Path, required=True)
            cmd.add_argument("--approve-plan", required=True)
            cmd.add_argument("--allow-code-execution", action="store_true")
            cmd.add_argument("--allow-network", action="store_true")
            cmd.add_argument("--timeout", type=float, default=60)
    cmd = sub.add_parser("deliver")
    source = cmd.add_mutually_exclusive_group(required=True)
    source.add_argument("--image")
    source.add_argument("--archive", type=Path)
    cmd.add_argument("--to", required=True)
    cmd.add_argument("--approve-digest", required=True)
    cmd.add_argument("--ref")
    cmd.add_argument("--docker", type=Path)
    cmd.add_argument("--docker-sha256")
    cmd.add_argument("--docker-socket", type=Path)
    cmd.add_argument("--ssh", type=Path)
    cmd.add_argument("--ssh-sha256")
    cmd.add_argument("--ssh-agent-socket", type=Path)
    cmd.add_argument("--timeout", type=float, default=600)
    cmd.add_argument("--receipt", type=Path)
    cmd.add_argument("--journal", type=Path)
    # Registry delivery (--to oci://host[:port]/repository[:tag]).
    cmd.add_argument("--node-registry")
    cmd.add_argument("--credentials", type=Path)
    cmd.add_argument("--ca-file", type=Path)
    cmd.add_argument("--via-forward")
    cmd.add_argument("--namespace")
    cmd.add_argument("--kubeconfig", type=Path)
    cmd.add_argument("--context")
    cmd.add_argument("--forward-remote-port", type=int, default=5000)
    cmd.add_argument("--kubectl", type=Path)
    cmd.add_argument("--kubectl-sha256")
    add_build_spec_commands(sub)
    args = parser.parse_args(arguments)
    if args.command == "build-spec":
        return run_build_spec_command(args)
    try:
        if args.command == "pin":
            result = SourcePin.capture(
                args.source_root, args.public_file, public=True
            ).__dict__
        elif args.command in {"preview", "build"}:
            plan = BuildPlan.from_dict(strict_json(args.plan.read_text(), 1_048_576))
            result = (
                plan.preview()
                if args.command == "preview"
                else OciBuilder().build(plan, args.source_root, args.output).summary()
            )
        elif args.command == "inspect":
            result = inspect_oci(args.layout).summary()
        elif args.command == "deliver":
            result = _deliver(args)
        elif args.command == "import-local":
            importer = DockerLocalImporter(
                ToolPin(args.docker, args.docker_sha256), args.socket
            )
            result = importer.import_image(
                args.layout,
                LocalImportGrant(args.approve_digest, args.socket, time.time() + 60),
            )
        else:
            spec = strict_json(args.spec.read_text(), 1_048_576)
            build = BuildCommand(
                ToolPin(Path(spec["tool"]), spec["tool_sha256"]),
                tuple(spec["arguments"]),
                tuple(SourcePin(**pin) for pin in spec.get("inputs", [])),
            )
            if args.command == "preview-command":
                result = build.preview()
            else:
                result = build.execute(
                    args.source_root,
                    ExecutionGrant(
                        args.approve_plan,
                        time.time() + args.timeout,
                        args.allow_code_execution,
                        args.allow_network,
                    ),
                    limits=ProcessLimits(args.timeout),
                )
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("state", "succeeded") == "succeeded" else 1
    except (ValueError, KeyError, TypeError, OSError, InterruptedError):
        # Do not echo private paths, process output, manifests or attacker-controlled errors.
        print(
            json.dumps(
                {"state": "rejected", "reason": "invalid-or-unavailable-artifact-input"}
            ),
            file=sys.stderr,
        )
        return 2


_REGISTRY_ONLY = (
    "node_registry",
    "credentials",
    "ca_file",
    "via_forward",
    "namespace",
    "kubeconfig",
    "context",
    "kubectl",
    "kubectl_sha256",
)
_NODE_ONLY = ("ref", "ssh", "ssh_sha256", "ssh_agent_socket")


def _deliver(args: argparse.Namespace) -> dict[str, object]:
    source = (
        DockerImageSource(args.image)
        if args.image
        else ArchiveSource(args.archive.absolute())
    )
    grant = DeliveryGrant(args.approve_digest, args.to, time.time() + args.timeout)
    if args.to.startswith("oci://"):
        receipt = _deliver_registry(args, source, grant)
    else:
        receipt = _deliver_node(args, source, grant)
    if args.receipt is not None:
        write_receipt(args.receipt, receipt)
    if args.journal is not None:
        append_journal(args.journal, receipt)
    return receipt


def _deliver_registry(
    args: argparse.Namespace,
    source: DockerImageSource | ArchiveSource,
    grant: DeliveryGrant,
) -> dict[str, object]:
    if any(getattr(args, name) is not None for name in _NODE_ONLY):
        raise ValueError("node-import options do not apply to a registry target")
    forward_options = ("namespace", "kubeconfig", "context", "kubectl")
    forward = None
    if args.via_forward is not None:
        if args.kubectl is None or args.kubeconfig is None or args.namespace is None:
            raise ValueError("--via-forward needs --namespace, --kubeconfig, --kubectl")
        forward = RegistryForward(
            namespace=args.namespace,
            target=args.via_forward,
            remote_port=args.forward_remote_port,
            kubeconfig=args.kubeconfig.absolute(),
            kubectl=ToolPin(args.kubectl, args.kubectl_sha256),
            context=args.context,
        )
    elif any(getattr(args, name) is not None for name in forward_options):
        raise ValueError("forward options need --via-forward")
    delivery = RegistryDelivery(
        docker=ToolPin(args.docker, args.docker_sha256) if args.docker else None,
        docker_socket=args.docker_socket,
        credentials=(
            RegistryCredentials.load(args.credentials.absolute())
            if args.credentials is not None
            else None
        ),
        ca_file=args.ca_file.absolute() if args.ca_file is not None else None,
        forward=forward,
    )
    return delivery.deliver(
        source,
        RegistryTarget.parse(args.to),
        grant,
        node_registry=args.node_registry,
        limits=ProcessLimits(args.timeout),
    )


def _deliver_node(
    args: argparse.Namespace,
    source: DockerImageSource | ArchiveSource,
    grant: DeliveryGrant,
) -> dict[str, object]:
    if any(getattr(args, name) is not None for name in _REGISTRY_ONLY):
        raise ValueError("registry options do not apply to a node target")
    target = NodeTarget.parse(args.to)
    delivery = NodeDelivery(
        docker=ToolPin(args.docker, args.docker_sha256) if args.docker else None,
        docker_socket=args.docker_socket,
        ssh=ToolPin(args.ssh, args.ssh_sha256) if args.ssh else None,
        ssh_agent_socket=args.ssh_agent_socket,
    )
    return delivery.deliver(
        source,
        target,
        grant,
        reference=args.ref,
        limits=ProcessLimits(args.timeout),
    )


if __name__ == "__main__":
    raise SystemExit(main())
