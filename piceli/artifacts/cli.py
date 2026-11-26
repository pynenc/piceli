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
from piceli.artifacts.retention_cli import add_retention_command, run_retention_command
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
    add_publish_command(sub)
    add_publish_files_commands(sub)
    add_copy_command(sub)
    add_retention_command(sub)
    return parser


def add_copy_command(sub: Any) -> None:
    """``artifacts copy`` (0.74.0): one image by digest, every platform and
    its referrers, to another registry. 0.83.0: a set, a ledger that resumes,
    whole-copy retries and ``--in-cluster``."""
    cmd = sub.add_parser(
        "copy",
        help="copy an image by digest (index, every platform, SBOM, provenance, "
        "signatures) to another registry",
    )
    cmd.add_argument(
        "--from",
        dest="source",
        help="oci://host[:port]/repository@sha256:<digest>",
    )
    cmd.add_argument("--to", help="oci://host[:port]/repository[:tag]")
    cmd.add_argument(
        "--set",
        dest="set_file",
        type=Path,
        help="a JSON or TOML list of {from, to} copies, instead of --from and "
        "--to; one plan digest approves them all",
    )
    cmd.add_argument(
        "--no-referrers",
        action="store_true",
        help="copy only the image, not its SBOM, provenance and signatures",
    )
    cmd.add_argument("--move-tag", action="store_true")
    cmd.add_argument("--approve", help="the plan digest printed without --approve")
    source = cmd.add_mutually_exclusive_group()
    source.add_argument("--from-credentials", type=Path)
    source.add_argument("--from-docker-config", type=Path)
    cmd.add_argument("--from-ca-file", type=Path)
    credentials = cmd.add_mutually_exclusive_group()
    credentials.add_argument("--credentials", type=Path)
    credentials.add_argument("--docker-config", type=Path)
    cmd.add_argument("--ca-file", type=Path)
    cmd.add_argument("--out", type=Path, help="write the copy receipt here")
    cmd.add_argument(
        "--timeout",
        type=float,
        default=1800,
        help="seconds one copy may take (with --in-cluster: the Job's deadline)",
    )
    cmd.add_argument(
        "--attempts",
        type=int,
        default=5,
        help="tries of a whole copy that failed for a transient reason "
        "(a dropped connection, HTTP 429/502/503/504), with a growing pause",
    )
    cmd.add_argument(
        "--ledger",
        help="a name that records each copy as done or failed; the same name "
        "again continues at the first copy not done",
    )
    cmd.add_argument(
        "--ledger-dir",
        type=Path,
        help="where the ledger files are (default: the user's state directory)",
    )
    cmd.add_argument(
        "--in-cluster",
        action="store_true",
        help="run the copy as a Job in the cluster (it reaches the cluster "
        "registry by its Service name; no port-forward); needs --kubeconfig "
        "and --context",
    )
    cmd.add_argument("--namespace", default="piceli-system")
    cmd.add_argument("--kubeconfig", type=Path)
    cmd.add_argument("--context")
    cmd.add_argument(
        "--credentials-secret",
        help="with --in-cluster: a Secret with keys source-username, "
        "source-password, source-token and the same for target",
    )
    cmd.add_argument(
        "--image", help="with --in-cluster: the Job's image (default: the controller's)"
    )
    job = sub.add_parser("copy-job-run", help=argparse.SUPPRESS)
    job.add_argument("--dir", type=Path, required=True)
    job.add_argument("--ledger", required=True)
    job.add_argument("--attempts", type=int, default=5)


