"""The command-line contract: output helpers and per-command metadata.

Convention (see ``docs/agents.md``):

- **stdout** carries machine output only: exactly one JSON object per
  command (JSON lines for streaming commands), written by :func:`emit_json`.
- **stderr** carries human text only (summaries, hints), written by :func:`say`.
- A refusal prints ``{"state": "rejected", "reason": "<code>"}`` on stdout and
  a one-line human explanation on stderr, then exits ``2``. Every ``<code>`` is
  an entry of :data:`piceli.errors.ERRORS` (``piceli explain <code>``).
- Exit codes: ``0`` success, ``1`` the operation ran but did not succeed (not
  ready, drift, build failed), ``2`` rejected before any change, ``3`` approval
  required (nothing was executed).

:data:`COMMANDS` records, for every command, what it touches and whether it
needs approval; ``piceli help-json`` merges it into the generated CLI tree.

Importing this module is side-effect free.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, NoReturn

from piceli.errors import ERRORS

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REJECTED = 2
EXIT_APPROVAL = 3

EXIT_CODES: Mapping[int, str] = MappingProxyType(
    {
        EXIT_OK: "success",
        EXIT_FAILED: "the operation ran but did not succeed (not ready, drift, build failed)",
        EXIT_REJECTED: "rejected before any change (stdout: the rejection object)",
        EXIT_APPROVAL: "approval required; nothing was executed",
    }
)

HELP_SCHEMA = "piceli.help.v1"


def emit_json(value: Mapping[str, Any]) -> None:
    """Print one JSON object on stdout (sorted keys, one line)."""
    sys.stdout.write(json.dumps(value, sort_keys=True, default=str) + "\n")
    sys.stdout.flush()


def say(message: str) -> None:
    """Print human text on stderr."""
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


class Rejected(SystemExit):
    """Raised by :func:`reject` after printing; carries the exit status."""

    def __init__(self, reason: str, status: int) -> None:
        super().__init__(status)
        self.reason = reason


def reject(code: str, *, exit_code: int = EXIT_REJECTED) -> NoReturn:
    """Print the rejection for ``code`` and exit with ``exit_code``.

    ``code`` must be registered in :data:`piceli.errors.ERRORS`; an
    unregistered code is a programming error and raises ``ValueError``.
    """
    entry = ERRORS.get(code)
    if entry is None:
        raise ValueError(f"unregistered error code {code!r}")
    emit_json({"state": "rejected", "reason": code})
    say(f"rejected: {entry.title} ({code}). {entry.fix}")
    raise Rejected(code, exit_code)


# ---------------------------------------------------------------- metadata


@dataclass(frozen=True)
class CommandContract:
    """What one command touches, and how an agent may run it.

    :param summary: One line; used when the command definition has no help.
    :param reads: Local inputs it reads (files, tools, daemons).
    :param writes: Local state it writes; empty for read-only commands.
    :param cluster: ``"none"``, ``"reads"`` or ``"writes"`` (via an explicit
        kubeconfig), or ``"ambient-writes"`` / ``"ambient-reads"`` for legacy
        commands that use the current kube context.
    :param approval_required: The command changes a cluster, registry or node,
        or runs code, only with an explicit approval (hash/digest/flag).
    :param safe_to_retry: Re-running it after an interruption cannot cause
        harm (it is read-only, idempotent or resumable).
    :param contract: ``"conforms"`` (this module's convention), ``"partial"``
        (JSON on stdout, but refusals differ) or ``"legacy"`` (human output).
    """

    summary: str
    reads: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    cluster: str = "none"
    approval_required: bool = False
    safe_to_retry: bool = True
    long_running: bool = False
    contract: str = "partial"
    exit_codes: tuple[int, ...] = (0, 2)
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "side_effects": {
                "reads": list(self.reads),
                "writes": list(self.writes),
                "cluster": self.cluster,
                "contacts_cluster": self.cluster != "none",
                "read_only": not self.writes
                and self.cluster in {"none", "reads", "ambient-reads"},
            },
            "approval_required": self.approval_required,
            "safe_to_retry": self.safe_to_retry,
            "long_running": self.long_running,
            "contract": self.contract,
            "exit_codes": {str(code): EXIT_CODES[code] for code in self.exit_codes},
            **({"notes": self.notes} if self.notes else {}),
        }


_C = CommandContract
_RELEASE_READS = ("release.toml", "composition", "state_dir", "kubeconfig")
_RELEASE_EXIT = (0, 1, 2, 3)
_LEGACY_MODEL = ("model modules/folders",)

COMMANDS: Mapping[str, CommandContract] = MappingProxyType(
    {
        # ----------------------------------------------------- contract
        "render": _C(
            "Print the manifests of a typed app or composition (YAML or JSON).",
            reads=("module/app file", "release.toml (optional)", "local receipts"),
            notes="Never contacts a cluster; secret values are placeholders.",
        ),
        "explain": _C(
            "Print the registry entry for an error code.",
            contract="conforms",
        ),
        "help-json": _C(
            "Print the whole CLI tree, with contracts, as JSON.",
            contract="conforms",
            exit_codes=(0,),
        ),
        # ------------------------------------------------------ release
        "release plan": _C(
            "Capture live discovery and persist an approvable plan.",
            reads=_RELEASE_READS,
            writes=("state_dir (pending plan, secret candidates)", "--out file"),
            cluster="reads",
            notes="Never changes the cluster. Prints the plan hash to approve.",
        ),
        "release preview": _C(
            "Alias of `release plan`.",
            reads=_RELEASE_READS,
            writes=("state_dir (pending plan, secret candidates)", "--out file"),
            cluster="reads",
        ),
        "release apply": _C(
            "Execute an approved plan (--approve HASH).",
            reads=_RELEASE_READS,
            writes=("state_dir (catalog, journal, secret store)",),
            cluster="writes",
            approval_required=True,
            safe_to_retry=False,
            exit_codes=_RELEASE_EXIT,
            notes="After an interruption use `release resume`, not apply.",
        ),
        "release rollback": _C(
            "Re-plan and re-apply an earlier release.",
            reads=_RELEASE_READS,
            writes=("state_dir (catalog, journal)",),
            cluster="writes",
            approval_required=True,
            safe_to_retry=False,
            exit_codes=_RELEASE_EXIT,
        ),
        "release resume": _C(
            "Resume an interrupted apply with the same grant and ids.",
            reads=_RELEASE_READS,
            writes=("state_dir (journal)",),
            cluster="writes",
            approval_required=False,
            safe_to_retry=True,
            exit_codes=(0, 1, 2),
            notes="Continues an already approved execution; needs no new approval.",
        ),
        "release stop": _C(
            "Cancel the latest execution of a release.",
            reads=_RELEASE_READS,
            writes=("state_dir (journal)",),
            cluster="reads",
            safe_to_retry=True,
            notes="Checks the cluster identity; records the cancellation locally.",
        ),
        "release secret show": _C(
            "Show a generated secret's metadata, or its value with --reveal.",
            reads=("release.toml", "state_dir (secret store)"),
            notes="Owner only. Never contacts the cluster; values print only with --reveal or a terminal confirmation.",
        ),
        "release status": _C(
            "Show catalogued releases, executions and history.",
            reads=("release.toml", "state_dir"),
        ),
        # ------------------------------------------------------- inputs
        "inputs record": _C(
            "Capture the git identity of declared sources.",
            reads=("inputs.toml", "git work trees"),
            writes=("--out lock file",),
            exit_codes=(0, 2),
            notes="Refusal reasons are free text today (D0b).",
        ),
        "inputs verify": _C(
            "Compare sources with a lock; exit 1 on drift.",
            reads=("inputs.toml", "lock", "git work trees"),
            exit_codes=(0, 1, 2),
            notes="Refusal reasons are free text today (D0b).",
        ),
        # ---------------------------------------------------- artifacts
        "artifacts preview": _C(
            "Preview a deterministic OCI build plan (no tools run).",
            reads=("plan JSON",),
        ),
        "artifacts build": _C(
            "Assemble an OCI image layout from a plan without running code.",
            reads=("plan JSON", "source root"),
            writes=("--output layout",),
        ),
        "artifacts pin": _C(
            "Pin one public source file by digest.",
            reads=("source file",),
        ),
        "artifacts inspect": _C(
            "Recheck an OCI layout and print its summary.",
            reads=("OCI layout",),
        ),
        "artifacts import-local": _C(
            "Import an OCI layout into the local Docker daemon.",
            reads=("OCI layout", "docker"),
            writes=("local Docker image store",),
            approval_required=True,
        ),
        "artifacts preview-command": _C(
            "Preview a pinned external build command.",
            reads=("command spec",),
        ),
        "artifacts execute-command": _C(
            "Run a pinned external build command under an approved plan.",
            reads=("command spec", "source root", "pinned tool"),
            writes=("tool outputs",),
            approval_required=True,
            safe_to_retry=False,
            exit_codes=(0, 1, 2),
        ),
        "artifacts deliver": _C(
            "Deliver a digest-approved image to a registry or node.",
            reads=("docker image or archive", "credentials file", "kubeconfig"),
            writes=("registry or node image store", "--receipt", "--journal"),
            cluster="reads",
            approval_required=True,
            safe_to_retry=True,
            exit_codes=(0, 1, 2),
            notes="Registry pushes are content-addressed and idempotent. "
            "Contacts the cluster only for --via-forward (port-forward).",
        ),
        "artifacts build-spec preview": _C(
            "Preview a containerized build and its plan hash.",
            reads=("build.toml", "inputs.toml", "build contexts"),
        ),
        "artifacts build-spec run": _C(
            "Run an approved containerized build and write a receipt.",
            reads=("build.toml", "inputs", "build contexts", "docker"),
            writes=("--out receipt", "--output-dir", "local Docker image store"),
            approval_required=True,
            safe_to_retry=True,
            long_running=True,
            exit_codes=(0, 1, 2),
        ),
        # ------------------------------------------------------ observe
        "observe status": _C(
            "Compare a session archive with live cluster state.",
            reads=("session archive", "kubeconfig"),
            cluster="reads",
        ),
        "observe forward-save": _C(
            "Save one port-forward preference.",
            reads=("session archive",),
            writes=("preferences file",),
        ),
        "observe forward-list": _C(
            "List saved port-forward preferences.",
            reads=("preferences file",),
        ),
        "observe forward-command": _C(
            "Print the kubectl argv for one saved forward.",
            reads=("preferences file",),
        ),
        "observe forward-run": _C(
            "Run one saved port forward until interrupted.",
            reads=("preferences file", "kubeconfig"),
            cluster="reads",
            long_running=True,
        ),
        "observe logs-command": _C(
            "Print the kubectl argv for a bounded log request.",
        ),
        "observe logs-run": _C(
            "Run a bounded log request in the foreground.",
            reads=("kubeconfig",),
            cluster="reads",
            long_running=True,
        ),
        "observe serve": _C(
            "Serve the local operations UI and JSON API on loopback.",
            reads=("session archive", "kubeconfig", "preferences file"),
            writes=("preferences file",),
            cluster="reads",
            long_running=True,
        ),
        "observe forwards apply": _C(
            "Start and supervise the forwards of an access profile.",
            reads=("access profile", "kubeconfig"),
            cluster="reads",
            long_running=True,
            exit_codes=(0, 2),
        ),
        "observe forwards status": _C(
            "Report the health of the forwards of an access profile.",
            reads=("access profile",),
            exit_codes=(0, 1),
        ),
        # ----------------------------------------------------- operator
        "operator status": _C(
            "Print classified operator inventory.",
            reads=("kubeconfig", "archive", "catalog"),
            cluster="reads",
        ),
        "operator promote": _C(
            "Promote a built digest to a new catalog entry.",
            reads=("catalog",),
            writes=("catalog",),
            safe_to_retry=False,
        ),
        "operator approve": _C(
            "Record an operator approval for a pull request rollout.",
            writes=("state_dir",),
            approval_required=True,
        ),
        "operator backup": _C(
            "Create a verified backup archive of operator state.",
            reads=("state_dir",),
            writes=("backup archive",),
        ),
        "operator restore": _C(
            "Restore operator state into an empty directory.",
            reads=("backup archive",),
            writes=("destination directory",),
            safe_to_retry=False,
        ),
        "operator serve": _C(
            "Serve the operator dashboard and REST API on loopback.",
            reads=("kubeconfig", "catalog", "archive", "preferences file"),
            writes=("preferences file", "state_dir"),
            cluster="reads",
            long_running=True,
        ),
        # ---------------------------------------------- legacy CLI engine
        "model list": _C(
            "List the objects loaded from the model.",
            reads=_LEGACY_MODEL,
            contract="legacy",
        ),
        # ------------------------------------------------ P5 pipeline
        "deploy": _C(
            "Deploy a pipeline from source: inputs, build, deliver, plan, apply, checks.",
            reads=(
                "pipeline module",
                "build specs and sources",
                "docker",
                "kubeconfig",
                "state_dir",
            ),
            writes=(
                "state_dir (run journal, receipts, release catalog, secret store)",
                "local Docker image store",
                "registry or node image store",
            ),
            cluster="writes",
            approval_required=True,
            safe_to_retry=True,
            long_running=True,
            contract="conforms",
            exit_codes=(0, 1, 2, 3),
            notes="--plan never changes the cluster, a registry or a node; "
            "--approve HASH executes exactly the combined plan; --resume continues "
            "the latest interrupted run without a new approval. Unchanged stages "
            "are skipped.",
        ),
    }
)


# --------------------------------------------------------------- help tree


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    return str(value)


_TYPE_NAMES = {
    "str": "text",
    "string": "text",
    "int": "integer",
    "int range": "integer",
    "integer range": "integer",
    "float range": "float",
    "bool": "boolean",
    "file": "path",
    "directory": "path",
    "filename": "path",
}


def _type_name(name: str) -> str:
    """Normalize a Click/Typer type name to text/integer/float/boolean/path/choice."""
    name = name.lower()
    return _TYPE_NAMES.get(name, name)


def _click_param(param: Any) -> dict[str, Any]:
    kind = "argument" if param.param_type_name == "argument" else "option"
    type_name = _type_name(getattr(param.type, "name", str(param.type)))
    entry: dict[str, Any] = {
        "name": param.name,
        "kind": kind,
        "type": "boolean" if getattr(param, "is_flag", False) else type_name,
        "required": bool(param.required),
        "multiple": bool(getattr(param, "multiple", False)),
    }
    if kind == "option":
        entry["flags"] = list(param.opts) + list(param.secondary_opts)
        entry["help"] = param.help or ""
        if param.envvar:
            entry["envvar"] = param.envvar
    choices = getattr(param.type, "choices", None)
    if choices:
        entry["choices"] = [_jsonable(item) for item in choices]
    if not param.required:
        default = param.default
        entry["default"] = None if callable(default) else _jsonable(default)
    return entry


def _click_tree(command: Any, path: tuple[str, ...]) -> dict[str, Any]:
    name = " ".join(path)
    help_text = (command.help or command.short_help or "").strip()
    params = [
        _click_param(param)
        for param in command.params
        if not getattr(param, "hidden", False)
        and param.name not in {"help", "install_completion", "show_completion"}
    ]
    node: dict[str, Any] = {"name": path[-1] if path else "piceli", "path": name}
    if hasattr(command, "commands"):
        node["help"] = help_text
        node["params"] = params
        node["commands"] = [
            _click_tree(sub, (*path, sub_name))
            for sub_name, sub in sorted(command.commands.items())
            if not getattr(sub, "hidden", False)
        ]
        return node
    contract = COMMANDS.get(name)
    node["help"] = help_text or (contract.summary if contract else "")
    node["params"] = params
    node["contract"] = contract.to_dict() if contract else None
    return node


def _argparse_type(action: Any) -> str:
    import argparse
    from pathlib import Path

    if isinstance(action, argparse._StoreTrueAction | argparse._StoreFalseAction):
        return "boolean"
    return {
        Path: "path",
        float: "float",
        int: "integer",
        None: "text",
        str: "text",
    }.get(action.type, getattr(action.type, "__name__", "text"))


def _argparse_tree(parser: Any, path: tuple[str, ...]) -> dict[str, Any]:
    import argparse

    name = " ".join(path)
    params: list[dict[str, Any]] = []
    children: list[dict[str, Any]] = []
    exclusive = [
        sorted(action.dest for action in group._group_actions)
        for group in parser._mutually_exclusive_groups
    ]
    for action in parser._actions:
        if isinstance(action, argparse._HelpAction):
            continue
        if isinstance(action, argparse._SubParsersAction):
            for sub_name, sub in sorted(action.choices.items()):
                children.append(_argparse_tree(sub, (*path, sub_name)))
            continue
        entry: dict[str, Any] = {
            "name": action.dest,
            "kind": "option" if action.option_strings else "argument",
            "type": _argparse_type(action),
            "required": bool(action.required),
            "multiple": False,
            "flags": list(action.option_strings),
            "help": action.help or "",
        }
        if not action.required:
            entry["default"] = _jsonable(action.default)
        params.append(entry)
    node: dict[str, Any] = {"name": path[-1], "path": name, "params": params}
    if exclusive:
        node["mutually_exclusive"] = exclusive
    if children:
        node["help"] = parser.description or ""
        node["commands"] = children
        return node
    contract = COMMANDS.get(name)
    node["help"] = parser.description or (contract.summary if contract else "")
    node["contract"] = contract.to_dict() if contract else None
    return node


def help_tree() -> dict[str, Any]:
    """The whole ``piceli`` command tree with option types and contracts.

    Built from the Typer application and the ``artifacts`` argparse parser.
    Loading the command modules is side-effect free (no cluster, no files).
    """
    import importlib.metadata

    import typer.main

    from piceli.artifacts.cli import build_parser
    from piceli.errors import AREAS
    from piceli.k8s.cli import app

    root = _click_tree(typer.main.get_command(app), ())
    artifacts = _argparse_tree(build_parser(), ("artifacts",))
    artifacts["help"] = (
        "Deterministic OCI builds, containerized builds and image delivery."
    )
    root["commands"] = sorted(
        [*root["commands"], artifacts], key=lambda item: item["name"]
    )
    try:
        version = importlib.metadata.version("piceli")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover
        version = "unknown"
    return {
        "schema": HELP_SCHEMA,
        "program": "piceli",
        "version": version,
        "conventions": {
            "stdout": "machine output: one JSON object (JSON lines for streams)",
            "stderr": "human text: summaries and hints",
            "rejection": '{"state": "rejected", "reason": "<error code>"} on stdout, exit 2',
            "explain": "piceli explain <code> [--json]",
            "exit_codes": {str(code): text for code, text in EXIT_CODES.items()},
            "error_areas": dict(AREAS),
        },
        "error_codes": sorted(ERRORS),
        "root": root,
    }


def leaf_commands(node: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Every runnable command below ``node`` in a :func:`help_tree` tree."""
    if "commands" not in node:
        return [node]
    return [leaf for child in node["commands"] for leaf in leaf_commands(child)]
