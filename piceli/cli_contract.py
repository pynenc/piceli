"""The command-line contract: output helpers and per-command metadata.

Convention (see ``docs/agents.md``):

- **stdout** carries machine output only: exactly one JSON object per
  command (JSON lines for streaming commands), written by :func:`emit_json`.
- **stderr** carries human text only (summaries, hints), written by :func:`say`.
- A refusal prints ``{"state": "rejected", "reason": "<code>", "message":
  "<human text>", …}`` on stdout (:func:`reject`) and the explanation on
  stderr, then exits ``2``. An operation that ran but did not succeed exits
  ``1`` and names its code in ``reason`` (:func:`fail` prints
  ``{"state": "failed", …}``). Every ``<code>`` is an entry of
  :data:`piceli.errors.ERRORS` (``piceli explain <code>``).
- Exit codes: ``0`` success, ``1`` the operation ran but did not succeed (not
  ready, drift, build failed), ``2`` rejected before any change, ``3`` approval
  required (nothing was executed).

:data:`COMMANDS` records, for every command, what it touches and whether it
needs approval; ``piceli help-json`` merges it into the generated CLI tree.

Importing this module is side-effect free.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
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
    """Raised by :func:`reject` and :func:`fail` after printing; carries the exit status."""

    def __init__(self, reason: str, status: int) -> None:
        super().__init__(status)
        self.reason = reason


def _entry(code: str) -> Any:
    entry = ERRORS.get(code)
    if entry is None:
        raise ValueError(f"unregistered error code {code!r}")
    return entry


def reject(
    reason: str,
    message: str | None = None,
    *,
    exit_code: int = EXIT_REJECTED,
    hints: Sequence[str] = (),
    **fields: Any,
) -> NoReturn:
    """Print the rejection for ``reason`` and exit with ``exit_code``.

    stdout gets ``{"state": "rejected", "reason": reason, "message": message,
    **fields}``; stderr gets ``message``, the code, the registry's fix and any
    ``hints``. ``reason`` must be registered in :data:`piceli.errors.ERRORS`;
    an unregistered code is a programming error and raises ``ValueError``.
    ``message`` defaults to the entry's title.
    """
    _report("rejected", reason, message, exit_code, hints, fields)


def fail(
    reason: str,
    message: str | None = None,
    *,
    hints: Sequence[str] = (),
    **fields: Any,
) -> NoReturn:
    """Like :func:`reject` for an operation that ran but did not succeed (exit 1)."""
    _report("failed", reason, message, EXIT_FAILED, hints, fields)


def _report(
    state: str,
    reason: str,
    message: str | None,
    exit_code: int,
    hints: Sequence[str],
    fields: Mapping[str, Any],
) -> NoReturn:
    entry = _entry(reason)
    text = message or entry.title
    emit_json({**fields, "state": state, "reason": reason, "message": text})
    say(f"{state}: {text} [{reason}]")
    for hint in hints:
        say(f"  {hint}")
    say(f"  next: {entry.fix}")
    raise Rejected(reason, exit_code)


#: Set to ``1`` to also print the traceback of an exception raised by the
#: user's module (``MODULE:ATTR``) on stderr; stdout keeps the rejection.
DEBUG_ENV = "PICELI_DEBUG"


def describe_user_error(error: BaseException) -> str:
    """``Type: message`` of an exception raised by the user's model module.

    Used as the rejection's ``message`` when importing or evaluating a
    ``MODULE:ATTR`` target raised: never a traceback on stdout. With
    ``PICELI_DEBUG=1`` the traceback is printed on stderr.
    """
    if os.environ.get(DEBUG_ENV, "") not in {"", "0"}:
        traceback.print_exception(error, file=sys.stderr)
    text = str(error).strip()
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


def error_code(error: BaseException, default: str) -> str:
    """The registered code an exception carries (``code`` or ``category``), else ``default``."""
    for attribute in ("code", "category"):
        value = getattr(error, attribute, None)
        if isinstance(value, str) and value in ERRORS:
            return value
    _entry(default)
    return default


def reject_error(
    error: BaseException,
    default: str,
    *,
    exit_code: int = EXIT_REJECTED,
    **fields: Any,
) -> NoReturn:
    """Reject with the error's own registered code (else ``default``) and its text."""
    reject(
        error_code(error, default),
        str(error) or type(error).__name__,
        exit_code=exit_code,
        **fields,
    )


@contextmanager
def rejecting(
    *rules: tuple[type[BaseException] | tuple[type[BaseException], ...], str],
) -> Iterator[None]:
    """Turn the listed exceptions into rejections; the first matching rule wins.

    Each rule is ``(exception types, default code)``; an exception that carries
    a registered ``code``/``category`` keeps it. Anything else propagates.
    """
    try:
        yield
    except Rejected:
        raise
    except Exception as error:
        for types, default in rules:
            if isinstance(error, types):
                reject_error(error, default)
        raise


# ---------------------------------------------------------------- metadata


