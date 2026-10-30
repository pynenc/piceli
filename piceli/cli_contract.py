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
  required (nothing was executed); ``75`` (``builder serve`` only) the
  controller runs another Piceli version.

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
#: 0.33.0: ``builder serve`` only (``EX_TEMPFAIL``).
EXIT_VERSION_MISMATCH = 75

EXIT_CODES: Mapping[int, str] = MappingProxyType(
    {
        EXIT_OK: "success",
        EXIT_FAILED: "the operation ran but did not succeed (not ready, drift, build failed)",
        EXIT_REJECTED: "rejected before any change (stdout: the rejection object)",
        EXIT_APPROVAL: "approval required; nothing was executed",
        EXIT_VERSION_MISMATCH: "builder serve: the controller runs another Piceli "
        "version; install it and start again (nothing was claimed)",
    }
)

HELP_SCHEMA = "piceli.help.v1"


def emit_json(value: Mapping[str, Any]) -> None:
    """Print one JSON object on stdout (sorted keys, one line)."""
    sys.stdout.write(json.dumps(value, sort_keys=True, default=str) + "\n")
    sys.stdout.flush()


_STAMPED = False


def stamp_lines(on: bool = True) -> None:
    """Prefix every human line with its UTC time (0.17.0; ``gitops run``).

    A long-running process (the GitOps controller) prints lines read later
    in ``kubectl logs``, where a line without its time cannot be placed. The
    Python ``logging`` lines (an OTLP exporter's errors) get the same prefix.
    """
    import logging
    import time

    global _STAMPED
    _STAMPED = on
    if on and not logging.getLogger().handlers:
        handler = logging.StreamHandler(sys.stderr)
        formatter = logging.Formatter(
            "%(asctime)s %(name)s: %(message)s", "%Y-%m-%dT%H:%M:%SZ"
        )
        formatter.converter = time.gmtime
        handler.setFormatter(formatter)
        logging.getLogger().addHandler(handler)


@contextmanager
def stamped_lines() -> Iterator[None]:
    """:func:`stamp_lines` for the duration of a command (restored after)."""
    was = _STAMPED
    stamp_lines()
    try:
        yield
    finally:
        stamp_lines(was)