def add_publish_command(sub: Any) -> None:
    """``artifacts publish``: images of a build receipt to a hosted registry."""
    cmd = sub.add_parser(
        "publish",
        help="push a build's images (every platform, one index) to a registry",
    )
    cmd.add_argument("--receipt", type=Path, required=True)
    cmd.add_argument(
        "--output-dir",
        type=Path,
        help="the build's output directory (default: the receipt's directory)",
    )
    cmd.add_argument("--to", required=True, help="oci://host[:port]/prefix")
    cmd.add_argument("--tag", required=True, help="the version tag, e.g. 1.4.0")
    cmd.add_argument(
        "--image", action="append", dest="images", help="publish only this image"
    )
    cmd.add_argument(
        "--no-attestations",
        action="store_true",
        help="do not attach the SBOM and provenance",
    )
    cmd.add_argument("--move-tag", action="store_true")
    cmd.add_argument("--approve", help="the plan digest printed without --approve")
    credentials = cmd.add_mutually_exclusive_group()
    credentials.add_argument("--credentials", type=Path)
    credentials.add_argument("--docker-config", type=Path)
    cmd.add_argument("--ca-file", type=Path)
    cmd.add_argument("--docker", type=Path)
    cmd.add_argument("--docker-sha256")
    cmd.add_argument("--docker-socket", type=Path)
    cmd.add_argument("--out", type=Path, help="write the publish receipt here")
    cmd.add_argument(
        "--values-out",
        type=Path,
        help="write the Helm values fragment (images.<key>.repository/tag/digest)",
    )
    cmd.add_argument(
        "--sign-key",
        type=Path,
        help="sign with cosign and this private key file (cosign generate-key-pair)",
    )
    cmd.add_argument("--cosign", type=Path)
    cmd.add_argument("--cosign-sha256")
    cmd.add_argument("--timeout", type=float, default=1800)


def add_publish_files_commands(sub: Any) -> None:
    """``artifacts publish-files`` and ``artifacts fetch-file`` (0.32.0)."""
    cmd = sub.add_parser(
        "publish-files",
        help="push local files as one OCI artifact (each file one layer, by its sha256)",
    )
    cmd.add_argument("files", nargs="*", type=Path, metavar="FILE")
    cmd.add_argument(
        "--receipt",
        type=Path,
        help="publish the [[output.files]] a host build receipt lists (0.39)",
    )
    cmd.add_argument(
        "--output-dir",
        type=Path,
        help="where the receipt's files are (default: the receipt's directory)",
    )
    cmd.add_argument("--to", required=True, help="oci://host[:port]/repository")
    cmd.add_argument("--tag", required=True, help="the artifact's tag")
    cmd.add_argument(
        "--artifact-type",
        required=True,
        help="the manifest's artifactType, e.g. application/vnd.example.downloads.v1",
    )
    cmd.add_argument("--move-tag", action="store_true")
    cmd.add_argument("--approve", help="the plan digest printed without --approve")
    credentials = cmd.add_mutually_exclusive_group()
    credentials.add_argument("--credentials", type=Path)
    credentials.add_argument("--docker-config", type=Path)
    cmd.add_argument("--ca-file", type=Path)
    cmd.add_argument("--out", type=Path, help="write the receipt here")
    cmd.add_argument("--timeout", type=float, default=1800)
    cmd = sub.add_parser(
        "fetch-file", help="download one blob (a published file) by its digest"
    )
    cmd.add_argument(
        "--from", dest="source", required=True, help="oci://host/repository"
    )
    cmd.add_argument("--digest", required=True, help="sha256:<hex> of the file")
    cmd.add_argument("--out", type=Path, required=True, help="where to write it")
    credentials = cmd.add_mutually_exclusive_group()
    credentials.add_argument("--credentials", type=Path)
    credentials.add_argument("--docker-config", type=Path)
    cmd.add_argument("--ca-file", type=Path)


def main(arguments: list[str] | None = None) -> int:
    args = build_parser().parse_args(arguments)
    if args.command == "build-spec":
        return run_build_spec_command(args)
    if args.command == "publish":
        return run_publish_command(args)
    if args.command == "publish-files":
        return run_publish_files_command(args)
    if args.command == "fetch-file":
        return run_fetch_file_command(args)
    if args.command == "copy":
        return run_copy_command(args)
    if args.command == "copy-job-run":
        return run_copy_job_command(args)
    if args.command == "retention":
        return run_retention_command(args)
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
        if args.kubeconfig is None or args.namespace is None or not args.context:
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