@dataclass(frozen=True)
class CommandContract:
    """What one command touches, and how an agent may run it.

    :param summary: One line; used when the command definition has no help.
    :param reads: Local inputs it reads (files, tools, daemons).
    :param writes: Local state it writes; empty for read-only commands.
    :param cluster: ``"none"``, ``"reads"`` or ``"writes"`` (always via an
        explicit kubeconfig).
    :param approval_required: The command changes a cluster, registry or node,
        or runs code, only with an explicit approval (hash/digest/flag).
    :param safe_to_retry: Re-running it after an interruption cannot cause
        harm (it is read-only, idempotent or resumable).
    :param contract: ``"conforms"`` (this module's convention) or ``"partial"``
        (JSON on stdout, but refusals differ).
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
                "read_only": not self.writes and self.cluster in {"none", "reads"},
            },
            "approval_required": self.approval_required,
            "safe_to_retry": self.safe_to_retry,
            "long_running": self.long_running,
            "contract": self.contract,
            "exit_codes": {str(code): EXIT_CODES[code] for code in self.exit_codes},
            **({"notes": self.notes} if self.notes else {}),
        }


_C = CommandContract
_EXPLICIT_CONTEXT = (
    "`--context` is required (current-context is never used, also not by the "
    "kubectl processes it starts); exec credential plugins need `--allow-exec` "
    "(optionally `--exec-sha256`)."
)
_RELEASE_READS = (
    "release.toml or pipeline module (--spec MODULE:ATTR)",
    "composition",
    "state_dir",
    "kubeconfig",
)
_RELEASE_EXIT = (0, 1, 2, 3)
_ENV_NOTE = (
    " With a pipeline, --env NAME selects one environment (its target, "
    "overrides and state)."
)

COMMANDS: Mapping[str, CommandContract] = MappingProxyType(
    {
        # ----------------------------------------------------- contract
        "render": _C(
            "Print the manifests of a typed app, composition or pipeline (YAML or JSON).",
            reads=("module/app file", "release.toml (optional)", "local receipts"),
            writes=("--out directory",),
            contract="conforms",
            notes="Never contacts a cluster; secret values are placeholders. "
            "--out DIR writes one YAML file per object (a directory for Argo CD "
            "or Flux from Git; a Secret needs --secrets external, redacted "
            "values and placeholder images are refused) and prints one JSON "
            "object; DIR must be absent, empty or a previous --out. A "
            "Pipeline renders with its target's namespace and declared nodes, "
            "build images as placeholders, and reads no kubeconfig, build spec "
            "or state. --env NAME renders one environment of the App (a "
            "pipeline's target for it); --diff-env OTHER prints the typed "
            "difference between the two environments instead (JSON with "
            "--format json). stdout carries the manifests (YAML, or one JSON "
            "object with --format json); a refusal is always the JSON rejection "
            "object.",
        ),
        "publish": _C(
            "Push the rendered manifests as a Flux OCI artifact (approval by digest).",
            reads=(
                "module/app file",
                "release.toml (optional)",
                "local receipts",
                "credentials file",
            ),
            writes=("OCI registry (artifact blobs, manifest, tag)",),
            approval_required=True,
            safe_to_retry=True,
            contract="conforms",
            exit_codes=(0, 1, 2, 3),
            notes="Never contacts a cluster. Without --approve it prints the "
            "deterministic artifact digest and exits 3 (nothing is pushed); "
            "--approve DIGEST pushes by digest, then the tag, and reads both "
            "back. Layout of `flux push artifact` (config "
            "application/vnd.cncf.flux.config.v1+json, one layer "
            "application/vnd.cncf.flux.content.v1.tar+gzip). A Secret needs "
            "--secrets external; redacted values and placeholder images are "
            "refused. Credentials only from --credentials FILE, never printed.",
        ),
        # ----------------------------------------------------- profiles
        "login": _C(
            "Store a credential profile (a reference to a kubeconfig file and context).",
            reads=("kubeconfig (only to check the context exists)",),
            writes=("$PICELI_PROFILES_DIR/NAME.json (mode 0600)",),
            contract="conforms",
            notes="Never contacts a cluster. Stores the absolute kubeconfig path and "
            "the context, never the kubeconfig contents or a token; the context is "
            "required unless the file defines exactly one (current-context is never "
            "used). Idempotent: logging in again replaces the profile.",
        ),
        "profiles": _C(
            "List the stored credential profiles (names, contexts, kubeconfig paths).",
            reads=("$PICELI_PROFILES_DIR",),
            contract="conforms",
            notes="Never contacts a cluster; prints references only, never secrets. "
            "--json prints {state, profiles: [{name, kubeconfig, context, "
            "kubeconfig_present}]}.",
        ),
        "logout": _C(
            "Delete a stored credential profile (never the kubeconfig file).",
            writes=("$PICELI_PROFILES_DIR/NAME.json (removed)",),
            contract="conforms",
            notes="Never contacts a cluster. Prints {state: removed|absent, name}; "
            "removing an absent profile is not an error.",
        ),
        # -------------------------------------------------------- chart
        "chart render": _C(
            "Write the app as a Helm chart directory (Chart.yaml, values, schema, templates).",
            reads=("module/app file", "release.toml (optional)", "local receipts"),
            writes=("--out chart directory",),
            contract="conforms",
            notes="Never contacts a cluster. Renders like `piceli render`; "
            "images, pull secrets, replicas, resources, node selectors, storage, "
            "hosts, ConfigMap keys and Secret names become values, documented "
            "and validated by values.schema.json. Secrets are never rendered: "
            "the chart reads existing Secrets by name. Objects holding redacted "
            "or apply-time secret values are refused. Deterministic. --out must "
            "be absent, empty or a previous chart render (.piceli-chart).",
        ),
        "chart package": _C(
            "Write the chart archive <name>-<version>.tgz (byte-identical per chart).",
            reads=("module/app file", "release.toml (optional)", "local receipts"),
            writes=("--out directory (<name>-<version>.tgz)",),
            contract="conforms",
            notes="Never contacts a cluster. The same render, name and version "
            "always give the same bytes and digest; an existing archive with "
            "other content is refused (bump --version).",
        ),
        "chart manifests": _C(
            "Print plain manifests with a values file applied (no Helm, no Kustomize).",
            reads=(
                "module/app file",
                "release.toml (optional)",
                "local receipts",
                "values files",
            ),
            writes=("--out directory",),
            contract="conforms",
            notes="Never contacts a cluster. Values merge over the chart "
            "defaults like Helm (null removes a key) and are validated against "
            "the chart schema (chart-values-invalid). Prints YAML, or with --out "
            "writes one file per object like render --out and prints one JSON "
            "object. Secrets are left out (provided by the client).",
        ),
        "chart publish": _C(
            "Push the chart to an OCI registry as helm push does (approval by digest).",
            reads=(
                "module/app file",
                "release.toml (optional)",
                "local receipts",
                "credentials file",
            ),
            writes=("OCI registry (chart blobs, manifest, version tag)",),
            approval_required=True,
            safe_to_retry=True,
            contract="conforms",
            exit_codes=(0, 1, 2, 3),
            notes="Never contacts a cluster. --to oci://host[:port]/path pushes "
            "path/<name>:<version> (config application/vnd.cncf.helm.config.v1+json, "
            "layer application/vnd.cncf.helm.chart.content.v1.tar+gzip). Without "
            "--approve it prints the deterministic digest and exits 3; --approve "
            "DIGEST pushes by digest, then the tag, and reads both back. "
            "Credentials only from --credentials FILE, never printed.",
        ),
        # ------------------------------------------------------ codegen
        "codegen crd": _C(
            "Generate pydantic models from a CustomResourceDefinition's schema.",
            reads=("CRD file", "kubeconfig (with --from-cluster)"),
            writes=("--out file",),
            cluster="reads",
            contract="conforms",
            notes=(
                "A file never contacts a cluster; --from-cluster sends one GET "
                "of the CRD through an explicit --kubeconfig and --context "
                "(exec plugins only with --allow-exec). Deterministic: the same "
                "schema always generates the same module. --out replaces only a "
                "file piceli generated, unless --force."
            ),
        ),
        # ------------------------------------------------------- import
        "import live": _C(
            "Generate a typed app module from the objects of a live namespace.",
            reads=("kubeconfig",),
            writes=("--out file",),
            cluster="reads",
            contract="conforms",
            notes=(
                "Read-only on the cluster: lists ConfigMaps, Secrets, Services, "
                "PersistentVolumeClaims, Deployments, NetworkPolicies and Pods. "
                "Secret values are never written; --out refuses to overwrite "
                "without --force."
            ),
        ),
        "import yaml": _C(
            "Generate a typed app module from a directory of manifests.",
            reads=("manifest directory",),
            writes=("--out file",),
            contract="conforms",
            notes=(
                "Never contacts a cluster. Secret values are never written; --out "
                "refuses to overwrite without --force."
            ),
        ),
        "explain": _C(
            "Print the registry entry for an error code.",
            contract="conforms",
            notes="`--run ID --spec SPEC` explains a past execution instead, from "
            "the local state (same as `piceli release status --spec SPEC --run "
            "ID`).",
        ),
        "help-json": _C(
            "Print the whole CLI tree, with contracts, as JSON.",
            contract="conforms",
            exit_codes=(0,),
        ),
        # ---------------------------------------------- access / status
        "access": _C(
            "Supervise the app's declared port forwards until interrupted.",
            reads=("release.toml or module:attr", "kubeconfig", "kubectl"),
            writes=("loopback ports (kubectl port-forward processes it owns)",),
            cluster="reads",
            long_running=True,
            contract="conforms",
            exit_codes=(0, 1, 2),
            notes="Refuses (access-port-conflict) when a declared local port is "
            "held by another process and names its pid (another process's command "
            "line is never printed); when the holder is Piceli's own stale process "
            "for this app it says so and suggests `piceli access stop --stale "
            "TARGET`. Never takes a port over. Stops every forward it started on "
            "Ctrl-C/SIGTERM/SIGHUP. Exit 1 only when every forward gave up. "
            "`piceli access BRANCH --pipeline MODULE:ATTR` forwards that branch "
            "environment's declared ports on free local ports.",
        ),
        "access ui": _C(
            "Forward the UI that `piceli cluster init` installed and print its launch URL.",
            reads=(
                "--cluster MODULE:ATTR or --profile NAME",
                "kubeconfig",
                "the UI's Service and launch Secret",
                "kubectl",
            ),
            writes=("loopback port 8790 (a kubectl port-forward it owns)",),
            cluster="reads",
            long_running=True,
            contract="conforms",
            exit_codes=(0, 1, 2),
            notes="Read-only on the cluster. Prints the one URL with the launch "
            "token (stdout, or the `started` JSON line with --json); the token is "
            "never logged elsewhere. Refuses (access-port-conflict) when "
            "127.0.0.1:8790 is held. Stops the forward on Ctrl-C/SIGTERM/SIGHUP; "
            "exit 1 only when the forward gave up.",
        ),
        "access stop": _C(
            "Stop Piceli's own stale forwards and servers for the app (--stale).",
            reads=("release.toml or module:attr", "local process table"),
            writes=("signals Piceli's own processes for this app (SIGTERM)",),
            contract="conforms",
            exit_codes=(0, 1, 2),
            notes="Only with --stale. Checks the app's declared forward ports and "
            "each --port; stops a port's holder only when it is Piceli's own "
            "process for this target (its kubectl port-forward, orphaned or "
            "supervised, or piceli access / observe serve / operator serve with "
            "the same target), never another process, which is reported by pid "
            "only. Local only: never contacts the cluster.",
        ),
        "status": _C(
            "Say whether the app is up and how to reach it (release, images, "
            "health, URLs).",
            reads=("release.toml or module:attr", "state_dir", "kubeconfig"),
            cluster="reads",
            contract="conforms",
            exit_codes=(0, 1, 2),
            notes="Read-only. Exit 0 when every workload is ready, 1 otherwise "
            "(including an unreadable cluster). Probes forwards on 127.0.0.1 only. "
            "JSON schema: docs/schemas/piceli-status-v1.schema.json.",
        ),
        # ------------------------------------------------------ release
        "release plan": _C(
            "Capture live discovery and persist an approvable plan.",
            contract="conforms",
            reads=_RELEASE_READS,
            writes=("state_dir (pending plan, secret candidates)", "--out file"),
            cluster="reads",
            notes=(
                "Never changes the cluster: reads plus dryRun=All requests "
                "(server dry runs of the writes). Prints the plan hash to approve."
                + _ENV_NOTE
            ),
        ),
        "release preview": _C(
            "Alias of `release plan`.",
            contract="conforms",
            reads=_RELEASE_READS,
            writes=("state_dir (pending plan, secret candidates)", "--out file"),
            cluster="reads",
        ),
        "release diff": _C(
            "Show what `release plan` would change, field by field.",
            contract="conforms",
            reads=_RELEASE_READS,
            cluster="reads",
            exit_codes=(0, 1, 2),
            notes=(
                "Read-only: stores no plan and no local state. Sends only reads "
                "and dryRun=All requests (server dry runs of the writes). "
                "Exit 1 with --exit-code when something would change (reason "
                "release-changes-pending)."
            ),
        ),
        "release apply": _C(
            "Execute an approved plan (--approve HASH).",
            contract="conforms",
            reads=_RELEASE_READS,
            writes=("state_dir (catalog, journal, secret store, check reports)",),
            cluster="writes",
            approval_required=True,
            safe_to_retry=False,
            exit_codes=_RELEASE_EXIT,
            notes="After an interruption use `release resume`, not apply. Runs "
            "[[checks]] after readiness (exit 1 with release_state "
            "checks-failed); with rollback_on_failed_checks it re-applies the "
            "previous ready release without a further approval. --skip-checks "
            "is recorded. --approve-if-policy plans and applies only when "
            "every action is inside the spec's [release] auto_approve policy "
            "(declared by the owner, part of the plan hash); otherwise exit 3 "
            "with reason approval-policy-exceeded and the hash to approve." + _ENV_NOTE,
        ),
        "release rollback": _C(
            "Re-plan and re-apply an earlier release.",
            contract="conforms",
            reads=_RELEASE_READS,
            writes=("state_dir (catalog, journal, check reports)",),
            cluster="writes",
            approval_required=True,
            safe_to_retry=False,
            exit_codes=_RELEASE_EXIT,
            notes="Runs [[checks]] after readiness, as apply does.",
        ),
        "release check": _C(
            "Run the spec's [[checks]] now against a release.",
            contract="conforms",
            reads=_RELEASE_READS,
            cluster="reads",
            exit_codes=(0, 1, 2),
            notes="Writes no state and never rolls back. Checks open temporary "
            "loopback port forwards, may exec declared commands in pods and "
            "run declared Python check functions. Exit 1 when a check failed "
            "(state failed, reason check-failed).",
        ),
        "release resume": _C(
            "Resume an interrupted apply with the same grant and ids.",
            contract="conforms",
            reads=_RELEASE_READS,
            writes=("state_dir (journal, check reports)",),
            cluster="writes",
            approval_required=False,
            safe_to_retry=True,
            exit_codes=(0, 1, 2),
            notes="Continues an already approved execution; needs no new approval. "
            "Runs [[checks]] when it becomes ready, as apply does.",
        ),
        "release stop": _C(
            "Cancel the latest execution of a release.",
            contract="conforms",
            reads=_RELEASE_READS,
            writes=("state_dir (journal)",),
            cluster="reads",
            safe_to_retry=True,
            notes="Checks the cluster identity; records the cancellation locally.",
        ),
        "release secret show": _C(
            "Show a generated secret's metadata, or its value with --reveal.",
            contract="conforms",
            reads=(
                "release.toml or pipeline module (--spec MODULE:ATTR)",
                "state_dir (secret store)",
            ),
            notes="Owner only. Never contacts the cluster; values print only with --reveal or a terminal confirmation.",
        ),
        "release status": _C(
            "Show catalogued releases, executions and history.",
            contract="conforms",
            reads=("release.toml or pipeline module (--spec MODULE:ATTR)", "state_dir"),
            notes='With state = "cluster" it first refreshes the working copy '
            "from the cluster (reads only, no lock). `--run ID` shows one past "
            "execution (an execution id or unique prefix, or a `piceli deploy` "
            "run id) with the causes recorded when it failed (pod reasons, exit "
            "codes, redacted log tails, events).",
        ),
        "release orphans": _C(
            "List the objects with the app's ownership labels that no current "
            "release owns; --prune deletes them after approval.",
            contract="conforms",
            reads=_RELEASE_READS,
            cluster="writes",
            approval_required=True,
            exit_codes=_RELEASE_EXIT,
            notes="Read-only without --prune. --prune prints the plan hash over "
            "the exact set (UIDs and resourceVersions) and exits 3; --prune "
            "--approve HASH deletes that set, or refuses when it changed "
            "(orphans-plan-changed); each deleted object is waited for until it "
            "is gone (outcome deleting when still terminating). Never prunes claims, Secrets, objects of "
            "another owner or cluster-scoped objects unless --include-claims, "
            "--include-secrets, --include-other-owners or "
            "--include-cluster-scoped; never an object without Piceli's owner "
            "annotation." + _ENV_NOTE,
        ),
        # -------------------------------------------------------- state
        "state show": _C(
            "Show where a release's state lives, its generation and the lock holder.",
            contract="conforms",
            reads=("release.toml or pipeline module (--spec MODULE:ATTR)", "state_dir"),
            cluster="reads",
            notes="Read-only; never prints state content. With local state it "
            "does not contact the cluster.",
        ),
        "state pull": _C(
            "Refresh the local working copy of a release's shared state.",
            contract="conforms",
            reads=("release.toml or pipeline module (--spec MODULE:ATTR)",),
            writes=("state_dir (replaced by the shared snapshot)",),
            cluster="reads",
            notes="Takes no release lock; skipped while a run on this machine "
            "holds the state directory. A no-op with local state.",
        ),
        "state export": _C(
            "Write a release's state to one file; secret material only encrypted.",
            contract="conforms",
            reads=(
                "release.toml or pipeline module (--spec MODULE:ATTR)",
                "state_dir",
                "--key-file",
            ),
            writes=("--out file",),
            cluster="reads",
            notes="Secret store, stored discovery, journals, pending plans and "
            "backups are left out unless --include-secrets --key-file (AES-256-GCM, "
            "scrypt key); refuses an existing --out without --force.",
        ),
        "state import": _C(
            "Replace a release's state with an export (approval by digest).",
            contract="conforms",
            reads=(
                "release.toml or pipeline module (--spec MODULE:ATTR)",
                "--in file",
                "--key-file",
            ),
            writes=("state_dir", 'shared state Secrets and Lease (state = "cluster")'),
            cluster="writes",
            approval_required=True,
            safe_to_retry=True,
            exit_codes=(0, 2, 3),
            notes="Without --approve it prints the import digest and exits 3; "
            "with it, it holds the release lock and replaces the state. An "
            "export without secret material needs --allow-partial.",
        ),
        # ------------------------------------------------------- inputs
        "inputs record": _C(
            "Capture the git identity of declared sources.",
            contract="conforms",
            reads=("inputs.toml", "git work trees"),
            writes=("--out lock file",),
            exit_codes=(0, 2),
        ),
        "inputs verify": _C(
            "Compare sources with a lock; exit 1 on drift.",
            contract="conforms",
            reads=("inputs.toml", "lock", "git work trees"),
            exit_codes=(0, 1, 2),
        ),
        # ---------------------------------------------------- artifacts
        "artifacts preview": _C(
            "Preview a deterministic OCI build plan (no tools run).",
            contract="conforms",
            reads=("plan JSON",),
        ),
        "artifacts build": _C(
            "Assemble an OCI image layout from a plan without running code.",
            contract="conforms",
            reads=("plan JSON", "source root"),
            writes=("--output layout",),
        ),
        "artifacts pin": _C(
            "Pin one public source file by digest.",
            contract="conforms",
            reads=("source file",),
        ),
        "artifacts inspect": _C(
            "Recheck an OCI layout and print its summary.",
            contract="conforms",
            reads=("OCI layout",),
        ),
        "artifacts import-local": _C(
            "Import an OCI layout into the local Docker daemon.",
            contract="conforms",
            reads=("OCI layout", "docker"),
            writes=("local Docker image store",),
            approval_required=True,
            exit_codes=(0, 1, 2),
        ),
        "artifacts preview-command": _C(
            "Preview a pinned external build command.",
            contract="conforms",
            reads=("command spec",),
        ),
        "artifacts execute-command": _C(
            "Run a pinned external build command under an approved plan.",
            contract="conforms",
            reads=("command spec", "source root", "pinned tool"),
            writes=("tool outputs",),
            approval_required=True,
            safe_to_retry=False,
            exit_codes=(0, 1, 2),
        ),
        "artifacts deliver": _C(
            "Deliver a digest-approved image to a registry or node.",
            contract="conforms",
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
            contract="conforms",
            reads=("build.toml", "inputs.toml", "build contexts"),
        ),
        "artifacts build-spec run": _C(
            "Run an approved containerized build and write a receipt.",
            contract="conforms",
            reads=("build.toml", "inputs", "build contexts", "docker"),
            writes=("--out receipt", "--output-dir", "local Docker image store"),
            approval_required=True,
            safe_to_retry=True,
            long_running=True,
            exit_codes=(0, 1, 2),
        ),
        "artifacts publish": _C(
            "Publish a build's images (every platform in one index) to a registry.",
            contract="conforms",
            reads=(
                "build receipt and its outputs",
                "credentials file or Docker config",
                "docker (Docker builds only)",
            ),
            writes=(
                "OCI registry (blobs, manifests, index, attestations, tag)",
                "--out receipt",
                "--values-out",
            ),
            approval_required=True,
            safe_to_retry=True,
            long_running=True,
            exit_codes=(0, 1, 2, 3),
            notes="Without --approve it prints the plan and its digest (exit 3) "
            "and contacts nothing. Pushes are content-addressed; a version tag "
            "that names another image is refused unless --move-tag.",
        ),
        "artifacts retention": _C(
            "Report which registry manifests the last releases, pins and live workloads keep, and delete the rest.",
            contract="conforms",
            reads=(
                "publish and delivery receipts",
                "credentials file or Docker config",
                "kubeconfig (live pods, workload templates, environment records, with --context)",
                "--cluster MODULE:ATTR (Cluster, composition, pipeline or Registry.in_cluster)",
                "credential profile (with --cluster)",
                "in-cluster registry storage (read-only find over pods/exec, with --cluster)",
                "kubectl (port-forward, with --cluster or --via-forward)",
            ),
            writes=(
                "OCI registry (manifest deletes, only with --delete --approve)",
                "--out",
            ),
            cluster="reads",
            approval_required=True,
            safe_to_retry=True,
            exit_codes=(0, 1, 2, 3),
            notes="Without --delete it only reads. --delete without --approve "
            "prints the plan and its hash (exit 3). A digest a running workload "
            "uses is never deleted, nor a workload's rollback target; --keep 0 "
            "needs --kubeconfig and --context. --cluster REF derives the "
            "registry, forward and cluster read, defaults --keep to 0 and "
            "lists every stored manifest (digest-only ones too). Blobs are "
            "freed by the registry's own garbage collection afterwards.",
        ),
        # ------------------------------------------------------ observe
        "observe status": _C(
            "Compare a session archive with live cluster state.",
            contract="conforms",
            reads=("session archive", "kubeconfig"),
            cluster="reads",
            notes=_EXPLICIT_CONTEXT,
        ),
        "observe forward-save": _C(
            "Save one port-forward preference.",
            contract="conforms",
            reads=("preferences file", "kubeconfig (with --context, for the scope)"),
            writes=("preferences file",),
        ),
        "observe forward-list": _C(
            "List saved port-forward preferences.",
            contract="conforms",
            reads=("preferences file",),
        ),
        "observe forward-command": _C(
            "Print the kubectl argv for one saved forward.",
            contract="conforms",
            reads=("preferences file",),
            notes="`--context` is required; the argv always carries it.",
        ),
        "observe forward-run": _C(
            "Run one saved port forward until interrupted.",
            contract="conforms",
            reads=("preferences file", "kubeconfig"),
            cluster="reads",
            long_running=True,
            notes=_EXPLICIT_CONTEXT
            + " After the checks, output and exit status are kubectl's own.",
        ),
        "observe logs-command": _C(
            "Print the kubectl argv for a bounded log request.",
            contract="conforms",
            notes="`--context` is required; the argv always carries it.",
        ),
        "observe logs-run": _C(
            "Run a bounded log request in the foreground.",
            contract="conforms",
            reads=("kubeconfig",),
            cluster="reads",
            long_running=True,
            notes=_EXPLICIT_CONTEXT
            + " After the checks, output and exit status are kubectl's own.",
        ),
        "observe serve": _C(
            "Serve the local operations UI and JSON API on loopback.",
            contract="conforms",
            reads=("session archive", "kubeconfig", "preferences file"),
            writes=("preferences file",),
            cluster="reads",
            notes=_EXPLICIT_CONTEXT
            + " Saved forwards start only with --restore-forwards, and only"
            " those saved for this cluster, context and namespace.",
            long_running=True,
        ),
        "observe forwards apply": _C(
            "Start and supervise the forwards of an access profile.",
            contract="conforms",
            reads=("access profile", "kubeconfig"),
            cluster="reads",
            notes=_EXPLICIT_CONTEXT,
            long_running=True,
            exit_codes=(0, 2),
        ),
        "observe forwards status": _C(
            "Report the health of the forwards of an access profile.",
            contract="conforms",
            reads=("access profile",),
            exit_codes=(0, 1, 2),
        ),
        # ----------------------------------------------------- operator
        "operator status": _C(
            "Print classified operator inventory.",
            contract="conforms",
            reads=("kubeconfig", "archive", "catalog"),
            cluster="reads",
            notes=_EXPLICIT_CONTEXT,
        ),
        "operator promote": _C(
            "Promote a built digest to a new catalog entry.",
            contract="conforms",
            reads=("catalog",),
            writes=("catalog",),
            safe_to_retry=False,
        ),
        "operator approve": _C(
            "Record an operator approval for a pull request rollout.",
            contract="conforms",
            writes=("state_dir",),
            approval_required=True,
        ),
        "operator backup": _C(
            "Create a verified backup archive of operator state.",
            contract="conforms",
            reads=("state_dir",),
            writes=("backup archive",),
        ),
        "operator restore": _C(
            "Restore operator state into an empty directory.",
            contract="conforms",
            reads=("backup archive",),
            writes=("destination directory",),
            safe_to_retry=False,
        ),
        "ui cluster-observe": _C(
            "Serve authenticated cluster observation with optional reviewed delivery and local-client access.",
            contract="conforms",
            reads=(
                "explicit Kubernetes API origin",
                "projected service-account CA and token files",
                "OIDC issuer metadata",
            ),
            writes=(
                "private generated kubeconfig",
                "configured durable UI control state",
                "separately approved cluster deployment",
            ),
            cluster="writes",
            approval_required=True,
            notes="Requires piceli[ui] and a separate HTTPS TLS gateway sidecar. Default subjects inspect one namespace and logs. Configured deploy grants enable exact-plan reviewed manual delivery and credential-free cluster builds through scoped Jobs. Access grants enable local-client tickets. The server never binds a laptop port or reads an ambient Kubernetes context.",
            long_running=True,
        ),
        "ui cluster-serve": _C(
            "Serve authenticated cluster observation with optional reviewed delivery and local-client access.",
            contract="conforms",
            reads=(
                "explicit Kubernetes API origin",
                "projected service-account CA and token files",
                "OIDC issuer metadata",
                "optional mounted release definition and pinned renderer",
            ),
            writes=(
                "private generated kubeconfig",
                "configured durable UI control state",
                "separately approved cluster deployment",
            ),
            cluster="writes",
            approval_required=True,
            notes="Requires piceli[ui] and a separate HTTPS TLS gateway sidecar. Observation is the default. Configured deploy grants permit exact-plan manual delivery and cluster build Jobs; access grants permit local-client tickets. No ambient Kubernetes context or server-side laptop port.",
            long_running=True,
        ),
        "ui forward-serve": _C(
            "Serve the composition UI in its pod, reached only through `piceli access ui`.",
            contract="conforms",
            reads=(
                "the pod's projected service-account CA and token",
                "the controller's status ConfigMap",
                "environments' workloads, pods and logs",
            ),
            writes=(
                "the UI's launch Secret (its launch token)",
                "sync requests in the controller's request ConfigMap",
            ),
            cluster="writes",
            long_running=True,
            notes='Runs in the pod `piceli cluster init` installs (Ui(access="forward")). Listens on the pod\'s loopback only, without OIDC; a fresh launch token goes into the Secret piceli-ui-launch and is never printed. The Sync button writes the same request as `piceli gitops sync`; nothing else is written.',
        ),
        "ui connect": _C(
            "Bind and supervise one local loopback forward for a scoped cluster UI ticket.",
            contract="conforms",
            reads=(
                "explicit local kubeconfig and context",
                "one-time UI pairing secret",
                "scoped target and resource identity",
            ),
            writes=(
                "owned local loopback port-forward process",
                "private local forward ownership record",
            ),
            cluster="reads",
            notes="Requires piceli[ui], a trusted HTTPS cluster UI and local kubectl. The pairing secret is prompted without echo; kubeconfig and context must be explicit. The port exists on the client host only while the supervised command runs.",
            long_running=True,
        ),
        "ui serve": _C(
            "Serve the packaged web application for an explicit target.",
            contract="conforms",
            reads=(
                "kubeconfig",
                "release or Pipeline definition",
                "release state",
                "approved source files",
                "pinned Docker renderer",
            ),
            writes=("configured UI control directory", "approved release state"),
            cluster="writes",
            approval_required=True,
            notes="Requires piceli[ui]. Supply --pipeline for a trusted Pipeline, --definition for a release definition, or explicit --kubeconfig, --context and --namespace for inventory. Loopback only; no ambient target. Pipeline builds require a second exact approval after delivery. Starting the service approves no deployment.",
            long_running=True,
        ),
        "ui backup": _C(
            "Back up stopped UI control state and release evidence.",
            contract="conforms",
            reads=("explicit offline UI control directory",),
            writes=("new private verified backup archive",),
            notes="Refuses an active dispatcher; archive includes private evidence and must be protected.",
        ),
        "ui restore": _C(
            "Restore a verified UI state archive into an empty directory.",
            contract="conforms",
            reads=("explicit UI backup archive",),
            writes=("new private UI control directory",),
            notes="Stop the server first and restore before starting a new single replica.",
        ),
        "operator serve": _C(
            "Serve the operator dashboard and REST API on loopback.",
            contract="conforms",
            reads=("kubeconfig", "catalog", "archive", "preferences file"),
            writes=("preferences file", "state_dir"),
            cluster="reads",
            notes=_EXPLICIT_CONTEXT
            + " --access starts the model's declared forwards; saved forwards"
            " start only with --restore-forwards, and only those saved for this"
            " cluster, context and namespace.",
            long_running=True,
        ),
        # ------------------------------------------------ P5 pipeline
        "deploy": _C(
            "Deploy a pipeline from source: inputs, build, deliver, plan, apply, checks.",
            reads=(
                "pipeline module",
                "build specs and sources",
                "git (source identity; --ref commits)",
                "docker",
                "kubeconfig",
                "state_dir",
                "source registries of mirror= images (pull only)",
                "mirror_credentials files",
            ),
            writes=(
                "state_dir (run journal, receipts, release catalog, secret store)",
                'shared state Secrets and release Lease (state="cluster")',
                "--out plan file",
                "local Docker image store",
                "registry or node image store",
                "temporary git worktrees with --ref (removed on exit)",
            ),
            cluster="writes",
            approval_required=True,
            safe_to_retry=True,
            long_running=True,
            contract="conforms",
            exit_codes=(0, 1, 2, 3),
            notes="--plan never changes the cluster, a registry or a node (it "
            "reads the namespace's Deployments and the registry node for a "
            "NodeLoopbackRegistry); before the images exist it previews the "
            "release with placeholder images (never approvable, never sent to "
            "the cluster) and refuses with the blocking objects when it needs "
            "adoption or replacement; --approve HASH executes exactly the "
            "combined plan, including mirror= copies and a registry "
            "adopt=/replace=, and a release planned after delivery may not "
            "adopt, replace or delete more than the approved preview; --resume "
            "continues the latest interrupted run without a new approval. "
            "Unchanged stages are skipped. --ref [SOURCE=]REV builds the "
            "sources from commits in temporary worktrees; the combined hash "
            "covers the resolved SHAs, --approve needs the same --ref, and "
            "--resume reuses the run's SHAs. --plan --out FILE writes a "
            "portable plan; --apply FILE --approve HASH applies it on any "
            "runner (it re-plans and refuses any difference; no build cache "
            'needed for images already delivered). With state="cluster" '
            "every command holds the release's Lease (pipeline-locked when "
            "another runner holds it; a stale lease is taken over) and "
            "--plan writes its state to the cluster too. --env NAME deploys one "
            "environment (the app's overrides and the pipeline's target for it, "
            "state under <state_dir>/environments/NAME); the combined hash "
            "covers the environment's name and resolved values, so --approve, "
            "--resume and --plan --out/--apply need the same --env. "
            "--approve-if-policy plans and executes without a hash only when "
            "every action is inside the pipeline's auto_approve policy "
            "(declared by the owner, part of the combined hash; never delete, "
            "replace or adopt); otherwise exit 3 with reason "
            "approval-policy-exceeded and the approval command. "
            "Every run "
            "that starts executing writes <state_dir>/runs/<run id>/summary.json "
            "(schema docs/schemas/piceli-run-summary-v1.schema.json) and "
            "summary.md; the result names them (summary). With the pipeline's "
            "cache_budget the state directory is pruned after the run (result: "
            "cache).",
        ),
        # ------------------------------------------- 0.8.0 maintenance
        "build job": _C(
            "Build the pipeline's host builds at a Git commit as a Job on a "
            "labelled builder node and print the receipt: plan (exit 3 with the "
            "plan hash), then --approve HASH.",
            reads=("pipeline module", "kubeconfig", "node facts"),
            writes=("node facts cache (state_dir)",),
            cluster="writes",
            approval_required=True,
            safe_to_retry=True,
            long_running=True,
            contract="conforms",
            exit_codes=(0, 1, 2, 3),
            notes="Without --approve it reads the node facts and prints the Job "
            "plan only. With --approve HASH it creates the cache claim (per "
            "branch and page size) if missing and one Job that fetches the "
            "commit (Git credentials from a Secret, never printed), builds for "
            "each --platform, pushes by digest to the node registry and prints "
            "the receipt; the Job is removed afterwards and the cache kept. "
            "Rejected: cluster-build-invalid; failed: cluster-build-failed.",
        ),
        "build job-run": _C(
            "Inside the build Job: build the host specs of a checked-out "
            "source and push the images by digest (not for manual use).",
            reads=("the checked-out source", "PICELI_BUILD_FACTS"),
            writes=("the build cache directory", "the node registry"),
            cluster="none",
            safe_to_retry=True,
            long_running=True,
            contract="conforms",
            exit_codes=(0, 1, 2),
            notes="The command the Job runs; it prints one receipt line on "
            "stdout. The approval was the plan hash of `build job`. With "
            "--sources and --component it builds a composition's components "
            "from their piceli.toml (the Job the composition controller runs).",
        ),
        "env push": _C(
            "Record a laptop-built image digest for a branch environment, "
            "where `env up` and the controller read it: plan (exit 3 with the "
            "hash), then --approve HASH.",
            reads=("pipeline module", "kubeconfig", "the receipt file"),
            cluster="writes",
            approval_required=True,
            safe_to_retry=True,
            contract="conforms",
            exit_codes=(0, 1, 2, 3),
            notes="Writes the ConfigMap piceli-env-<branch> (keys images, "
            "commit, pushed_at) in the branch environment's namespace, creating "
            "the namespace when absent. It pushes no image and deploys nothing.",
        ),
        "cache status": _C(
            "Show the disk Piceli uses per state directory and category, and "
            "its temporary directories.",
            reads=(
                "pipeline module or --state-dir",
                "state_dir",
                "temporary directory",
            ),
            contract="conforms",
            exit_codes=(0, 2),
            notes="Read-only; never contacts a cluster. Categories: builds "
            "(outputs and logs), toolchains, blobs, receipts, runs (journals "
            "and summaries), release (never pruned), other, temp (partial "
            "files). reclaimable_bytes is what cache prune with --keep-last "
            "would free.",
        ),
        "cache prune": _C(
            "Remove stale temporary directories, old runs, unused delivery "
            "receipts and, over --budget, build outputs and logs.",
            reads=(
                "pipeline module or --state-dir",
                "state_dir",
                "temporary directory",
            ),
            writes=(
                "state_dir (old runs, unused delivery receipts, build outputs "
                "and logs, stale partial files)",
                "stale piceli-* temporary directories",
            ),
            contract="conforms",
            exit_codes=(0, 1, 2),
            notes="Never removes the release state (catalog, execution journal, "
            "secret store, approved plans, backups, history), build or mirror "
            "receipts, the latest run, a resumable run or the runs of the last "
            "--keep-last applied releases. Holds each state directory's run "
            "lock (pipeline-locked while a deploy runs). With shared state "
            '(state="cluster") only machine-local files are pruned. --dry-run '
            "removes nothing. Exit 1 (cache-over-budget) when a state directory "
            "is still over the budget. Always prints one JSON object.",
        ),
        "heavy run": _C(
            "Run a command under the machine-wide heavy-work lock and write a receipt.",
            reads=("the given command's working directory", "git (HEAD, status)"),
            writes=(
                "the per-user heavy state directory (lock, holder record, "
                "receipts, bounded)",
            ),
            contract="conforms",
            exit_codes=(0, 1, 2),
            long_running=True,
            safe_to_retry=False,
            notes="Runs exactly the command given after --, without a shell and "
            "with the caller's own authority (like time or flock); Piceli "
            "executes nothing else, so there is no plan or approval. The "
            "command's exit code is the exit code (128+N for signal N), so 2 "
            "may also be the command's own; stdout holds the receipt JSON only "
            "(the child's stdout goes to stderr). Crash-safe OS lock (flock): a "
            "killed holder releases it. Receipts hold the redacted command, "
            "cwd, git commit, times, exit code and peak memory; never the "
            "environment. Rejected: heavy-lock-timeout after --wait.",
        ),
        "heavy status": _C(
            "Show who holds the heavy-work lock and the recent receipts.",
            reads=("the per-user heavy state directory",),
            contract="conforms",
            exit_codes=(0,),
            notes="Read-only; never contacts a cluster.",
        ),
        "gitops enable": _C(
            "Plan and, with --approve HASH, install the GitOps controller that "
            "deploys branches from Git (one environment per branch).",
            reads=("kubeconfig", "the pipeline module under --root (if present)"),
            cluster="writes",
            approval_required=True,
            contract="conforms",
            exit_codes=(0, 2, 3),
            notes="Reads the pipeline's named environments and idle_stop from "
            "the working tree (--root, default .) into the controller config "
            "(gitops-pipeline-invalid when the file is there and does not "
            "load). Without --approve prints the install plan (Namespace, "
            "ServiceAccount, scoped Role/ClusterRole and bindings, state "
            "PersistentVolumeClaim, config ConfigMap, one-replica Deployment) "
            "and its hash, exit 3. --image must be pinned by digest "
            "(gitops-image-unpinned); --repo must carry no credentials "
            "(gitops-repo-invalid): they come from the Secret named by "
            "--credentials-secret, which is mounted and never read or printed. "
            "A changed plan is refused (gitops-plan-changed). Given a "
            "composition module (infra.py, no :ATTR) the controller config holds "
            "every source, component and environment of it instead "
            "(composition-invalid when it does not load); without --kubeconfig "
            "the composition's Cluster credentials profile is used. "
            + _EXPLICIT_CONTEXT,
        ),
        "gitops sync": _C(
            "Ask the GitOps controller to deploy an environment (or every one) "
            "now at its revision; --component also rebuilds that component.",
            reads=("kubeconfig or --state-dir",),
            writes=("--state-dir requests (local controller)",),
            cluster="writes",
            safe_to_retry=True,
            contract="conforms",
            exit_codes=(0, 2),
            notes="Writes a request to the ConfigMap piceli-gitops-requests; the "
            "controller deploys on its next poll with the environment's usual "
            "approval (auto_approve or gitops approve of the plan hash). An "
            "unknown environment or component is dropped "
            "(gitops-request-invalid in gitops status); --component needs a "
            "composition controller.",
        ),
        "env stop": _C(
            "Ask the GitOps controller to stop a composition's named "
            "environment: no replicas, claims kept, not planned or deployed.",
            reads=(
                "--cluster infra.py:CLUSTER (its credentials profile) or --state-dir",
            ),
            writes=("--state-dir requests (local controller)",),
            cluster="writes",
            safe_to_retry=True,
            contract="conforms",
            exit_codes=(0, 2),
            notes="Writes a stop request to the ConfigMap piceli-gitops-requests; "
            "the controller scales the environment's workloads to zero on its next "
            "poll and shows it stopped (reason requested) in gitops status. The "
            "owner's command is the decision: no plan hash. `piceli env start ENV` "
            "ends it. An unknown environment is dropped (gitops-request-invalid in "
            "gitops status).",
        ),
        "env start": _C(
            "Ask the GitOps controller to start a named environment it stopped "
            "on request: scale back, deploy its revision when it moved.",
            reads=(
                "--cluster infra.py:CLUSTER (its credentials profile) or --state-dir",
            ),
            writes=("--state-dir requests (local controller)",),
            cluster="writes",
            safe_to_retry=True,
            contract="conforms",
            exit_codes=(0, 2),
            notes="Writes a start request; a deploy it needs asks for the usual "
            "approval. Refused (gitops-env-stop-declared) for an environment the "
            "composition declares Environment(stopped=True): the declaration "
            "wins.",
        ),
        "gitops disable": _C(
            "Plan and, with --approve HASH, remove the GitOps controller; never "
            "an environment.",
            reads=("kubeconfig",),
            cluster="writes",
            approval_required=True,
            contract="conforms",
            exit_codes=(0, 2, 3),
            notes="Deletes the controller's objects only; keeps the branch "
            "environments, the namespace and (without --delete-state) the state "
            "volume. Exit 3 with the plan hash until --approve. " + _EXPLICIT_CONTEXT,
        ),
        "registry install": _C(
            "Plan and, with --approve HASH, install the in-cluster registry "
            "(Registry.in_cluster) and the containerd mirror on every node.",
            reads=(
                "kubeconfig or the Cluster's credential profile",
                "MODULE:ATTR or composition module (optional)",
            ),
            cluster="writes",
            approval_required=True,
            contract="conforms",
            exit_codes=(0, 2, 3),
            notes="Without --approve prints the plan (Namespace, ConfigMap, "
            "retained PersistentVolumeClaim, Service, registry Deployment "
            "pinned to the node, node agent DaemonSet that writes "
            "certs.d/<name>.<namespace>.svc:<port>/hosts.toml on each node) "
            "and its hash, exit 3. Idempotent: an installed registry plans "
            "unchanged. A changed plan is refused "
            "(cluster-registry-plan-changed). " + _EXPLICIT_CONTEXT,
        ),
        "registry uninstall": _C(
            "Plan and, with --approve HASH, remove the in-cluster registry and "
            "its node mirrors.",
            reads=(
                "kubeconfig or the Cluster's credential profile",
                "MODULE:ATTR or composition module (optional)",
            ),
            cluster="writes",
            approval_required=True,
            contract="conforms",
            exit_codes=(0, 2, 3),
            notes="Keeps the namespace and (without --delete-storage) the claim "
            "with the images. The node agents remove their hosts.toml when they "
            "stop. Workloads that pull from the registry fail to start new "
            "pods afterwards. " + _EXPLICIT_CONTEXT,
        ),
        "registry status": _C(
            "Show the in-cluster registry pod, the mirror on every node and the "
            "storage use.",
            reads=(
                "kubeconfig or the Cluster's credential profile",
                "MODULE:ATTR or composition module (optional)",
            ),
            cluster="reads",
            contract="conforms",
            exit_codes=(0, 2),
            notes="Read-only. state: ready, degraded (registry or a node's "
            "mirror not ready) or not-installed. Storage use is the claim's: "
            "the kubelet's volume stats when they are the claim's own "
            "filesystem (needs nodes/proxy), else du in the registry pod "
            "(needs pods/exec), else null; storage.used_source says which, and "
            "storage.filesystem the shared filesystem's use. " + _EXPLICIT_CONTEXT,
        ),
        "registry forward": _C(
            "Keep a loopback port-forward to the in-cluster registry Service open "
            "for pushes from this machine.",
            reads=("kubeconfig", "kubectl"),
            cluster="reads",
            long_running=True,
            contract="conforms",
            exit_codes=(0, 2),
            notes="Prints one JSON object with the local push URL, then runs "
            "until interrupted. Pushes through it go by digest; nodes pull "
            "by the stable name. " + _EXPLICIT_CONTEXT,
        ),
        "cluster init": _C(
            "Plan and, with --approve HASH, set a declared cluster up: node role "
            "labels, the in-cluster registry and its node mirrors, the GitOps "
            "controller's foundation and the UI.",
            reads=("MODULE:ATTR (piceli.infra.Cluster)", "credential profile"),
            cluster="writes",
            approval_required=True,
            contract="conforms",
            exit_codes=(0, 2, 3),
            notes="Reaches the cluster with the declaration's credential profile "
            "(or --profile) and refuses one whose API server is not Cluster(api=) "
            "(cluster-api-mismatch). Without --approve prints the plan (Node "
            "label changes, objects) and its hash, exit 3. Idempotent: a re-run "
            "plans only changes (unchanged, exit 0). After applying it waits up "
            "to --wait seconds for the node mirrors and lists k3s nodes that "
            "need a k3s restart (restart_needed); it never restarts k3s. The "
            "controller's configuration and Deployment come from piceli gitops "
            "enable.",
        ),
        "cluster status": _C(
            "Show a declared cluster's nodes and labels, registry and node "
            "mirrors (k3s restarts needed), controller, UI and Git Secret.",
            reads=("MODULE:ATTR (piceli.infra.Cluster)", "credential profile"),
            cluster="reads",
            contract="conforms",
            exit_codes=(0, 2),
            notes="Read-only. state: ready, degraded (problems lists why) or "
            "not-initialized. The Git Secret shows its key names only.",
        ),
        "secrets git": _C(
            "Store the Git token the GitOps controller and cluster build Jobs "
            "use (Secret piceli-build-git: username, password).",
            reads=(
                "stdin (--prompt)",
                "MODULE:ATTR (piceli.infra.Cluster)",
                "credential profile",
            ),
            cluster="writes",
            contract="conforms",
            exit_codes=(0, 2),
            notes="The token is read from stdin only (typed without echo, or "
            "piped); a token argument or PICELI_GIT_TOKEN is refused "
            "(secrets-token-refused) without echoing it. Creates or updates the "
            "Secret without a plan; prints its name and key names, never a "
            "value. Needs piceli cluster init first (cluster-not-initialized).",
        ),
        # -------------------------------------------------------- infra
        "infra plan": _C(
            "Render the OpenTofu configuration of a declared Infrastructure, "
            "plan it and print the changes, the monthly estimate and the plan hash.",
            reads=(
                "MODULE:ATTR (piceli.infra.Infrastructure)",
                "tofu binary",
                "credentials (provider token, state passphrase)",
                "provider API (plan refresh, prices)",
            ),
            writes=("state directory (configuration, lock file, providers)",),
            contract="conforms",
            exit_codes=(0, 2, 3),
            notes="Never changes a resource. Exit 3 with the plan and approve_command "
            "(piceli infra apply ... --approve HASH); 0 and state unchanged when "
            "nothing changes. Refuses a plan that would change or delete a resource "
            "Piceli did not create (infra-foreign-resource). Tokens and the state "
            "passphrase reach tofu through its environment only; its output is redacted.",
        ),
        "infra apply": _C(
            "Apply a declared Infrastructure: plan again and apply that plan only "
            "when its hash is --approve.",
            reads=(
                "MODULE:ATTR (piceli.infra.Infrastructure)",
                "tofu binary",
                "credentials (provider token, state passphrase)",
            ),
            writes=(
                "resources at the provider (servers, IPs, firewalls, DNS records)",
                "encrypted OpenTofu state",
                "state directory records (inventory.json)",
            ),
            approval_required=True,
            safe_to_retry=True,
            long_running=True,
            contract="conforms",
            exit_codes=(0, 1, 2, 3),
            notes="Without --approve it plans (exit 3). A hash that is not the fresh "
            "plan's is refused (infra-plan-changed). A failed tofu apply exits 1 "
            "(infra-apply-failed); plan again to continue. One command at a time "
            "per state (infra-state-locked).",
        ),
        "infra destroy": _C(
            "Destroy what Piceli created for a declared Infrastructure, with "
            "approval of the destroy plan's hash.",
            reads=(
                "MODULE:ATTR (piceli.infra.Infrastructure)",
                "tofu binary",
                "credentials (provider token, state passphrase)",
            ),
            writes=("resources at the provider (deleted)", "encrypted OpenTofu state"),
            approval_required=True,
            long_running=True,
            contract="conforms",
            exit_codes=(0, 1, 2, 3),
            notes="Without --approve prints the destroy plan and its hash (exit 3). "
            "Only resources in Piceli's ownership ledger that carry "
            "piceli.io/managed-by=piceli and piceli.io/infra=<name> may be deleted; "
            "anything else refuses the whole plan (infra-foreign-resource). DNS zones "
            "are never deleted (Piceli manages record sets only). Credential "
            "profiles of registered clusters are left in place.",
        ),
        "infra status": _C(
            "Show a declared Infrastructure's servers (state, addresses, install, "
            "cluster registration, monthly cost), records and estimate.",
            reads=(
                "MODULE:ATTR (piceli.infra.Infrastructure)",
                "state directory records",
            ),
            contract="conforms",
            exit_codes=(0, 2),
            notes="Read-only and offline: no tofu run, no credential, no network; it "
            "reads what the last apply, install and register recorded. --json prints "
            "piceli.infra.status.v1.",
        ),
        "infra install": _C(
            "Run a server's install hook (Server(install=Hook([...]))) with its "
            "addresses, after approval of the rendered command's digest.",
            reads=(
                "MODULE:ATTR (piceli.infra.Infrastructure)",
                "state directory records",
            ),
            writes=("the server (whatever the owner's command does)", "installs.json"),
            approval_required=True,
            safe_to_retry=False,
            long_running=True,
            contract="conforms",
            exit_codes=(0, 1, 2, 3),
            notes="Without --approve prints the rendered argv and its digest (exit 3). "
            "Runs it without a shell, in the current directory, with the caller's "
            "environment minus provider tokens and the state passphrase; its output "
            "goes to stderr. Piceli does not own the OS: the command is the owner's.",
        ),
        "infra register": _C(
            "Wait for a server's k3s and make it the server's declared Cluster "
            "(kubeconfig over SSH or --kubeconfig; a credential profile).",
            reads=(
                "MODULE:ATTR (piceli.infra.Infrastructure)",
                "the server over SSH (host keys, k3s kubeconfig) or --kubeconfig",
                "the cluster's API",
            ),
            writes=(
                "$PICELI_CREDENTIALS_DIR/kubeconfigs/CLUSTER.yaml (mode 0600)",
                "credential profile Cluster(credentials=)",
                "known_hosts and registrations.json in the state directory",
            ),
            cluster="reads",
            approval_required=True,
            long_running=True,
            contract="conforms",
            exit_codes=(0, 1, 2, 3),
            notes="Without --approve prints the cluster, profile and the SSH host-key "
            "fingerprints with a digest (exit 3); approving pins those keys "
            "(StrictHostKeyChecking against them only). The kubeconfig's server is "
            "set to Cluster(api=); it is never printed or put in Git. Exit 1 "
            "(infra-k3s-not-ready) when k3s does not answer with a Ready node within --wait.",
        ),
        "secrets provider": _C(
            "Store a provider API token (or a state backend password) as a local credential.",
            reads=("stdin (--prompt)",),
            writes=("$PICELI_CREDENTIALS_DIR/NAME.json (mode 0600)",),
            contract="conforms",
            exit_codes=(0, 2),
            notes="The token is read from stdin only (typed without echo, or piped); "
            "an argument or HCLOUD_TOKEN in the environment is refused "
            "(infra-credential-refused) without echoing it. Prints the name, never the value.",
        ),
        "secrets state-key": _C(
            "Store the passphrase that encrypts an Infrastructure's OpenTofu state "
            "(--generate, or --prompt).",
            reads=("stdin (--prompt)",),
            writes=("$PICELI_CREDENTIALS_DIR/NAME.json (mode 0600)",),
            contract="conforms",
            exit_codes=(0, 2),
            notes="Never printed; back the file up, the state cannot be read without "
            "it. Refuses to replace an existing key without --replace "
            "(infra-credential-exists).",
        ),
        "gitops status": _C(
            "Show the GitOps controller's health, repository, last poll and "
            "each branch's commit, state and pending approval.",
            reads=("kubeconfig or --state-dir",),
            cluster="reads",
            contract="conforms",
            exit_codes=(0, 2),
            notes="Read-only. Reads the ConfigMap piceli-gitops-status and the "
            "controller Deployment (or a local --state-dir). Health: healthy, "
            "degraded (last poll failed), stale (no poll for 3 intervals), "
            "starting, down. gitops-not-installed without a controller.",
        ),
        "gitops approve": _C(
            "Approve the pending plan hash of one branch environment; the "
            "controller applies it on its next poll.",
            reads=("kubeconfig or --state-dir",),
            writes=("--state-dir requests (local controller)",),
            cluster="writes",
            approval_required=True,
            contract="conforms",
            exit_codes=(0, 2),
            notes="This is the owner's approval: run it only with the hash the "
            "owner approved. Writes a request to the ConfigMap "
            "piceli-gitops-requests; a hash the environment is no longer "
            "waiting for is dropped (gitops-approval-stale in gitops status).",
        ),
        "gitops run": _C(
            "Run the GitOps controller loop (the controller Deployment's "
            "entrypoint); --once polls once and prints the status.",
            reads=("--config", "git remote", "--credentials-dir"),
            writes=("--state-dir (Git mirror, state, build receipts)",),
            cluster="writes",
            long_running=True,
            safe_to_retry=True,
            contract="conforms",
            exit_codes=(0, 2),
            notes="Deploys branch environments without a per-run approval: "
            "only inside the pipeline's auto_approve policy, or a plan hash "
            "approved with gitops approve; main only on a new tag or a "
            "promotion, and only with an approved hash unless the owner "
            "enabled --main-auto-approve; a named environment by its own "
            "trigger (Branch, Tag, Promote) and with an approved hash unless "
            "it declares auto_approve=True. Scales a branch environment idle "
            "for EnvConfig(idle_stop=...) to zero. One step at a time, bounded retries "
            "with backoff; a failing branch never stops the others. Git "
            "output and credentials are never printed. A composition "
            "controller polls every source, builds only the components whose "
            "source digest changed (a build Job, or this machine with "
            "--local-build) and rolls only those.",
        ),
        "promote": _C(
            "Ask the GitOps controller to deploy BRANCH@SHA to the main "
            "branch's environment, or to a named environment (promote ENV "
            "BRANCH@SHA).",
            reads=("kubeconfig or --state-dir",),
            writes=("--state-dir requests (local controller)",),
            cluster="writes",
            contract="conforms",
            exit_codes=(0, 2),
            notes="Writes a request; the controller accepts only a commit it "
            "saw on that branch (gitops-promote-unknown otherwise), a named "
            "environment only when it follows Promote() "
            "(gitops-promote-not-allowed), and the environment then waits for "
            "gitops approve of its plan hash.",
        ),
        "doctor": _C(
            "Check free disk and memory against the next build's needs, and "
            "the tools the pipeline uses.",
            reads=(
                "pipeline module or --state-dir",
                "build receipts",
                "docker, docker buildx, kubectl (version only)",
            ),
            contract="conforms",
            exit_codes=(0, 1, 2),
            notes="Read-only; never contacts a cluster. Exit 1 with a warning "
            "(runner-disk-low, runner-memory-low, runner-tool-missing); the "
            "need is estimated from the last build receipts.",
        ),
        "watch": _C(
            "Follow a deploy run from its journal: stage changes, progress "
            "lines, then the outcome (JSON lines with --json).",
            reads=("pipeline module or --state-dir", "state_dir"),
            contract="conforms",
            exit_codes=(0, 1, 2),
            notes="Read-only and offline (never the cluster); stdout is JSON "
            "lines piceli.watch-event.v1: snapshot, stage/run/progress, result. "
            "With shared state it reads the local working copy (piceli state "
            "pull first). --once prints the current state; --timeout ends with "
            "watch-timeout (exit 1) and leaves the run alone.",
        ),
        "runs": _C(
            "List a pipeline's deploy runs with state, release, duration and "
            "summary files.",
            reads=("pipeline module or --state-dir", "state_dir"),
            contract="conforms",
            exit_codes=(0, 2),
            notes="Read-only; newest first. With shared state it reads the local "
            "working copy (piceli state pull first). summary.json follows "
            "docs/schemas/piceli-run-summary-v1.schema.json.",
        ),
        # ------------------------------------------- 0.9.0 restore points
        "restore-points": _C(
            "List a pipeline's restore points (verified claim archives), newest "
            "first; --verify reads every archive back.",
            reads=("pipeline module", "restore point directory"),
            contract="conforms",
            exit_codes=(0, 1, 2),
            notes="Read-only and offline (never the cluster); prints ids, claims, "
            "sizes and digests, never file contents. With --verify, exit 1 when "
            "an archive does not match its SHA-256 or content digest (each "
            "claim's check names the code).",
        ),
        "restore": _C(
            "Put a restore point back into its claims: plan (exit 3 with the "
            "restore_hash), then --approve HASH.",
            reads=("pipeline module", "restore point directory", "kubeconfig"),
            writes=("restore point directory (restores/ and verifies/ receipts)",),
            cluster="writes",
            approval_required=True,
            safe_to_retry=True,
            long_running=True,
            contract="conforms",
            exit_codes=(0, 1, 2, 3),
            notes="Without --approve it only reads: verifies the archives, "
            "checks the claims exist and lists the writers it would stop. With "
            "--approve HASH it plans again (restore-plan-changed on any "
            "difference), holds the pipeline's state lock, scales the writers "
            "to zero and waits until their pods are gone, empties each claim and "
            "extracts its archive in a helper Job, checks the content digest in "
            "the cluster and scales the writers back. Every file in the claims "
            "is replaced; run it again after a failure. With --to-new-claim "
            "(and --all for every verified point) it touches no live claim and "
            "stops no writer: the plan (exit 3, verify_hash) names one scratch "
            "claim per restored claim; --approve HASH creates them, restores "
            "and checks each content digest, runs the app's restore_verify "
            "command read-only with the workload's image, writes a receipt "
            "with PASS or FAIL per claim (exit 1 on any FAIL), and deletes the "
            "scratch claims unless --keep.",
        ),
        # ----------------------------------------------- environments
        "env up": _C(
            "Deploy a branch into its own namespace (EnvConfig): plan (exit 3 "
            "with the env_hash), then --approve HASH.",
            reads=(
                "pipeline module (--pipeline or $PICELI_PIPELINE)",
                "kubeconfig",
                "state_dir",
                "build receipt (--receipt)",
            ),
            writes=(
                "<state_dir>/branches/<namespace> (journal, release state, secrets)",
            ),
            cluster="writes",
            approval_required=True,
            safe_to_retry=True,
            long_running=True,
            contract="conforms",
            exit_codes=(0, 1, 2, 3),
            notes="The branch's namespace is <prefix><slug> (at most 63 "
            "characters, hash suffix when cut); main maps to the main "
            "namespace, which is never created. Branch environments never "
            "build: images come from --digest NAME=REF or --receipt. The app is "
            "isolated at render time (relative Service names, no NodePort, "
            "hostPort or hostPath, namespace-qualified cluster objects, its own "
            "generated Secrets) and gets a default-deny NetworkPolicy across "
            "namespaces and a ResourceQuota. The plan (--plan, or without an "
            "approval) lists the namespace to create, the environments the "
            "budget stops (max_envs, least recently pushed first; scaled to "
            "zero, kept), the deploy's combined plan and the seed "
            "(--seed-from main). --approve-if-policy runs a branch when "
            "EnvConfig(auto_approve=True), or when the pipeline's auto_approve "
            "policy allows the deploy and nothing is stopped; main only by the "
            "policy. --wait refuses with env-budget-full instead of stopping.",
        ),
        "env down": _C(
            "Delete a branch environment: its claims, namespace and volumes "
            "(approval by env_hash; never main's).",
            reads=("pipeline module (--pipeline or $PICELI_PIPELINE)", "kubeconfig"),
            writes=("<state_dir>/branches/<namespace> (removed)",),
            cluster="writes",
            approval_required=True,
            safe_to_retry=True,
            contract="conforms",
            exit_codes=(0, 1, 2, 3),
            notes="Refuses the main branch and the main namespace always "
            "(env-main-protected) and a namespace without this app's "
            "piceli.io/env-of label or of another branch "
            "(env-namespace-not-managed). The plan lists the claims and the "
            "volumes bound to them; with --approve HASH (or --approve-if-policy "
            "and EnvConfig(auto_approve=True)) it deletes the claims, the "
            "namespace and those volumes. An absent environment prints state "
            "absent (exit 0).",
        ),
        "env seed": _C(
            "Restore main's latest restore point into a branch environment's "
            "claims (approval by env_hash).",
            reads=(
                "pipeline module (--pipeline or $PICELI_PIPELINE)",
                "restore point directory",
                "kubeconfig",
            ),
            writes=("restore point directory (restores/ receipts)",),
            cluster="writes",
            approval_required=True,
            safe_to_retry=True,
            long_running=True,
            contract="conforms",
            exit_codes=(0, 1, 2, 3),
            notes="Uses the restore machinery of `piceli restore`: verifies the "
            "archives, stops the branch's writers, empties and restores each "
            "claim, checks the content digest in the cluster and starts the "
            "writers again. Never reads or writes main's claims (the archives "
            "are local). Refuses the main branch (env-main-protected).",
        ),
        "envs": _C(
            "List every environment: branch, namespace, commit, build and "
            "deploy state, health, age, last push.",
            reads=("pipeline module (--pipeline or $PICELI_PIPELINE)", "kubeconfig"),
            cluster="reads",
            contract="conforms",
            notes="Read-only. Human table on stderr; --json prints one "
            "piceli.envs.v1 object (envs[]: branch, namespace, main, state "
            "running|stopped|absent, health healthy|degraded|stopped|unknown, "
            "commit, build, deploy, created_at, pushed_at, age_seconds, "
            "workloads, gitops, fixed). Named environments are listed after "
            "main with fixed=true and their name as branch.",
        ),
        "logs": _C(
            "Print one workload's logs in an environment (kubectl logs with the "
            "explicit context).",
            reads=(
                "pipeline module (--pipeline or $PICELI_PIPELINE)",
                "kubeconfig",
                "kubectl",
            ),
            cluster="reads",
            long_running=True,
            contract="conforms",
            exit_codes=(0, 1, 2),
            notes="`piceli logs BRANCH WORKLOAD [--previous] [-f] [--tail N]`: "
            "after the checks, output and exit status are kubectl's own (log "
            "lines on stdout); a refusal is the JSON rejection object. WORKLOAD is a Deployment, StatefulSet, "
            "DaemonSet or Job of the app (env-workload-unknown otherwise).",
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
    default = getattr(command, "default_command", None)
    if default is not None and hasattr(command, "commands"):
        # A command with subcommands (``piceli access TARGET`` and ``piceli
        # access stop``): the node stays a runnable command, as it was
        # before it had any, and lists the others under ``subcommands``.
        node = _click_tree(command.commands[default], path)
        node["subcommands"] = [
            _click_tree(sub, (*path, sub_name))
            for sub_name, sub in sorted(command.commands.items())
            if sub_name != default and not getattr(sub, "hidden", False)
        ]
        return node
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
            "rejection": '{"state": "rejected", "reason": "<error code>", '
            '"message": "<human text>", ...} on stdout, exit 2',
            "failure": 'exit 1; the result object names its "reason" (an error code)',
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
        return [
            node,
            *(
                leaf
                for child in node.get("subcommands", ())
                for leaf in leaf_commands(child)
            ),
        ]
    return [leaf for child in node["commands"] for leaf in leaf_commands(child)]
