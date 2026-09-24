"""Thin JSON CLI over artifact library calls. Preview does not instantiate clients.

Machine JSON goes to stdout, human text to stderr. Refusals print
``{"state": "rejected", "reason": "<code>", "message": <the code's title>}``
and exit 2; a result whose ``state`` is not ``succeeded`` exits 1 and names
its registered code in ``reason``. Messages never carry paths or tool output.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

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
    normalize_reference,
    write_receipt,
)
from piceli.artifacts.delivery_inputs import (
    DeliveryInputError,
    discover_docker_socket,
    discover_tool,
)
from piceli.artifacts.local_import import DockerLocalImporter, LocalImportGrant
from piceli.artifacts.node_transport import NodeTarget
from piceli.artifacts.plan import validate_digest
from piceli.artifacts.process import (
    BuildCommand,
    ExecutionGrant,
    ProcessLimits,
    ToolPin,
)
from piceli.artifacts.registry import RegistryCredentials, RegistryTarget
from piceli.artifacts.registry_delivery import RegistryDelivery, RegistryForward
from piceli.cli_contract import Rejected, reject
from piceli.errors import ERRORS
from piceli.k8s.ops.bounds import strict_json


def build_parser() -> argparse.ArgumentParser:
    """The ``piceli artifacts`` argument parser (also read by ``piceli help-json``)."""
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
    return parser


def main(arguments: list[str] | None = None) -> int:
    args = build_parser().parse_args(arguments)
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
        return _finish(args.command, result)
    except (ValueError, KeyError, TypeError, OSError, InterruptedError) as error:
        # Only fixed codes: never private paths, process output, manifests or
        # attacker-controlled errors.
        reason = (
            error.code
            if isinstance(error, DeliveryInputError)
            else "invalid-or-unavailable-artifact-input"
        )
        return rejection(reason)


def rejection(reason: str, **fields: Any) -> int:
    """Print the rejection for ``reason`` (stdout JSON, stderr text); return 2.

    The message is the registry title: artifact errors never echo their detail.
    """
    try:
        reject(reason, **fields)
    except Rejected as rejected:
        return int(rejected.code or 2)


# Result states of a pinned command or a local import, and their codes.
_RESULT_PREFIX = {"execute-command": "command-", "import-local": "import-"}


def _finish(command: str, result: dict[str, object]) -> int:
    """Print a command result; exit 1 (with ``reason``) unless it succeeded."""
    state = result.get("state", "succeeded")
    if state == "succeeded":
        print(json.dumps(result, sort_keys=True))
        return 0
    if "reason" not in result or result["reason"] is None:
        prefix = _RESULT_PREFIX.get(command)
        code = f"{prefix}{state}" if prefix else ""
        result = {
            **result,
            "reason": code if code in ERRORS else "invalid-delivery-input",
        }
    print(json.dumps(result, sort_keys=True))
    print(f"{command}: {state} [{result['reason']}]", file=sys.stderr)
    return 1


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
_FORWARD_OPTIONS = ("namespace", "kubeconfig", "context", "kubectl", "kubectl_sha256")
T = TypeVar("T")


def _input(code: str, make: Callable[[], T]) -> T:
    """Run one input step; any failure becomes the fixed rejection ``code``."""
    try:
        return make()
    except DeliveryInputError:
        raise
    except (ValueError, KeyError, TypeError, OSError) as error:
        raise DeliveryInputError(code) from error


def _docker(args: argparse.Namespace) -> tuple[ToolPin, Path]:
    """The pinned docker CLI (explicit or from PATH) and its unix socket."""
    tool = discover_tool("docker", args.docker, args.docker_sha256)
    socket = discover_docker_socket(
        tool, args.docker_socket.absolute() if args.docker_socket else None
    )
    return tool, socket


def _deliver(args: argparse.Namespace) -> dict[str, object]:
    _input("invalid-approved-digest", lambda: validate_digest(args.approve_digest))
    limits = _input("invalid-timeout", lambda: ProcessLimits(args.timeout))
    source: DockerImageSource | ArchiveSource = _input(
        "invalid-source",
        lambda: (
            DockerImageSource(args.image)
            if args.image
            else ArchiveSource(args.archive.absolute())
        ),
    )
    grant = _input(
        "invalid-target",
        lambda: DeliveryGrant(args.approve_digest, args.to, time.time() + args.timeout),
    )
    if args.to.startswith("oci://"):
        receipt = _deliver_registry(args, source, grant, limits)
    else:
        receipt = _deliver_node(args, source, grant, limits)
    if args.receipt is not None:
        write_receipt(args.receipt, receipt)
    if args.journal is not None:
        append_journal(args.journal, receipt)
    return receipt


def _deliver_registry(
    args: argparse.Namespace,
    source: DockerImageSource | ArchiveSource,
    grant: DeliveryGrant,
    limits: ProcessLimits,
) -> dict[str, object]:
    if any(getattr(args, name) is not None for name in _NODE_ONLY):
        raise DeliveryInputError("node-options-on-registry-target")
    target = _input("invalid-target", lambda: RegistryTarget.parse(args.to))
    forward = None
    if args.via_forward is not None:
        if args.kubeconfig is None or args.namespace is None:
            raise DeliveryInputError("forward-options-incomplete")
        kubectl = discover_tool("kubectl", args.kubectl, args.kubectl_sha256)
        forward = _input(
            "invalid-forward",
            lambda: RegistryForward(
                namespace=args.namespace,
                target=args.via_forward,
                remote_port=args.forward_remote_port,
                kubeconfig=args.kubeconfig.absolute(),
                kubectl=kubectl,
                context=args.context,
            ),
        )
    elif any(getattr(args, name) is not None for name in _FORWARD_OPTIONS):
        raise DeliveryInputError("forward-options-without-forward")
    docker, socket = (
        _docker(args) if isinstance(source, DockerImageSource) else (None, None)
    )
    credentials = (
        _input(
            "invalid-credentials-file",
            lambda: RegistryCredentials.load(args.credentials.absolute()),
        )
        if args.credentials is not None
        else None
    )
    delivery = _input(
        "invalid-delivery-input",
        lambda: RegistryDelivery(
            docker=docker,
            docker_socket=socket,
            credentials=credentials,
            ca_file=args.ca_file.absolute() if args.ca_file is not None else None,
            forward=forward,
        ),
    )
    return _input(
        "invalid-delivery-input",
        lambda: delivery.deliver(
            source, target, grant, node_registry=args.node_registry, limits=limits
        ),
    )


def _deliver_node(
    args: argparse.Namespace,
    source: DockerImageSource | ArchiveSource,
    grant: DeliveryGrant,
    limits: ProcessLimits,
) -> dict[str, object]:
    if any(getattr(args, name) is not None for name in _REGISTRY_ONLY):
        raise DeliveryInputError("registry-options-on-node-target")
    target = _input("invalid-target", lambda: NodeTarget.parse(args.to))
    if args.ref is not None:
        _input("invalid-reference", lambda: normalize_reference(args.ref))
    docker, socket = (
        _docker(args)
        if isinstance(source, DockerImageSource) or target.transport == "docker"
        else (None, None)
    )
    ssh = (
        discover_tool("ssh", args.ssh, args.ssh_sha256)
        if target.transport == "ssh"
        else None
    )
    delivery = _input(
        "invalid-delivery-input",
        lambda: NodeDelivery(
            docker=docker,
            docker_socket=socket,
            ssh=ssh,
            ssh_agent_socket=args.ssh_agent_socket,
        ),
    )
    return _input(
        "invalid-delivery-input",
        lambda: delivery.deliver(
            source, target, grant, reference=args.ref, limits=limits
        ),
    )


if __name__ == "__main__":
    raise SystemExit(main())