def say(message: str) -> None:
    """Print human text on stderr (each line with its UTC time after
    :func:`stamp_lines`)."""
    if _STAMPED:
        from datetime import UTC, datetime

        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        message = "\n".join(f"{now} {line}" for line in message.split("\n"))
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
        # ------------------------------------------------------- bundle
        "bundle": _C(
            "Package the app for a cluster Piceli never accesses (kustomize, prepare.sh, images; or a Helm chart).",
            reads=(
                "module/app file",
                "build receipt and its image archives (--receipt)",
                "credentials file (--helm --push)",
            ),
            writes=(
                "--out bundle directory",
                "OCI registry (--helm --push --approve DIGEST: chart blobs, manifest, version tag)",
            ),
            approval_required=True,
            contract="conforms",
            exit_codes=(0, 1, 2, 3),
            notes="Approval applies to --helm --push only: without --approve it "
            "prints the chart's deterministic digest and exits 3; --approve DIGEST "
            "pushes path/<name>:<version> as helm push does. --helm writes a Helm "
            "3 chart (Chart.yaml, values.yaml, values.schema.json, templates/) "
            "instead of the kustomize bundle: settings, secret inputs and toggles "
            "are values; generated Secrets are made at install and kept across "
            "upgrades with lookup. Otherwise never contacts a cluster or registry and writes no secret "
            "value. Renders like `piceli render --env`; refuses objects that "
            "break a foreign-cluster safety rule (bundle-unsafe-<rule>, with "
            "every violation) unless the App waives it per object "
            "(app.safety_exception). Writes a kustomize base and overlays/dev, "
            "prepare.sh (the generated Secrets, made in the client's cluster "
            "with kubectl and openssl), OCI image archives with SBOMs, "
            "INSTALL.md, UNINSTALL.md, bundle.json and SHA256SUMS. --out must "
            "be absent or empty; nothing is left behind on failure.",
        ),
        "support-bundle": _C(
            "Collect a read-only, redacted snapshot of a namespace (no Secrets, no env values).",
            reads=("kubeconfig",),
            writes=("--out .tar.gz (mode 0600, never overwritten)",),
            cluster="reads",
            contract="conforms",
            notes=_EXPLICIT_CONTEXT
            + " GET requests only (any other method is refused before it is "
            "sent); Secrets are never requested; env and ConfigMap values are "
            "never collected; logs, events and conditions are redacted. The "
            "archive's manifest.json lists every file, its sha256 and the "
            "request methods.",
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
        "artifacts publish-files": _C(
            "Publish local files as one OCI artifact (each file one layer, by its sha256).",
            contract="conforms",
            reads=("the files", "credentials file or Docker config"),
            writes=("OCI registry (blobs, manifest, tag)", "--out receipt"),
            approval_required=True,
            safe_to_retry=True,
            long_running=True,
            exit_codes=(0, 1, 2, 3),
            notes="0.32.0. Without --approve it prints the plan (every file's "
            "name, size and digest, the manifest digest) and its digest (exit 3) "
            "and contacts nothing. Each file is stored as it is, so its blob "
            "digest is its sha256; a file changed since the plan is refused "
            "(publish-input-changed). A tag that names another manifest is "
            "refused unless --move-tag. Never prints a credential.",
        ),
        "artifacts fetch-file": _C(
            "Download one published file (a blob) by its digest and check it.",
            contract="conforms",
            reads=("OCI registry (one blob)", "credentials file or Docker config"),
            writes=("--out PATH (only when the digest matches)",),
            safe_to_retry=True,
            exit_codes=(0, 1, 2),
            notes="0.32.0. Anonymous unless --credentials or --docker-config: a "
            "public repository's anonymous pull token is fetched as the registry "
            "asks. blob-digest-mismatch leaves --out untouched.",
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
        "host module": _C(
            "Render the NixOS module that installs a NixHost's agent (the agent, "
            "its configuration, a systemd service and timer).",
            reads=("MODULE:ATTR (piceli.infra.NixHost)",),
            writes=("--output FILE (else the module on stdout)",),
            contract="conforms",
            exit_codes=(0, 2),
            notes="0.24.0. Never contacts the host or the cache. The module holds "
            "no secret (paths of root-only files only); commit it and import it "
            "from the host's configuration. With --output prints one JSON object "
            "(path, sha256).",
        ),
        "host status": _C(
            "Read a Nix host agent's status over SSH: the current and previous "
            "builds, the last attempt with its stages, checks and rollback.",
            reads=(
                "MODULE:ATTR (piceli.infra.NixHost), its name with --infra, or "
                "an SSH target",
                "--infra MODULE[:ATTR] (declared hosts, applied address, pinned "
                "host keys)",
                "the host's /var/lib/piceli-host/status.json (ssh)",
            ),
            contract="conforms",
            exit_codes=(0, 2),
            notes="0.24.0. Read-only; no cluster (works with every cluster down). "
            "ssh runs with BatchMode and strict host key checking "
            "(host-unreachable, host-status-missing).",
        ),
        "targets": _C(
            "One status view of every target: each GitOps environment and each "
            "NixHost with its commit or tag, the product commit it pins, its "
            "checks and its state.",
            reads=(
                "--composition MODULE (environments, NixReleases)",
                "--infra MODULE (NixHosts, applied addresses, pinned host keys)",
                "the controller's status, read once: --kubeconfig/--context or --state-dir, "
                "else the composition's home Cluster with its piceli login profile",
                "each host's /var/lib/piceli-host/status.json (ssh, in parallel)",
            ),
            cluster="reads",
            contract="conforms",
            exit_codes=(0, 1, 2),
            notes="0.25.0. Read-only; safe to retry. --json prints one "
            "piceli.targets.v1 object. An unreachable controller or host never "
            "fails the view: its rows say unreachable with the reason (host rows "
            "still work with the home cluster down; a missing login profile of "
            "the home cluster shows profile-not-found with a piceli login hint). Exit 0: every target "
            "reachable, none failed; 1: a target failed, rolled back, is "
            "unreachable or its checks failed; 2: no module and no controller "
            "named (targets-input-required) or a module that does not load.",
        ),
        "train": _C(
            "Merge candidate branches onto the base one tested merge at a time; "
            "eject the failing ones with the step that failed.",
            reads=(
                "the train spec module (MODULE[:ATTR]), then the same module in each merged tree",
                "the Git repository (branches, base, tested)",
                "the steps' tools (cargo, scripts of the merged tree)",
                "Train(builds=): the declared build hosts (their engines, queues, health)",
                "--promote-run: an earlier run's record",
            ),
            writes=(
                "worktrees of the repository at stable paths under the train cache (removed after each trial)",
                "unreferenced merge commits; refs/piceli/train/<name>/last",
                "$XDG_STATE_HOME/piceli/train/<name> (run records, events, logs, flaky.jsonl, the queue)",
                "$XDG_CACHE_HOME/piceli/train (slot locks, shared cargo targets, the content store)",
                "build hosts: dev runs of the merged tree (their queues, warm caches, health events)",
                "--promote: the tag, tested and the base (fast-forward only), publish steps",
                "--promote with Step.images: images pushed to the cluster's registry, a prebuilt request to its controller",
                "--push: the remote's tag, tested and base",
                "--compare: a scratch clone (removed), the old command's output in the run's directory",
            ),
            approval_required=True,
            safe_to_retry=True,
            long_running=True,
            contract="conforms",
            exit_codes=(0, 1, 2),
            notes="0.26.0. Runs the train's steps, which run code: the explicit "
            "branch list is the approval, and --promote/--push the approval to "
            "move refs and push. Without --promote no ref changes; nothing is "
            "pushed without --push (which needs --promote). Each candidate set "
            "is merged with git merge --no-ff onto the train's head in a "
            "temporary worktree; its steps come from the merged tree's spec. "
            "A failing set is bisected. --json prints one piceli.train.v1 "
            "object. --queue on a busy train writes a queued request (keeping "
            "its flags, --promote-run too, with last resolved when queued) and "
            "returns at once (piceli.train.queued.v1, exit 0); "
            "every run serves the train's queue when it ends. A run that "
            "raises midway (a --promote-run too) is recorded failed "
            "(run-failed: train-git-failed, train-host-unavailable, "
            "train-run-failed, train-run-cancelled), notified, exit 1. Exit 0: every branch joined (or was already in) and the "
            "promotion and push asked for succeeded; 1: a branch was ejected "
            "(train-step-failed, train-step-timeout, train-merge-conflict, train-merge-failed, "
            "train-spec-invalid), the promotion was refused "
            "(train-not-fast-forward, train-checkout-dirty), a publish step "
            "or the push failed; 2: rejected before anything ran "
            "(train-not-found, train-branch-unknown, train-busy, ...). "
            "Steps may run on the train's build hosts (Train(builds=, hosts=)) "
            "through the dev run machinery; a step with inputs that passed "
            "before is reused. --promote-run RUN promotes an earlier run's head "
            "without running it (train-run-unknown, train-not-fast-forward); "
            "no host for a step fails the run (train-host-unavailable). "
            "--compare COMMAND (0.28.0) runs the old command in a scratch clone "
            "first, then the train on the same branches without promoting, and "
            "prints both side by side (piceli.train-compare.v1 under --json); "
            "exit 0 when both agree on pass or fail, 1 when they disagree; "
            "refuses --promote, --push, --queue, --retire, --promote-run "
            "(train-compare-invalid).",
        ),
        "train-init": _C(
            "Write a starting train spec: from a shell script (its test "
            "commands become steps) or as a commented template.",
            reads=(
                "--from: the shell script",
                "the directory's project files (Cargo.toml, pyproject.toml, package.json)",
                "the written spec, loaded once with the train's loader",
            ),
            writes=(
                "the spec file (only a new file, or an existing one with --force)",
            ),
            approval_required=False,
            safe_to_retry=False,
            long_running=False,
            contract="conforms",
            exit_codes=(0, 1, 2),
            notes="0.28.0. Also spelled piceli train init. Never runs the script. "
            "cargo, pytest (and uv/poetry run, python -m), tox, nox, npm, pnpm, "
            "yarn, make, just and go commands become steps; git commands are what "
            "the train does; shell glue is skipped; every other command line "
            "stays in the spec as a TODO comment with a hint. --images "
            "COMPOSITION[:ATTR] [--repository URL --branch B] adds Step.images; "
            "--host NAME [--builds MODULE:ATTR] runs the cargo steps on a build "
            "host. --json prints one piceli.train-init.v1 object. Exit 0: written "
            "and it loads; 1: written but it does not load (load_error); 2: "
            "nothing written (train-init-exists, train-init-invalid).",
        ),
        "train-prune": _C(
            "Bound what a train keeps on this machine: trial worktrees, cargo "
            "target directories, the content store and old runs.",
            reads=(
                "the train cache ($XDG_CACHE_HOME/piceli/train)",
                "the train's state directory (runs)",
                "a train spec, when given instead of a name (its retention)",
            ),
            writes=(
                "removes leftover worktrees, unused or least recently used target "
                "directories and store records, unreferenced store bytes, old runs",
            ),
            approval_required=False,
            safe_to_retry=True,
            long_running=False,
            contract="conforms",
            exit_codes=(0, 2),
            notes="0.26.0. Never removes what a running trial holds, the newest "
            "runs (keep_runs) or flaky.jsonl. --dry-run removes nothing. Every "
            "train run prunes the same way at its end. --json prints one "
            "piceli.train-prune.v1 object (removed: path, kind, bytes, why; "
            "sizes and limits).",
        ),
        "trains": _C(
            "Show the trains: the run in progress (branches, step, start, elapsed, "
            "host, log tail), recent runs with step durations, per-step trends, "
            "flaky tests, and which run produced what each target runs.",
            reads=(
                "$XDG_STATE_HOME/piceli/train/<name> (run records, events.jsonl, "
                "step logs, flaky.jsonl, the lock's owner)",
                "--composition/--infra: what piceli targets reads (the controller, "
                "hosts over ssh)",
            ),
            cluster="reads",
            long_running=True,
            contract="conforms",
            exit_codes=(0, 2),
            notes="0.26.0. Read-only; safe to retry; never takes a train's lock. "
            "--json prints one piceli.trains.v1 object. --follow streams the "
            "running step's log until the run ends. A record still running "
            "with no live process holding the lock shows as interrupted. Reads "
            "a cluster only with --composition/--infra. Exit 2: an unknown "
            "train name (train-unknown).",
        ),
        "train-queue": _C(
            "List a train's queue (waiting requests with their branches, flags "
            "and when they were queued), cancel one, or serve it here.",
            reads=(
                "$XDG_STATE_HOME/piceli/train/<name>/queue (waiting, running, done)",
            ),
            writes=(
                "--cancel: moves the request to queue/done as cancelled",
                "--drain: what piceli train writes, for each request served",
            ),
            long_running=True,
            contract="conforms",
            exit_codes=(0, 2),
            notes="0.26.0. Without options read-only; --json prints one "
            "piceli.train.queue.v1 object (a request whose process died shows "
            "stale). --cancel ID cancels a waiting request "
            "(train-queue-not-waiting, train-queue-not-found). --drain runs "
            "each queued request's steps with the flags it was queued with "
            "(the request was the approval) until the queue is empty, first "
            "putting back a request whose serving process died; a request "
            "whose branches are gone is rejected (train-branch-unknown).",
        ),
        "train-wait": _C(
            "Wait until a train run ends and exit with its verdict.",
            reads=(
                "$XDG_STATE_HOME/piceli/train/<name>/runs/<run>/run.json and the "
                "train's lock (every --interval)",
            ),
            long_running=True,
            contract="conforms",
            exit_codes=(0, 1, 2),
            notes="0.26.0. Read-only; safe to retry. RUN is a run id, a queue "
            "id (q-…, waits for the run that serves it; train-queue-cancelled, "
            "train-queue-not-found) or a train's name (its running run, else "
            "its newest). A run that failed midway (run-failed) exits 1. --json prints "
            "the run's piceli.train.summary.v1. Exit 0: the run ended ok; 1: "
            "a branch was ejected, the promotion or push failed, the run was "
            "killed (train-interrupted) or --timeout ran out "
            "(train-wait-timeout); 2: no such run (train-run-not-found).",
        ),
        "host wait": _C(
            "Wait until a Nix host runs a commit or tag with its checks passed.",
            reads=(
                "MODULE:ATTR (piceli.infra.NixHost) or an SSH target",
                "the host's status file (ssh, every --interval)",
            ),
            long_running=True,
            contract="conforms",
            exit_codes=(0, 1, 2),
            notes="0.24.0. Read-only; no cluster. Exit 0: the host runs REV and its "
            "checks passed; 1: the attempt was refused or failed "
            "(host-deploy-failed), rolled back (host-rolled-back), or "
            "host-wait-timeout. The JSON carries the attempt's stage times.",
        ),
        "host pin": _C(
            "Make a Nix host deploy a tag's published build (an older one too) "
            "and hold it: no newer build until unpin.",
            reads=(
                "MODULE:ATTR (piceli.infra.NixHost) or an SSH target",
                "--infra MODULE[:ATTR] (address, pinned host keys, host names)",
            ),
            writes=("the host's system and /var/lib/piceli-host (ssh, as root)",),
            long_running=True,
            contract="conforms",
            exit_codes=(0, 1, 2),
            notes="0.24.0. No approval digest: it only goes to a build that is "
            "signed, published and verified like every deploy, with checks and "
            "a rollback on failure. Runs piceli-host-agent pin TAG over SSH. "
            "Exit 1: the build failed its switch or checks and the host is back "
            "on its previous system; 2: refused before any change "
            "(host-pin-not-found, host-signature-invalid, host-busy). Reported "
            "to the OTLP collector as piceli.host.pin.",
        ),
        "host unpin": _C(
            "Drop a Nix host's pin or rollback hold: it follows its declared "
            "ref again from the next poll (started at once).",
            reads=("MODULE:ATTR (piceli.infra.NixHost) or an SSH target",),
            writes=("/var/lib/piceli-host/state.json on the host (ssh, as root)",),
            contract="conforms",
            exit_codes=(0, 2),
            notes="0.24.0. Changes no system itself; the poll it starts deploys "
            "the newest build of the followed ref. Reported as piceli.host.unpin.",
        ),
        "host rollback": _C(
            "Switch a Nix host back to its previous system (already in its "
            "store) and hold it until unpin.",
            reads=("MODULE:ATTR (piceli.infra.NixHost) or an SSH target",),
            writes=("the host's system and /var/lib/piceli-host (ssh, as root)",),
            contract="conforms",
            exit_codes=(0, 1, 2),
            notes="0.24.0. No approval digest: the previous system was verified "
            "when it was deployed. Runs the checks and switches forward again "
            "when one fails (exit 1). The hold keeps the next poll from "
            "deploying what was rolled away from. host-rollback-unavailable "
            "when no previous system is recorded or in the store. Reported as "
            "piceli.host.rollback.",
        ),
        "host secrets": _C(
            "Put a Nix host's age identity (its sops-nix key) and the cache's "
            "read keys on the host: plan, then --approve DIGEST.",
            reads=(
                "MODULE:ATTR (piceli.infra.NixHost with age_identity= or a cache "
                "with a read pair: the bucket's S3Key(access='read') or "
                "NixCache(read_credentials=))",
                "$PICELI_CREDENTIALS_DIR/NAME.json (age-identity, s3-keys)",
                "the host's key file and cache.env, by fingerprint only (ssh)",
            ),
            writes=(
                "NixHost(age_key_file=) on the host, 0600 root (ssh stdin, "
                "atomic rename)",
                "/var/lib/piceli-host/cache.env, 0600 root (0.25.0)",
            ),
            approval_required=True,
            contract="conforms",
            exit_codes=(0, 1, 2, 3),
            notes="0.24.0. The plan lists host, path, mode, owner, public key and "
            "whether the host has the same, a different or no key (exit 3 with "
            "the digest; 0 when it has the same). A different key is refused "
            "without --replace (host-secret-exists). The key travels on ssh's "
            "stdin, never in an argument, log or state. After writing, the "
            "host re-runs switch-to-configuration switch so sops-nix decrypts. "
            "0.25.0: with a bucket S3Key(access='read') the plan's cache lists the "
            "read pair (credential, short digest, same/different/absent); it is "
            "written without --replace and needs no activation (the agent reads "
            "it each poll). 0.25.3: an owner-made pair, NixCache(read_credentials="
            "NAME), is planned, digested, compared and written the same way; "
            "missing, it is refused infra-credential-missing with the command "
            "that stores it (piceli secrets s3 NAME --prompt).",
        ),
        "gitops enable": _C(
            "Plan and, with --approve HASH, install the GitOps controller that "
            "deploys branches from Git (one environment per branch).",
            reads=("kubeconfig", "the pipeline module under --root (if present)"),
            cluster="writes",
            approval_required=True,
            contract="conforms",
            exit_codes=(0, 1, 2, 3),
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
            "the composition's Cluster credentials profile is used. 0.28.0, "
            "--wait: after the install, waits until every environment runs its "
            "revision with its checks passed (exit 0), reports the first failure "
            "(environment, stage, reason code, log tail; a composition that does "
            "not import; exit 1) or the timeout (gitops-wait-timeout, exit 2); "
            "progress lines on stderr, the result under wait in the JSON. "
            "0.28.0, --canary (with --approve, a composition): before the plan "
            "replaces a running controller, runs the new image once as a Job "
            "(gitops canary: scratch state, its own ConfigMap, plan only) that "
            "imports the composition, resolves every environment, loads its "
            "builds and plans it; a failure refuses the replacement "
            "(gitops-canary-failed, exit 2) unless --force; the Job and its "
            "ConfigMap are removed when it ends; the result under canary in "
            "the JSON. " + _EXPLICIT_CONTEXT,
        ),
        "gitops cache clear": _C(
            "Ask the GitOps controller to empty its cluster build cache (the "
            "cargo targets on the build cache claim), or only some crates' "
            "artifacts (--crate).",
            reads=("kubeconfig or --state-dir",),
            writes=("--state-dir requests (local controller)",),
            cluster="writes",
            safe_to_retry=True,
            contract="conforms",
            exit_codes=(0, 2),
            notes="0.19.0. Writes a request to the ConfigMap "
            "piceli-gitops-requests; the controller runs a short Job on the "
            "builder node between steps and records controller.cache_clear "
            "(at, crates, state) in gitops status. The next build is cold for "
            "what was cleared. A controller without --builder-image drops it "
            "(gitops-request-invalid).",
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
            "composition controller. 0.25.0: ENV may name a NixRelease (its host): "
            "the controller publishes its newest tag now, with the release's "
            "approval; a sync of every environment also retries failed releases.",
        ),
        "gitops build": _C(
            "Ask the GitOps controller to build a NixRelease (--ref: a tag of "
            "it, its branch or a full commit; the newest without one) or an "
            "environment's images now, through the same gate, plan and approval.",
            reads=("kubeconfig or --state-dir",),
            writes=("--state-dir requests (local controller)",),
            cluster="writes",
            safe_to_retry=True,
            contract="conforms",
            exit_codes=(0, 2),
            notes="0.26.0. Writes a build request; the controller plans it on its "
            "next poll, recorded as triggered by cli with the requester (--by, "
            "else the user name). A NixRelease waits in approval-required unless "
            "auto_approve or --approve names the plan's digest. An environment "
            "rebuilds every image at the commit it follows (--ref may name it), "
            "since 0.29.0 also when that commit is already deployed. "
            "An unknown target or ref is dropped (gitops-request-invalid or "
            "gitops-build-ref-unknown in gitops status).",
        ),
        "gitops cancel": _C(
            "Ask the GitOps controller to cancel a NixRelease's or an "
            "environment's queued or running build.",
            reads=("kubeconfig or --state-dir",),
            writes=("--state-dir requests (local controller)",),
            cluster="writes",
            safe_to_retry=True,
            contract="conforms",
            exit_codes=(0, 2),
            notes="0.26.0. Unclaimed, the build is dropped; claimed, the build "
            "host stops at its next stage and its answer is refused. A cancelled "
            "image build fails its step until piceli gitops sync; a cancelled "
            "publish waits for a newer tag, a sync or a build. Nothing running: "
            "gitops-build-not-running in gitops status.",
        ),
        "gitops builds": _C(
            "List every build the GitOps controller knows (NixRelease publishes, "
            "image builds, train handoffs) with its trigger, commit, tag, host, "
            "stage times, log tail, duration and the targets that run it now.",
            reads=("kubeconfig or --state-dir", "the controller's status and history"),
            cluster="reads",
            contract="conforms",
            exit_codes=(0, 2),
            notes="0.26.0. Read-only. --json prints piceli.builds.v1; --target "
            "and --state (a stage, or active) filter it.",
        ),
        "cycle report": _C(
            "Report how long push to running-and-checked takes per target "
            "(environment, Nix release, Nix host, train): p50, p90, count, "
            "slowest phase and slowest cycle.",
            reads=(
                "kubeconfig or --state-dir (optional)",
                "the controller's status and history",
                "local train run records",
                "host switches piceli host wait kept",
            ),
            cluster="reads",
            contract="conforms",
            exit_codes=(0, 2),
            notes="0.28.0. Read-only. --json prints piceli.cycle-report.v1; "
            "--markdown prints a Markdown table on stdout instead; --since "
            "(24h, 7d or an ISO time) and --target filter it "
            "(cycle-options-invalid). Without --kubeconfig/--context or "
            "--state-dir the controller is not read.",
        ),
        "gitops history": _C(
            "List each environment's deploy runs from the controller's "
            "deployment history, newest first: outcome, trigger, commits, "
            "digests per component, checks, stage and cycle times, and the "
            "run's and the release's time windows.",
            reads=("kubeconfig or --state-dir", "the controller's history"),
            cluster="reads",
            contract="conforms",
            exit_codes=(0, 2),
            notes="0.38.0. Read-only; the document the UI's Deployment history "
            "reads. --json prints piceli.gitops-runs.v1 (each run is the "
            "piceli.gitops-history.v1 entry with window and release_window). "
            "No history yet: gitops-history-unavailable; an unknown ENV: "
            "gitops-run-unknown.",
        ),
        "gitops show": _C(
            "Show one deploy run of the controller's history (its id or "
            "run_id, or a unique prefix); --logs adds the log tail it kept.",
            reads=("kubeconfig or --state-dir", "the controller's history"),
            cluster="reads",
            contract="conforms",
            exit_codes=(0, 2),
            notes="0.38.0. Read-only. --json prints piceli.gitops-run.v1 (env, "
            "run; logs and logs_note with --logs). The controller keeps only "
            "the redacted log tail of a failed build. An unknown or ambiguous "
            "run: gitops-run-unknown.",
        ),
        "gitops compare": _C(
            "Compare two deploy runs of an environment, by default the running "
            "release and the one before it: source commits, components whose "
            "image changed, objects added, removed and changed, and both time "
            "windows.",
            reads=("kubeconfig or --state-dir", "the controller's history"),
            cluster="reads",
            contract="conforms",
            exit_codes=(0, 2),
            notes="0.38.0. Read-only. --json prints piceli.gitops-compare.v1. "
            "Field values are not in the controller's history (fields.available "
            "false); fewer than two runs: gitops-run-unknown.",
        ),
        "gitops suspects": _C(
            "List the deployed images a build host (or the builder node) built, "
            "in every environment and component, from the provenance the "
            "controller recorded; --since narrows it to builds that ended later.",
            reads=("kubeconfig or --state-dir", "the controller's status"),
            cluster="reads",
            contract="conforms",
            exit_codes=(0, 2),
            notes="0.22.0. Read-only. Each image names its environment (and "
            "cluster), component, pull reference, digest and provenance (host, "
            "node, builder image, commits, times).",
        ),
        "gitops rebuild": _C(
            "Ask the GitOps controller to rebuild every deployed image a build "
            "host (or the builder node) built (--since: builds that ended "
            "later) and deploy them.",
            reads=("kubeconfig or --state-dir",),
            writes=("--state-dir requests (local controller)",),
            cluster="writes",
            safe_to_retry=True,
            contract="conforms",
            exit_codes=(0, 2),
            notes="0.22.0. Writes a request to the ConfigMap "
            "piceli-gitops-requests; the controller drops those images' cached "
            "build results and deploys each environment that runs one with its "
            "usual approval and checks (trigger 'sync rebuild HOST'; "
            "controller.last_rebuild in gitops status). No such image: "
            "gitops-request-invalid in gitops status.",
        ),
        "gitops hosts": _C(
            "Show the health events and quarantine the GitOps controller "
            "recorded for its build hosts and builder node.",
            reads=("kubeconfig or --state-dir", "the controller's status"),
            cluster="reads",
            contract="conforms",
            exit_codes=(0, 2),
            notes="0.22.0. Read-only (controller.build_health in gitops status).",
        ),
        "gitops alarms": _C(
            "Show the GitOps controller's alarms (failed or waiting "
            "environments, failed or waiting NixReleases, a composition that "
            "does not import, a new controller version) and the alarms it "
            "could not send; --follow raises each new one as a desktop "
            "notification.",
            reads=("kubeconfig or --state-dir", "the controller's status"),
            cluster="reads",
            contract="conforms",
            exit_codes=(0, 2),
            notes="0.28.0. Read-only. Each alarm is shown once per run of the "
            "command (a resolved one once more); --follow keeps reading every "
            "--interval seconds (--polls N stops after N reads). --json prints "
            "piceli.gitops-alarms.v1 with each alarm as piceli.alarm.v1. The "
            "controller itself sends to the ntfy and webhook targets of "
            "Controller(alarms=Alarms(...)).",
        ),
        "gitops release-host": _C(
            "Ask the GitOps controller to forget a build host's or the builder "
            "node's health events, ending its quarantine.",
            reads=("kubeconfig or --state-dir",),
            writes=("--state-dir requests (local controller)",),
            cluster="writes",
            safe_to_retry=True,
            contract="conforms",
            exit_codes=(0, 2),
            notes="0.22.0. Writes a request to the ConfigMap "
            "piceli-gitops-requests. A docker or native host's own record is "
            "cleared with piceli dev release-host.",
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
        "dev nix build": _C(
            "Build a flake output (a NixOS system, a disk image) as a development "
            "run on a docker or native build host with Nix; with --publish, sign "
            "and push its closure to a NixCache and upload a signed build manifest.",
            reads=(
                "MODULE:ATTR (piceli.infra.DevBuilds, or a Cluster with dev=)",
                "MODULE:ATTR (piceli.infra.NixCache, --publish)",
                "the local Git repository",
                "credentials (signing key, S3 keys; --publish after approval)",
            ),
            writes=(
                "--artifacts-dir RUN/ and NAME.image.json (with --image)",
                "the build host's Nix store",
                "the binary cache and piceli/NAME/REV.json (--publish)",
            ),
            approval_required=True,
            safe_to_retry=True,
            long_running=True,
            contract="conforms",
            exit_codes=(0, 1, 2, 3),
            notes="0.24.0. Without --publish it is a development run (no approval; "
            "covered like dev run). With --publish it prints the plan (commit, "
            "output, host, cache, key) and exits 3; --approve DIGEST builds, then "
            "a separate step on the same host receives the signing key and S3 "
            "keys on stdin only (never the build), pushes with nix copy and the "
            "client uploads the manifest. Prints the store path, the image's "
            "sha256 and the public key, never a secret. Exit 1: the build "
            "failed, no output (dev-nix-no-output), dev-nix-publish-failed, "
            "dev-nix-manifest-failed. --tag-pattern GLOB (a NixHost's Tag "
            "pattern) points the highest version tag of that pattern at the "
            "commit; a --ref tag of another pattern is refused (dev-nix-invalid). "
            "A publish of a gated host's attribute to its cache (a NixRelease or "
            "NixHost with gate= in the --publish module or --composition MODULE; "
            "--release HOST names it) runs the promotion gate at plan time: the commit's flake.lock must pin the gate's input "
            "to a commit its environment ran with passed release checks "
            "(promotion-not-verified, promotion-input-missing); the history is "
            "read as gitops status reads it (--state-dir, --kubeconfig/--context, "
            "else the composition cluster's profile) and an unreadable one "
            "refuses (promotion-history-unreachable). The plan, and the signed "
            "manifest, carry pinned.",
        ),
        "dev nix gc": _C(
            "Remove the builds of old commits from a Nix cache: keep the manifests "
            "of the newest --keep-tags tags (5) and the current --main, and every "
            "path their closures reach; delete the rest after approval.",
            reads=(
                "MODULE:ATTR (piceli.infra.NixCache)",
                "the local Git repository (tags, main)",
                "the cache's objects (write credentials)",
            ),
            writes=("the binary cache (deleted manifests, narinfos, NAR files)",),
            approval_required=True,
            safe_to_retry=True,
            long_running=True,
            contract="conforms",
            exit_codes=(0, 1, 2, 3),
            notes="0.24.0. Without --approve prints the keys it would delete and "
            "the plan digest (exit 3; 0 and state clean when nothing goes). A "
            "path a kept manifest's closure reaches is never deleted; objects "
            "younger than --grace stay. --approve with another digest is "
            "refused (dev-nix-plan-changed); a failed delete exits 1 "
            "(dev-nix-gc-failed). --tag-pattern GLOB (repeatable, each host's "
            "Tag pattern) keeps the newest --keep-tags versions of each pattern, "
            "compared after its prefix; the plan lists tag_patterns.",
        ),
        "dev run": _C(
            "Run a build or test command on the cluster's builder node for a "
            "working tree or a commit, with the shared warm cache; stream its log, print the "
            "result (exit code, phases, tests, cache use, log tail).",
            reads=(
                "MODULE:ATTR (piceli.infra.Cluster with dev=)",
                "credential profile",
                "the local Git repositories of the sources",
            ),
            writes=(
                "--artifacts-dir RUN/ (with --artifact)",
                "the build host's health record (0.22.0, after a machine-health sign)",
            ),
            cluster="writes",
            approval_required=False,
            safe_to_retry=True,
            long_running=True,
            contract="conforms",
            exit_codes=(0, 1, 2),
            notes="Safe for agents: the owner approved what runs may do when "
            "piceli cluster init applied Cluster(dev=DevBuilds(...)); each run is "
            "an isolated pod in piceli-dev (non-root, no token, no Secret, no "
            "cluster network) that only builds in the shared dev cache, never in "
            "release caches. Exit 0 the command passed; 1 it failed "
            "(dev-command-failed), timed out, ran out of memory, was cancelled or "
            "the run broke; 2 refused (dev-not-enabled, dev-ref-unknown, "
            "dev-source-invalid, dev-profile-unknown, dev-run-invalid). The run's "
            "Job is removed on every outcome. 0.22.0: a quarantined build host is "
            "skipped (said on stderr); a run showing a machine-health sign "
            "records an event on its host (health and host_quarantined in the "
            "result).",
        ),
        "builder serve": _C(
            "On a build host: take the builds the GitOps controller publishes "
            '(Controller(builds=("hosts", ...))), build them in the builder '
            "image on the host's container engine, push them with an index per "
            "image, answer built or build-failed.",
            reads=(
                "MODULE:ATTR (piceli.infra.Cluster with dev=DevBuilds(hosts=[...]))",
                "credential profile",
                "the controller's status",
                "each source with this machine's Git access",
                "MODULE:ATTR of each --publish NixCache, and its signing key "
                "and S3 write keys from this machine's credentials (0.25.0)",
            ),
            writes=(
                "the controller's requests (build-claim, built, build-failed, "
                "nix-published, build-host-version)",
                "--version-file PATH: the controller's Piceli version (0.33.0)",
                "the binary cache: closure, signed manifest, tag pointer (--publish)",
                "images in the cluster's registry (through a port-forward)",
                "~/.cache/piceli/builder (Git mirrors), the piceli-build-cache volume",
                "the host's health record (piceli-health-* volumes)",
            ),
            cluster="writes",
            contract="conforms",
            exit_codes=(0, 2, 75),
            notes="0.21.0. Runs until Ctrl-C (--once: one build). The builder "
            "image must be the controller's Piceli version. 0.22.0: each "
            "delivered image's provenance is pushed as an OCI referrer when the "
            "registry takes it; a build showing a machine-health signature "
            "records a health event on the host, and a quarantined host claims "
            "nothing (quarantined in --once --json). 0.25.0: --publish "
            "MODULE:ATTR (repeatable) also takes the nix-publish builds of the "
            "NixReleases that name this host as builder, only for those caches; "
            "every key is read at start (infra-credential-missing, "
            "dev-nix-key-mismatch refuse it) and never leaves the host: a claim "
            "carries the cache key's signature over it. A native host with "
            "--publish only publishes. The publish itself was approved by the "
            "release's policy or gitops approve of its plan digest. 0.25.0: "
            "--publish-only (needs --publish, else dev-nix-invalid) claims only "
            "those publishes on any kind of host, never an image build (said "
            "once), and needs no cluster registry: a remote publisher is a "
            "docker host with an SSH context to itself, since a native host is "
            "the machine running Piceli. A host declared runs=False implies "
            "--publish-only and is refused without --publish or with "
            "--no-publish-only (dev-host-publish-only); without --host it is "
            "never the default. 0.26.0: before each publish the host fetches the "
            "plan's private flake inputs into its own store (its own git-token or "
            'git-ssh-key, or for credentials="controller" the controller\'s '
            "token sealed to the claim's one-time key), else build-failed with "
            "infra-nix-input-unreachable before any build; at start it warns per "
            "release of an input it holds no credential for, and --check claims "
            "nothing and prints piceli.builder-check.v1 (each release's mode, "
            "inputs and credential states; exit 1 when one is missing). 0.29.0: "
            "--check also runs git ls-remote of every source the controller "
            "follows and writes into a scratch directory and the build cache "
            "from inside the builder image (sources and scratch in "
            "piceli.builder-check.v1; exit 1 when one fails). A host the "
            "composition does not declare claims nothing and says so. A failure "
            "before the build's first command (a source it cannot fetch, an "
            "engine that cannot start the build) answers build-failed with "
            "early: the controller hands the build to another host or the "
            "in-cluster fallback and quarantines this host for that source until "
            "gitops release-host. --once --take-next stands by (build-standby) "
            "for the next build it may take, claims it before the fallback and "
            "exits (--timeout seconds, default 1800). --args-file PATH adds the "
            "arguments in PATH (one per line, # comments) at start. 0.33.0: it "
            "claims only while it runs the controller's Piceli version; on a "
            "mismatch it claims nothing (the controller builds in the cluster), "
            "tells the controller its version (build-host-version; builder "
            "status, gitops hosts and the build-host-version alarm show it), "
            "writes the controller's version into --version-file PATH when "
            "given and exits 75 (builder-version-mismatch), for its service to "
            "install that version and start it again. --check reports both "
            "versions (version in piceli.builder-check.v1; exit 1 on a mismatch). "
            "It builds in the controller's builder image (controller.builder_image, "
            "pulled again when it changes; nothing claimed while it cannot be "
            "pulled); --image is an override, said once when it differs.",
        ),
        "builder install": _C(
            "On a build host: write and start a service (systemd on Linux, "
            "launchd on macOS) that runs builder serve with these arguments.",
            reads=(
                "MODULE:ATTR (piceli.infra.Cluster with dev=DevBuilds(hosts=[...]))",
                "credential profile",
                "the controller's status",
                "MODULE:ATTR of each --publish NixCache and its keys from this "
                "machine's credentials (checked, never written to the unit)",
                "PATH (docker, git, kubectl)",
            ),
            writes=(
                "/etc/systemd/system or ~/.config/systemd/user/"
                "piceli-builder-HOST.service (Linux), /Library/LaunchDaemons or "
                "~/Library/LaunchAgents/piceli.builder.HOST.plist (macOS)",
                "systemctl daemon-reload, enable, restart (launchctl bootstrap)",
            ),
            cluster="reads",
            contract="conforms",
            exit_codes=(0, 2),
            notes="0.28.0. Runs the same checks as builder serve --check first "
            "(the profile reaches the controller; every credential the host "
            "needs is present, else infra-credential-missing names it) and "
            "refuses when docker, git or kubectl is missing "
            "(builder-service-invalid). The unit runs the piceli program that "
            "ran install (absolute path, version in its description), in the "
            "directory install ran in, with Restart=on-failure, RestartSec=10 "
            "and KillSignal=SIGINT; it holds directories, never a secret. As "
            "root it needs --user USER (a system service); otherwise it is a "
            "user service. --print writes the unit to stdout and changes "
            "nothing; since 0.29.0 it checks nothing either (the unit is printed "
            "where the secrets are absent, for example for NixOS). A failing "
            "systemctl or launchctl is builder-service-failed.",
        ),
        "builder uninstall": _C(
            "On a build host: stop, disable and remove the service builder "
            "install wrote.",
            reads=("the unit or property list of --host",),
            writes=(
                "the unit or property list (removed); systemctl disable --now "
                "(launchctl bootout)",
            ),
            contract="conforms",
            exit_codes=(0, 2),
            notes="0.28.0. state absent when none is installed (nothing runs).",
        ),
        "builder status": _C(
            "List each build host the GitOps controller heard from: Piceli "
            "version, last seen, live or expired claim, last build and its "
            "outcome, quarantine; and the declared hosts never seen.",
            reads=(
                "MODULE:ATTR (piceli.infra.Cluster with dev=DevBuilds(hosts=[...]))",
                "credential profile",
                "the controller's status",
            ),
            cluster="reads",
            contract="conforms",
            exit_codes=(0, 2),
            notes="0.28.0. Read-only (controller.build_hosts, builds_wanted and "
            "build_health in gitops status); piceli.builder-status.v1 under "
            "--json.",
        ),
        "dev hosts": _C(
            "Show each declared build host: up or down (and why), its platforms, "
            "slots, running and waiting runs.",
            reads=(
                "MODULE:ATTR (piceli.infra.Cluster with dev=)",
                "each host's container engine (docker hosts), the queue files "
                "(native hosts), the builder node (cluster host)",
            ),
            cluster="reads",
            contract="conforms",
            exit_codes=(0, 2),
            notes="0.20.0. Read-only. A docker host is asked with its CLI "
            "(`info`, `ps`, `volume ls`), locally or over its ssh:// context. "
            "0.22.0: each host's health events and quarantine (health).",
        ),
        "dev release-host": _C(
            "Clear a docker or native build host's health events: it is no "
            "longer quarantined, so runs and builder serve use it again.",
            reads=("MODULE:ATTR (piceli.infra.Cluster with dev=)",),
            writes=(
                "the host's health record (its piceli-health-* volumes on a "
                "docker host, health.json in a native host's state_dir)",
            ),
            contract="conforms",
            exit_codes=(0, 2),
            notes="0.22.0. Never contacts the cluster. The builder node's "
            "health is the GitOps controller's: piceli gitops release-host.",
        ),
        "dev status": _C(
            "Show the development-run queue: slots, running and queued runs with "
            "their position, the last finished runs and the cache use.",
            reads=(
                "MODULE:ATTR (piceli.infra.Cluster with dev=)",
                "credential profile",
            ),
            cluster="reads",
            contract="conforms",
            exit_codes=(0, 2),
            notes="Read-only. Reads the Jobs in piceli-dev and the scheduler's "
            "ConfigMap piceli-dev-status (piceli-system).",
        ),
        "dev logs": _C(
            "Print a development run's output: live (--follow), or the recorded "
            "tail of a finished run.",
            reads=(
                "MODULE:ATTR (piceli.infra.Cluster with dev=)",
                "credential profile",
            ),
            cluster="reads",
            contract="conforms",
            exit_codes=(0, 2),
            notes="Read-only. The log on stdout; dev-run-unknown for a run that is "
            "neither live nor among the last 50 recorded.",
        ),
        "dev cache clear": _C(
            "Empty the development builds' cargo cache: every free lineage, one "
            "(--lineage), or only some crates' artifacts (--crate).",
            reads=(
                "MODULE:ATTR (piceli.infra.Cluster with dev=)",
                "credential profile",
            ),
            writes=("a short Job in piceli-dev (deleted when it ends)",),
            cluster="writes",
            contract="conforms",
            exit_codes=(0, 1, 2),
            notes="0.19.0. A lineage a run holds is skipped and listed under busy. "
            "The next run on a cleared lineage is cold. Exit 1: "
            "dev-cache-clear-failed (the Job printed no result).",
        ),
        "dev cancel": _C(
            "Cancel a queued or running development run: its Job and pod are deleted.",
            reads=(
                "MODULE:ATTR (piceli.infra.Cluster with dev=)",
                "credential profile",
            ),
            cluster="writes",
            contract="conforms",
            exit_codes=(0, 2),
            notes="Only your own runs (the run id dev run printed); the run ends "
            "as dev-run-cancelled. dev-run-unknown when it is not queued or running.",
        ),
        "dev schedule": _C(
            "Run the development-run queue in this process (a cluster without the "
            "GitOps controller, which runs it on a thread).",
            reads=(
                "MODULE:ATTR (piceli.infra.Cluster with dev=)",
                "credential profile",
            ),
            writes=("ConfigMap piceli-dev-status in piceli-system",),
            cluster="writes",
            long_running=True,
            contract="conforms",
            exit_codes=(0, 2),
            notes="Starts queued runs into the slots, records finished runs and "
            "deletes their Jobs. --once ticks once and prints the status. Only one "
            "queue should run: not next to a controller that runs it.",
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
        "secrets cluster": _C(
            "Store another cluster's credentials for the GitOps controller "
            "(Secret piceli-cluster-<name>: kubeconfig) on the home cluster.",
            reads=(
                "--kubeconfig FILE --context NAME, or stdin (--prompt: a token)",
                "MODULE:ATTR (piceli.infra.Cluster)",
                "credential profiles",
            ),
            cluster="writes",
            contract="conforms",
            exit_codes=(0, 2),
            notes="One context, certificates and token inlined, no exec plugin "
            "(cluster-credentials-unsupported); the kubeconfig's server must be "
            "Cluster(api=) (cluster-api-mismatch); --server sets the address the "
            "controller uses. Writes the Secret on the cluster with "
            "Controller(...) (or --home) without a plan; prints names only, "
            "never a value. A token argument is refused (secrets-token-refused).",
        ),
        # -------------------------------------------------------- infra
        "infra plan": _C(
            "Render the OpenTofu configuration of a declared Infrastructure, "
            "plan it and print the changes, the monthly estimate and the plan hash.",
            reads=(
                "MODULE:ATTR (piceli.infra.Infrastructure)",
                "tofu binary",
                "credentials (provider token, state passphrase)",
                "provider API (plan refresh, prices, Porkbun API access per domain)",
            ),
            writes=("state directory (configuration, lock file, providers)",),
            contract="conforms",
            exit_codes=(0, 2, 3),
            notes="Never changes a resource. Exit 3 with the plan and approve_command "
            "(piceli infra apply ... --approve HASH); 0 and state unchanged when "
            "nothing changes. Refuses a plan that would change or delete a resource "
            "Piceli did not create (infra-foreign-resource). A plan that changes "
            "Porkbun records checks API access for each domain (infra-dns-api-disabled; "
            "infra-dns-check-failed when it cannot) and records it in checks. Tokens and "
            "the state passphrase reach tofu through its environment only; its output "
            "is redacted. 0.25.3: a Bucket(keys=[S3Key(...)]) at a provider that "
            "cannot create S3 keys (the Infomaniak preset) is refused before "
            "anything runs (infra-s3-keys-unsupported), with the commands that "
            "make and store each pair by hand.",
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
                "resources at the provider (servers, IPs, firewalls, DNS records, "
                "images, bucket S3 key pairs)",
                "encrypted OpenTofu state",
                "state directory records (inventory.json, s3-keys.json, images.json, "
                "image-upload-NAME.log)",
                "$PICELI_CREDENTIALS_DIR/NAME.json of each S3Key (s3-keys, 0600)",
            ),
            approval_required=True,
            safe_to_retry=True,
            long_running=True,
            contract="conforms",
            exit_codes=(0, 1, 2, 3),
            notes="Without --approve it plans (exit 3). A hash that is not the fresh "
            "plan's is refused (infra-plan-changed). A failed tofu apply exits 1 "
            "(infra-apply-failed); one that changed something first exits 1 whatever "
            "its code, with partially_applied, applied (address, action, id) and "
            "not_applied; plan again to continue. One command at a time per state "
            "(infra-state-locked). 0.25.3: an OpenStack Image is uploaded by Piceli "
            "(from the build host of an Image.from_build, else this machine), "
            "retried, its progress on stderr, verified and adopted (tofu import) "
            "before tofu apply; a failed upload deletes its image "
            "(infra-image-upload-failed, exit 1) and the log's path is printed.",
        ),
        "infra replace": _C(
            "Replace one server of a declared Infrastructure (0.26.2): plan it "
            "destroyed and created again (tofu plan -replace) on its persistent "
            "port with its current user data; apply with --approve.",
            reads=(
                "MODULE:ATTR (piceli.infra.Infrastructure)",
                "tofu binary",
                "credentials (provider token, state passphrase, the server's "
                "user data)",
            ),
            writes=(
                "resources at the provider (the server destroyed and created; "
                "any other declared change)",
                "encrypted OpenTofu state",
                "state directory records (inventory.json)",
            ),
            approval_required=True,
            long_running=True,
            contract="conforms",
            exit_codes=(0, 1, 2, 3),
            notes="Without --approve it plans (exit 3) with replaced and "
            "approve_command (piceli infra replace ... SERVER --approve HASH); the "
            "hash covers the replacement, so an infra apply approval never runs it. "
            "The server is destroyed first (its disk is gone), then created on its "
            "PrimaryIp port (same addresses) with its current UserData, which must "
            "be stored (infra-user-data-missing). An undeclared server is "
            "infra-server-unknown. Failures and partial applies as for infra apply.",
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
            "profiles of registered clusters are left in place. A failure after "
            "some deletions exits 1 with partially_applied, as for apply.",
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
            "(kubeconfig over SSH or --kubeconfig; a credential profile); for a "
            "server without cluster=, pin its SSH host keys only.",
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
            "Store a provider API credential (or a state backend password) as a "
            "local credential, part by part.",
            reads=(
                "stdin (--prompt)",
                "the provider's variables (--from-env)",
                "--infra MODULE:ATTR (which provider uses NAME)",
            ),
            writes=("$PICELI_CREDENTIALS_DIR/NAME.json (mode 0600)",),
            contract="conforms",
            exit_codes=(0, 2),
            notes="0.25.0: a two-part credential (Porkbun API key pair, OpenStack "
            "application credential) is asked for part by part, each checked before "
            "anything is stored (infra-credential-part-invalid names the part), and "
            "stored by part; piped, one part per line. The provider is --provider, "
            "else --infra's provider using NAME, else the one stored before, else "
            "the name; otherwise one token. --from-env reads the provider's standard "
            "variables. An argument, or a token variable without --from-env, is "
            "refused (infra-credential-refused) without echoing it. Replaces a stored "
            "credential. Prints the name and the parts' names, never a value.",
        ),
        "secrets nix-cache": _C(
            "Generate or import a Nix binary cache's signing key as a local "
            "credential, or print its public key.",
            reads=("stdin (--prompt)", "$PICELI_CREDENTIALS_DIR/NAME.json"),
            writes=(
                "$PICELI_CREDENTIALS_DIR/NAME.json (mode 0600; --generate, --prompt)",
            ),
            contract="conforms",
            exit_codes=(0, 2),
            notes="0.24.0. Prints the public key (NAME:base64), never the secret "
            "key. Without --generate or --prompt it only reads. Refuses to "
            "replace a key without --replace (infra-credential-exists): hosts "
            "that trust the old key refuse what a new one signs.",
        ),
        "secrets age": _C(
            "Generate or import a host's age identity (for sops-nix) as a local "
            "credential, or print its public key.",
            reads=("stdin (--prompt)", "$PICELI_CREDENTIALS_DIR/NAME.json"),
            writes=(
                "$PICELI_CREDENTIALS_DIR/NAME.json (mode 0600; --generate, --prompt)",
            ),
            contract="conforms",
            exit_codes=(0, 2),
            notes="0.24.0. Prints the public key (age1...) for .sops.yaml, never "
            "the identity. Without --generate or --prompt it only reads. Refuses "
            "to replace an identity without --replace (infra-credential-exists).",
        ),
        "secrets s3": _C(
            "Store a bucket's S3 access keys (access key id and secret) as a "
            "local credential.",
            reads=(
                "stdin (--prompt)",
                "AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY (--from-env)",
            ),
            writes=("$PICELI_CREDENTIALS_DIR/NAME.json (mode 0600)",),
            contract="conforms",
            exit_codes=(0, 2),
            notes="0.24.0. Two lines on stdin (the key id echoed, the secret not; "
            "or piped); since 0.25.0 each part is checked before anything is stored "
            "(infra-credential-part-invalid names the part) and they are stored by "
            "part, and --from-env reads AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY. "
            "An argument is refused (infra-credential-refused) without "
            "echoing it. Prints the name and the parts' names, never a value.",
        ),
        "secrets git-token": _C(
            "Store a Git token a build host fetches private flake inputs with "
            "as a local credential.",
            reads=("stdin (--prompt)",),
            writes=("$PICELI_CREDENTIALS_DIR/NAME.json (mode 0600)",),
            contract="conforms",
            exit_codes=(0, 2),
            notes="0.26.0. The token, its host (--host; it is used for no other) "
            "and a user name (--username, default git). A build host fetches the "
            "private inputs of a NixRelease or `dev nix build` with it before the "
            "build, never inside it. Prints the name and the host, never the "
            "token; an argument is refused (infra-credential-refused) without "
            "echoing it. It reaches another build host with `piceli secrets "
            "backup --only NAME` and `piceli secrets restore`.",
        ),
        "secrets git-ssh-key": _C(
            "Store a read-only deploy key a build host fetches private git+ssh "
            "flake inputs with as a local credential.",
            reads=("--file PATH", "--known-hosts PATH"),
            writes=("$PICELI_CREDENTIALS_DIR/NAME.json (mode 0600)",),
            contract="conforms",
            exit_codes=(0, 2),
            notes="0.26.0. The key, its host (--host; used for no other) and, "
            "optionally, that host's known_hosts lines. Used before the build by "
            "the build host's fetch step, never inside the build. Prints the "
            "name and the host, never the key.",
        ),
        "secrets signing-key": _C(
            "Generate or import a release signing key (ssh-ed25519) as a local "
            "credential, or print its public key.",
            reads=("--file PATH", "$PICELI_CREDENTIALS_DIR/NAME.json"),
            writes=(
                "$PICELI_CREDENTIALS_DIR/NAME.json (mode 0600; --generate, --file)",
            ),
            contract="conforms",
            exit_codes=(0, 2),
            notes="0.32.0. Prints the public key and its allowed_signers line, "
            "never the private key. --file takes an ed25519 key without a "
            "passphrase (a key with one stays in ssh-agent: sign-file --agent). "
            "Without --generate or --file it only reads. Refuses to replace a "
            "key without --replace (infra-credential-exists).",
        ),
        "sign-file": _C(
            "Write FILE.sig for each file, as `ssh-keygen -Y sign -n file` would.",
            contract="conforms",
            reads=(
                "the files",
                "$PICELI_CREDENTIALS_DIR/NAME.json (--key)",
                "SSH agent (--agent)",
            ),
            writes=("FILE.sig next to each file (replaced)",),
            safe_to_retry=True,
            exit_codes=(0, 2),
            notes="0.32.0. The key is a stored signing-key (--key NAME) or one an "
            "SSH agent holds (--agent KEY.pub). Verify with `ssh-keygen -Y "
            "verify -f allowed_signers -I PRINCIPAL -n file -s FILE.sig < FILE`. "
            "Never prints the private key.",
        ),
        "secrets user-data": _C(
            "Store a server's user data (a cloud-config, a script or a bootstrap "
            "key) as a local credential.",
            reads=("stdin (--prompt)", "PATH (--file)"),
            writes=("$PICELI_CREDENTIALS_DIR/NAME.json (mode 0600)",),
            contract="conforms",
            exit_codes=(0, 2),
            notes="0.24.0. Prints the name, size and a short digest (the one the "
            "plan shows), never the value; an argument is refused "
            "(infra-credential-refused) without echoing it. Used only by a plan "
            "that creates the server (Server(user_data=UserData(NAME))).",
        ),
        "secrets delete": _C(
            "Delete a stored local credential (a spent bootstrap key, an old token).",
            reads=("$PICELI_CREDENTIALS_DIR/NAME.json (its kind, never the value)",),
            writes=("$PICELI_CREDENTIALS_DIR/NAME.json (removed)",),
            approval_required=True,
            contract="conforms",
            exit_codes=(0, 2, 3),
            notes="0.24.0. Never prints the value. Without --yes it asks for the "
            "name on a terminal; otherwise it exits 3 (approval-required, with "
            "the command to run) and deletes nothing. --kind (provider, "
            "state-key, nix-cache, s3, age, user-data) refuses another kind "
            "(infra-credential-invalid); a missing one is infra-credential-missing.",
        ),
        "secrets backup": _C(
            "Write the whole local secret store and the profiles, encrypted with "
            "age to the chosen recipients, to one file.",
            reads=(
                "$PICELI_CREDENTIALS_DIR (every file)",
                "$PICELI_PROFILES_DIR",
                "--recipients-file",
            ),
            writes=(
                "--to PATH (age ciphertext, mode 0600)",
                "<credentials dir>.backup.json (recipients and the last backup's "
                "marker; no values)",
            ),
            contract="conforms",
            exit_codes=(0, 1, 2),
            notes="0.25.0. Runs the age command (PICELI_AGE, else age on PATH; "
            "secrets-age-missing otherwise): the tar is streamed into age, no "
            "plaintext touches the disk. Refuses an existing file without --force "
            "(secrets-backup-exists). Prints names, kinds, the archive's sha256 and "
            "the staleness, never a value. Exit 1 when age refuses "
            "(secrets-backup-failed). Needs two recipients: one is refused "
            "(secrets-backup-single-recipient) unless --single-recipient, which "
            "the manifest, the marker and the staleness record. --only NAME "
            "(repeatable) writes just those credentials for another machine (a "
            "build host): it needs --recipient (never the remembered ones), "
            "allows one, records only: true in the manifest, changes neither the "
            "marker nor the remembered recipients, and refuses an unknown name "
            "(secrets-backup-only-unknown).",
        ),
        "secrets status": _C(
            "List every secret a declaration needs: present, missing, incomplete "
            "or damaged, who creates it and the next command.",
            reads=(
                "MODULE[:ATTR] (Infrastructure, Bucket, NixCache, NixHost, "
                "NixRelease, DevBuilds, a composition's clusters)",
                "$PICELI_CREDENTIALS_DIR (kinds and parts; signing keys and age "
                "identities checked in memory)",
                "$PICELI_PROFILES_DIR",
                "the infrastructure's state records (which servers exist)",
                "<credentials dir>.backup.json (the last backup's marker)",
            ),
            contract="conforms",
            exit_codes=(0, 1, 2),
            notes="0.25.0. Read-only; safe to retry; never prints a value. For each "
            "secret: state (present, pending: a pair the next infra apply "
            "creates, or an S3 pair the owner makes by hand and has not stored "
            "(0.25.3; next: piceli secrets s3 NAME --prompt, detail: how to make "
            "it, e.g. openstack ec2 credentials create signed in as the project's "
            "user), missing, incomplete, damaged, or not-needed: the user data "
            "of a server that exists), created_by (infra-apply, "
            "piceli: --generate, owner: --prompt), next (the exact command), "
            "purpose and where else it must live; then the backup's state "
            "(empty, none, stale, single-recipient, current) and warnings (an S3 "
            "read pair the provider does not limit to reading, a spent user-data "
            "key). --json prints one piceli.secrets-status.v1 object. Exit 0: every "
            "secret stored and complete or pending (the backup does not count); "
            "a secret a NixRelease's build host needs gets, once present, the "
            "backup --only command that carries it there; 1: something "
            "missing, incomplete or damaged; 2: a module that does not load or "
            "declares nothing (secrets-nothing-declared).",
        ),
        "secrets setup": _C(
            "Walk through a declaration's missing secrets in order: provider "
            "credentials, generated keys, user data, S3 pairs, a backup.",
            reads=(
                "MODULE[:ATTR]",
                "stdin (each credential part; on a terminal, confirmations)",
                "$PICELI_CREDENTIALS_DIR",
            ),
            writes=(
                "$PICELI_CREDENTIALS_DIR/NAME.json (mode 0600) for each secret it "
                "stores or generates",
                "on a terminal, when chosen: the offline backup key, the backup and "
                "<credentials dir>.backup.json",
            ),
            contract="conforms",
            exit_codes=(0, 1, 2),
            notes="0.25.0. Provider credentials are asked for part by part (piped: "
            "one part per line, in the order status lists them; stdin at its end "
            "skips them). Keys Piceli generates (state key, Nix signing key, age "
            "identity) are generated after a confirmation on a terminal or with "
            "--yes, printing only public halves; a damaged key is replaced only "
            "after a confirmation on a terminal, never with --yes. User data is "
            "offered on a terminal only (default: later, right before infra "
            "apply); S3 pairs are left to infra apply. --json prints one object "
            "per step, then a summary. Never prints a value. Exit 0: everything "
            "stored but what is left for later on purpose (user data, the pairs "
            "infra apply makes); 1: something still missing, incomplete or "
            "damaged (a declined generation, a refused part, a profile); 2: a "
            "module that does not load or declares nothing.",
        ),
        "secrets backup-key": _C(
            "Create the offline age key for backups: write the private key to a "
            "file and print only its age1 recipient.",
            reads=("<credentials dir>.backup.json (--add)",),
            writes=(
                "--out PATH (the age identity, mode 0600)",
                "<credentials dir>.backup.json (--add: the remembered recipients)",
            ),
            contract="conforms",
            exit_codes=(0, 1, 2),
            notes="0.25.0. Runs age-keygen (PICELI_AGE_KEYGEN, next to PICELI_AGE, "
            "or on PATH; secrets-age-missing otherwise). Refuses a path inside the "
            "store or in a missing directory (secrets-backup-key-invalid) and an "
            "existing file without --force (secrets-backup-key-exists). Never "
            "prints the private key. Exit 1 when age-keygen fails "
            "(secrets-backup-key-failed).",
        ),
        "secrets restore": _C(
            "Restore the local secret store and the profiles from an encrypted backup.",
            reads=("PATH (age ciphertext)", "--identity", "the store (digests)"),
            writes=(
                "$PICELI_CREDENTIALS_DIR and $PICELI_PROFILES_DIR (files 0600, "
                "directories 0700; into a used store only with --approve)",
            ),
            approval_required=True,
            contract="conforms",
            exit_codes=(0, 1, 2, 3),
            notes="0.25.0. Decrypts in memory with age (identity file, plugin or "
            "passphrase) and refuses an unsafe archive "
            "(secrets-restore-archive-invalid). Into an empty store it restores "
            "directly; otherwise it prints what it would add, what is identical "
            "and what differs, with a digest (exit 3), and writes only with "
            "--approve DIGEST. Never prints a value. Exit 1 when age cannot "
            "decrypt (secrets-restore-decrypt-failed).",
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
            "starting, down. gitops-not-installed without a controller. 0.25.0: "
            "nix_releases lists each NixRelease (latest tag, state, plan digest "
            "and approve_command, stages with times, the published manifest's "
            "sha256, failure). A gated release shows pinned (input, commit, "
            "environment, verified_run, verified_at), or state rejected with its "
            "reason and refusal.",
        ),
        "gitops wait": _C(
            "Wait until an environment runs a commit: deployed, failed (the "
            "stage and cause), approval-required, superseded, stopped or "
            "timed-out.",
            reads=("kubeconfig or --state-dir",),
            cluster="reads",
            contract="conforms",
            exit_codes=(0, 1, 2, 3),
            notes="Read-only; safe to run and to retry. Polls the status "
            "ConfigMap every --interval seconds (default 10) for at most "
            "--timeout seconds (default 1800). Retries of the controller are "
            "waited through. Exit 0 deployed, 1 failed, superseded, stopped "
            "or timed out, 3 approval-required (the plan hash is in the "
            "output). COMMIT is a commit of any source of the environment.",
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
            "waiting for is dropped (gitops-approval-stale in gitops status). "
            "0.25.0: ENV may name a NixRelease waiting in approval-required; "
            "the hash is its plan digest, and approving it lets a build host "
            "build, sign and publish that tag.",
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
            "BRANCH@SHA); with --from ENV, pin the newest commit ENV ran with "
            "passed release checks in a NixRelease's flake.lock, commit it, tag "
            "the next version of the host's Tag pattern and push both (0.25.0).",
            reads=(
                "kubeconfig or --state-dir",
                "with --from: the composition module (--composition; the "
                "NixRelease and its gate), the controller's history (--state-dir, "
                "--kubeconfig/--context, or the composition cluster's credential "
                "profile), the flake's working copy (--repo) and its remote (git "
                "fetch), nix when on the PATH, else the input's repository (git "
                "clone into a temporary directory)",
            ),
            writes=(
                "--state-dir requests (local controller)",
                "with --from --approve: flake.lock, one commit and one annotated "
                "tag in the working copy; the branch and the tag on the remote, "
                "pushed atomically",
            ),
            cluster="writes",
            approval_required=True,
            contract="conforms",
            exit_codes=(0, 1, 2, 3),
            notes="Writes a request; the controller accepts only a commit it "
            "saw on that branch (gitops-promote-unknown otherwise), a named "
            "environment only when it follows Promote() "
            "(gitops-promote-not-allowed), and the environment then waits for "
            "gitops approve of its plan hash. 0.25.0, promote HOST --from ENV: "
            "without --approve it prints the plan (pinned, the lock change old "
            "rev -> new rev, the commit message, the tag, the remote and branch) "
            "with its digest and exits 3. A lock that already pins the commit "
            "exits 0 with state current: with a tag of the pattern at HEAD there "
            "is nothing to do; without one the plan only tags HEAD and --approve "
            "tags and pushes the tag. --approve recomputes the plan "
            "(promote-plan-changed when it differs), commits flake.lock alone, "
            "tags and pushes with git push --atomic (promote-push-failed exits "
            "1; the commit and tag stay local). Refuses a dirty working copy "
            "(promote-dirty), one behind, ahead of or diverged from its upstream "
            "(promote-not-in-sync), an environment without a passed run "
            "(promotion-not-verified) and an unreadable history "
            "(promotion-history-unreachable). 0.28.0, --from --wait: after "
            "the push (or when the tag is current), waits until the host runs "
            "the tag with its checks passed (exit 0), reports the first failure "
            "(a failed publish of the tag in the controller's status, or the "
            "host's failed or rolled-back attempt over SSH; exit 1) or the "
            "timeout (gitops-wait-timeout, exit 2); progress lines on stderr, "
            "the result under wait in the JSON.",
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
                "--cluster infra.py:CLUSTER (its credentials profile) or --state-dir",
            ),
            writes=(
                "<state_dir>/branches/<namespace> (journal, release state, secrets)",
                "--state-dir requests (local controller)",
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
            "policy. --wait refuses with env-budget-full instead of stopping. "
            "0.28.0: with --cluster infra.py:CLUSTER (or --state-dir) it asks a "
            "composition's GitOps controller instead: a preview request in the "
            "ConfigMap piceli-gitops-requests, served within seconds (the "
            "controller wakes for it), taking the images a train offered for "
            "the branch's head; a branch outside the rule's patterns gets one "
            "on request. --wait then waits until the preview runs its head "
            "with its checks passed and prints its URL "
            "(Environment.per_branch(url=...)) once (exit 0), the failure or "
            "a rejected request (exit 1), or the timeout (gitops-wait-timeout, "
            "exit 2). The --pipeline options are refused there "
            "(gitops-request-invalid).",
        ),
        "env down": _C(
            "Delete a branch environment: its claims, namespace and volumes "
            "(approval by env_hash; never main's).",
            reads=(
                "pipeline module (--pipeline or $PICELI_PIPELINE)",
                "kubeconfig",
                "--cluster infra.py:CLUSTER (its credentials profile) or --state-dir",
            ),
            writes=(
                "<state_dir>/branches/<namespace> (removed)",
                "--state-dir requests (local controller)",
            ),
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
            "absent (exit 0). 0.28.0: with --cluster (or --state-dir) it asks "
            "the GitOps controller to tear the branch's preview down; it stays "
            "down until the branch's next push. --wait waits until it is "
            "removed (exit 0), rejected (1) or timed out (2).",
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