def run_publish_command(args: argparse.Namespace, **seams: Any) -> int:
    """Plan (exit 3), or publish an approved plan (exit 0, or 1 on failure)."""
    from piceli.artifacts.build_spec import BuildReceipt, BuildSpecError
    from piceli.artifacts.publish import (
        Publisher,
        PublishError,
        PublishGrant,
        PublishPlan,
    )
    from piceli.artifacts.registry import docker_config_credentials
    from piceli.cli_contract import EXIT_APPROVAL, emit_json, say

    try:
        signer = None
        if args.sign_key is not None:
            from piceli.artifacts.signing import CosignSigner

            signer = CosignSigner(
                discover_tool("cosign", args.cosign, args.cosign_sha256),
                args.sign_key.absolute(),
                **seams.pop("signer_options", {}),
            )
        elif args.cosign is not None or args.cosign_sha256 is not None:
            raise PublishError("publish-invalid", "--cosign needs --sign-key")
        receipt = BuildReceipt.from_json(args.receipt.read_text())
        output_dir = args.output_dir or args.receipt.absolute().parent
        plan = PublishPlan.from_receipt(
            receipt,
            output_dir,
            args.to,
            args.tag,
            images=args.images,
            attach=not args.no_attestations,
            move_tag=args.move_tag,
            signing=signer.public() if signer is not None else None,
        )
        if args.approve is None or args.approve != plan.digest:
            if args.approve is not None:
                raise PublishError("publish-not-approved")
            # 0.57.0: plan_hash, as every plan document names its approval
            emit_json(
                {
                    "state": "approval-required",
                    **plan.preview(),
                    "plan_hash": plan.digest,
                }
            )
            say(
                f"publish: {len(plan.images)} image(s) to {plan.target.registry} "
                f"as {plan.tag}; approve with --approve {plan.digest}"
            )
            return EXIT_APPROVAL
        credentials = None
        if args.credentials is not None:
            credentials = _input(
                "invalid-credentials-file",
                lambda: RegistryCredentials.load(args.credentials.absolute()),
            )
        elif args.docker_config is not None:
            credentials = docker_config_credentials(
                args.docker_config.absolute(), plan.target.registry
            )
        if signer is not None:  # cosign reaches the registry like the push
            signer.credentials = credentials
            signer.ca_file = args.ca_file.absolute() if args.ca_file else None
        docker, socket = (None, None)
        if any(
            item.archive is None for image in plan.images for item in image.platforms
        ):
            docker, socket = _docker(args)
        publisher = Publisher(
            credentials=credentials,
            ca_file=args.ca_file.absolute() if args.ca_file is not None else None,
            docker=docker,
            docker_socket=socket,
            signer=signer,
            timeout=args.timeout,
            **seams,
        )
        result = publisher.publish(
            plan, PublishGrant(plan.digest, time.time() + args.timeout)
        )
        if args.out is not None:
            write_receipt(args.out, result)
        if args.values_out is not None and result["values"] is not None:
            _write_values(args.values_out, result["values"])
        emit_json(result)
        if result["state"] != "published":
            reason = (
                result["reason"] if result["reason"] in ERRORS else "publish-failed"
            )
            say(f"publish: failed [{reason}]")
            return 1
        for name, entry in result["images"].items():
            say(
                f"published {name}: {entry['repository']}:{entry['tag']}@{entry['digest']}"
            )
        return 0
    except (PublishError, DeliveryInputError, BuildSpecError) as error:
        return rejection(error.code if error.code in ERRORS else "publish-invalid")
    except (ValueError, KeyError, TypeError, OSError):
        return rejection("publish-invalid")


def _registry_credentials(
    args: argparse.Namespace, registry: str
) -> RegistryCredentials | None:
    """``--credentials FILE`` or ``--docker-config FILE`` for ``registry``."""
    from piceli.artifacts.registry import docker_config_credentials

    if args.credentials is not None:
        return _input(
            "invalid-credentials-file",
            lambda: RegistryCredentials.load(args.credentials.absolute()),
        )
    if args.docker_config is not None:
        return docker_config_credentials(args.docker_config.absolute(), registry)
    return None


def _state_dir() -> Path:
    """``$XDG_STATE_HOME/piceli/copy`` (default ``~/.local/state/piceli/copy``)."""
    import os

    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "piceli" / "copy"


def _copy_items(args: argparse.Namespace) -> list[Any]:
    """The copies asked for: ``--from``/``--to`` or ``--set`` (the credentials
    flags apply to every copy that names none)."""
    from piceli.artifacts.copy_set import CopyItem, load_set_file
    from piceli.artifacts.image_copy import CopyError

    def path(value: Path | None) -> str | None:
        return str(value.absolute()) if value is not None else None

    from_path = path(args.from_credentials or args.from_docker_config)
    to_path = path(args.credentials or args.docker_config)
    if args.set_file is not None:
        if args.source or args.to:
            raise CopyError("copy-invalid", "give --set or --from and --to, not both")
        base = load_set_file(args.set_file.absolute())
    else:
        if not args.source or not args.to:
            raise CopyError("copy-invalid", "give --from and --to, or --set")
        base = [CopyItem(args.source, args.to)]
    return [
        CopyItem(
            item.source,
            item.target,
            item.source_credentials or from_path,
            item.target_credentials or to_path,
        )
        for item in base
    ]


def _copy_say_plan(plan: Any) -> None:
    from piceli.cli_contract import say

    lines = []
    for each in plan.plans:
        tag = f":{each.target.tag}" if each.target.tag else ""
        where = f"{each.target.registry}/{each.target.repository}{tag}"
        lines.append(f"copy: {each.source.digest} to {where}")
    if len(lines) == 1:
        say(f"{lines[0]}; approve with --approve {plan.digest}")
        return
    for line in lines:
        say(line)
    say(f"copy: {len(lines)} copies; approve with --approve {plan.digest}")


def _cluster_copy(
    args: argparse.Namespace, items: list[Any], options: Any, ledger: str, **seams: Any
) -> Any:
    """``--in-cluster``: the Job's report."""
    from piceli.artifacts.copy_cluster import CopyCluster, copy_in_cluster, secret_data
    from piceli.artifacts.copy_set import CopySetError, load_credentials
    from piceli.artifacts.image_copy import CopySource
    from piceli.cli_contract import say
    from piceli.k8s.ops.provider_factory import api_client_from_kubeconfig

    if args.kubeconfig is None or not args.context:
        raise CopySetError(
            "copy-invalid", "--in-cluster needs --kubeconfig and --context"
        )
    if options.ca_file is not None or options.source_ca_file is not None:
        raise CopySetError("copy-invalid", "--ca-file does not apply with --in-cluster")
    sources = {item.source_credentials for item in items}
    targets = {item.target_credentials for item in items}
    if len(sources) > 1 or len(targets) > 1:
        raise CopySetError(
            "copy-invalid", "--in-cluster uses one set of credentials per registry side"
        )
    local = args.credentials_secret is None
    client = seams.pop("kube", None) or api_client_from_kubeconfig(
        args.kubeconfig.absolute(), args.context
    )
    cluster = seams.pop("cluster", None) or CopyCluster(client, args.namespace)
    try:
        secret = args.credentials_secret
        made = None
        if local:
            first = items[0]
            source = load_credentials(
                first.source_credentials,
                CopySource.parse(first.source).repository.registry,
            )
            target = load_credentials(
                first.target_credentials, _target_registry(first.target)
            )
            data = secret_data(source, target)
            if data:
                made = secret = f"piceli-copy-{ledger}-credentials"[:253]
                cluster.apply_secret(made, data)
        elif args.credentials or args.docker_config or args.from_credentials:
            raise CopySetError(
                "copy-invalid",
                "give --credentials-secret or credentials files, not both",
            )
        report = copy_in_cluster(
            items,
            ledger=ledger,
            namespace=args.namespace,
            credentials_secret=secret,
            kube=client,
            attempts=args.attempts,
            deadline=args.timeout,
            image=args.image,
            options=options,
            cluster=cluster,
            say=lambda line: say(line),
        )
        if made is not None:  # kept when the client is interrupted: a rerun needs it
            cluster.delete_secret(made)
        return report
    finally:
        cluster.close()


def _target_registry(target: str) -> str:
    return RegistryTarget.parse(target).registry


def run_copy_command(args: argparse.Namespace, **seams: Any) -> int:
    """Plan (exit 3), or copy an approved plan (exit 0, or 1 on failure).

    0.83.0: ``--set`` copies several images under one approval; a failed copy
    with a transient reason is tried again (``--attempts``); ``--ledger``
    records each copy so a rerun continues; ``--in-cluster`` runs the copies
    as a Job. Without those flags the output is the single copy's receipt.
    """
    from piceli.artifacts.copy_set import (
        CopyOptions,
        CopySetError,
        CopySetPlan,
        FileLedger,
        MemoryLedger,
        prepare_copies,
        run_copy_set,
        validate_name,
    )
    from piceli.artifacts.image_copy import CopyError, summary
    from piceli.artifacts.registry import describe
    from piceli.cli_contract import EXIT_APPROVAL, emit_json, say

    sleep = seams.pop("sleep", time.sleep)
    try:
        items = _copy_items(args)
        options = CopyOptions(
            referrers=not args.no_referrers,
            move_tag=args.move_tag,
            timeout=args.timeout,
            source_ca_file=args.from_ca_file.absolute() if args.from_ca_file else None,
            ca_file=args.ca_file.absolute() if args.ca_file else None,
        )
        plan = CopySetPlan.parse(items, options)
        if args.approve is None or args.approve != plan.digest:
            if args.approve is not None:
                raise CopyError("copy-not-approved")
            emit_json(
                {
                    "state": "approval-required",
                    **plan.preview(),
                    "plan_hash": plan.digest,
                }
            )
            _copy_say_plan(plan)
            return EXIT_APPROVAL
        name = args.ledger or (
            f"set-{plan.digest.split(':')[1][:12]}" if args.in_cluster else "copy"
        )
        validate_name(name)
        if args.in_cluster:
            report = _cluster_copy(args, items, options, name, **seams)
        else:
            store = (
                FileLedger((args.ledger_dir or _state_dir()).absolute())
                if args.ledger
                else MemoryLedger()
            )
            report = run_copy_set(
                items,
                ledger=name,
                store=store,
                attempts=args.attempts,
                sleep=sleep,
                say=lambda line: say(line),
                copy=prepare_copies(items, options, **seams),
            )
        single = args.set_file is None
        first = report.items[0]
        if single:
            result: dict[str, Any] = first.receipt or {
                "state": "copied" if first.state == "done" else "failed",
                "reason": first.reason,
                "result": "already-done" if first.skipped else None,
                "digest": plan.plans[0].source.digest,
                "ledger": name,
                "receipt_digest": first.receipt_digest,
            }
            if report.retries:
                result = {**result, "copy_retries": report.retries}
        else:
            result = report.to_dict()
        if args.out is not None:
            write_receipt(args.out, result)
        emit_json(result)
        if not report.ok:
            reason = report.failed or "copy-failed"
            reason = reason if reason in ERRORS else "copy-failed"
            where = describe((first.receipt or {}).get("error")) if single else ""
            say(f"copy: failed [{reason}]" + (f" {where}" if where else ""))
            return 1
        if single and first.receipt is not None:
            say(summary(first.receipt))
        else:
            say(f"copy: {sum(i.state == 'done' for i in report.items)} copied")
        return 0
    except (CopyError, CopySetError, DeliveryInputError) as error:
        return rejection(error.code if error.code in ERRORS else "copy-invalid")
    except (ValueError, KeyError, TypeError, OSError):
        return rejection("copy-invalid")


def run_copy_job_command(args: argparse.Namespace, **seams: Any) -> int:
    """Inside the Job of ``artifacts copy --in-cluster`` (not for manual use):
    copy the mounted set and print the ledger and the report as lines."""
    import os

    from piceli.artifacts.copy_cluster import run_job
    from piceli.artifacts.copy_set import CopySetError
    from piceli.artifacts.image_copy import CopyError
    from piceli.cli_contract import say

    def emit(line: str) -> None:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    try:
        report = run_job(
            args.dir,
            ledger=args.ledger,
            attempts=args.attempts,
            environ=os.environ,
            emit=emit,
            say=lambda line: say(line),
            **seams,
        )
    except (CopyError, CopySetError, DeliveryInputError) as error:
        return rejection(error.code if error.code in ERRORS else "copy-invalid")
    except (ValueError, KeyError, TypeError, OSError):
        return rejection("copy-invalid")
    return 0 if report.ok else 1


def run_publish_files_command(args: argparse.Namespace, **seams: Any) -> int:
    """Plan (exit 3), or push an approved files artifact (exit 0, or 1 on failure)."""
    from piceli.artifacts.publish import PublishError, PublishGrant
    from piceli.artifacts.publish_files import FilePublisher, FilesPlan
    from piceli.cli_contract import EXIT_APPROVAL, emit_json, say

    try:
        receipt = getattr(args, "receipt", None)
        if (receipt is None) == (not args.files):
            raise PublishError(
                "publish-invalid", "give the files or --receipt, not both"
            )
        if receipt is not None:
            plan = FilesPlan.from_receipt(
                receipt,
                args.to,
                args.tag,
                args.artifact_type,
                output_dir=getattr(args, "output_dir", None),
                move_tag=args.move_tag,
            )
        else:
            plan = FilesPlan.from_paths(
                args.files,
                args.to,
                args.tag,
                args.artifact_type,
                move_tag=args.move_tag,
            )
        if args.approve is None or args.approve != plan.digest:
            if args.approve is not None:
                raise PublishError("publish-not-approved")
            # 0.57.0: plan_hash, as every plan document names its approval
            emit_json(
                {
                    "state": "approval-required",
                    **plan.preview(),
                    "plan_hash": plan.digest,
                }
            )
            for item in plan.files:
                say(f"  {item.name}  {item.size} bytes  {item.sha256}")
            say(
                f"publish-files: {len(plan.files)} file(s) to "
                f"{plan.target.registry}/{plan.target.repository}:{plan.tag} "
                f"(manifest {plan.manifest_digest}); approve with --approve {plan.digest}"
            )
            return EXIT_APPROVAL
        credentials = _registry_credentials(args, plan.target.registry)
        publisher = FilePublisher(
            credentials=credentials,
            ca_file=args.ca_file.absolute() if args.ca_file is not None else None,
            timeout=args.timeout,
            **seams,
        )
        result = publisher.publish(
            plan, PublishGrant(plan.digest, time.time() + args.timeout)
        )
        if args.out is not None:
            write_receipt(args.out, result)
        emit_json(result)
        if result["state"] != "published":
            reason = (
                result["reason"] if result["reason"] in ERRORS else "publish-failed"
            )
            say(f"publish-files: failed [{reason}]")
            return 1
        say(
            f"published {result['repository']}:{result['tag']}@{result['manifest_digest']}"
        )
        return 0
    except (PublishError, DeliveryInputError) as error:
        return rejection(error.code if error.code in ERRORS else "publish-invalid")
    except (ValueError, KeyError, TypeError, OSError):
        return rejection("publish-invalid")


def run_fetch_file_command(args: argparse.Namespace, **seams: Any) -> int:
    """Download one published file by digest (exit 0, 1 on failure, 2 refused)."""
    from piceli.artifacts.publish import PublishError
    from piceli.artifacts.publish_files import fetch_file
    from piceli.artifacts.registry import RegistryError
    from piceli.cli_contract import emit_json, say

    try:
        validate_digest(args.digest)
        target = RegistryTarget.parse(args.source)
        credentials = _registry_credentials(args, target.registry)
        result = fetch_file(
            args.source,
            args.digest,
            args.out,
            credentials=credentials,
            ca_file=args.ca_file.absolute() if args.ca_file is not None else None,
            **seams,
        )
    except RegistryError as error:
        reason = error.reason if error.reason in ERRORS else "registry-error"
        emit_json({"state": "failed", "reason": reason, "digest": args.digest})
        say(f"fetch-file: failed [{reason}]")
        return 1
    except (PublishError, DeliveryInputError) as error:
        return rejection(error.code if error.code in ERRORS else "publish-invalid")
    except (ValueError, KeyError, TypeError, OSError):
        return rejection("publish-invalid")
    emit_json(result)
    say(f"fetched {result['digest']} ({result['size']} bytes)")
    return 0


def _write_values(path: Path, values: dict[str, Any]) -> None:
    """The Helm values fragment as YAML (``helm install -f`` / ``piceli chart``)."""
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.piceli-partial")
    try:
        temporary.write_text(yaml.safe_dump(values, sort_keys=True))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
