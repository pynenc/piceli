"""A set of image copies that survives a flaky network and a killed client (0.83.0).

``piceli artifacts copy`` copies one image. A release often copies several, and
a copy that fails on a dropped connection used to fail the whole release.
This module adds three things around :class:`~piceli.artifacts.image_copy.Copier`
(which it does not change):

1. **A set** (:class:`CopyItem`, :class:`CopySetPlan`): a list of
   ``source -> target`` pairs. One plan digest covers the whole set, so one
   ``--approve`` approves exactly these copies and no others.
2. **A ledger** (:class:`LedgerStore`): one small record per item (state,
   attempts, reason, receipt digest, time), written after every item and
   after every failed attempt. A run with the same ledger name continues at
   the first item that is not ``done``. The ledger holds no credential and no
   receipt, at most :data:`MAX_ITEMS` records, so it stays small (a ConfigMap
   holds 1 MiB).
3. **A whole-copy retry** (:func:`run_copy_set`): a copy that failed for a
   transient reason (a dropped connection, HTTP 429, 502, 503 or 504) is
   tried again as a whole, with an exponential pause and jitter, up to
   ``attempts`` times. Everything already copied is skipped by the registry
   (content-addressed), so a retry sends only what is missing. A permanent
   reason (for example ``copy-tag-exists`` or ``registry-unauthorized``)
   stops at once.

The report (``piceli.copy-set.v1``) lists every item with its state. Importing
this module runs nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import tempfile
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from piceli.artifacts.delivery_inputs import DeliveryInputError
from piceli.artifacts.image_copy import (
    Copier,
    CopyError,
    CopyGrant,
    CopyPlan,
)
from piceli.artifacts.plan import canonical, digest
from piceli.artifacts.registry import (
    RETRY_STATUSES,
    RegistryCredentials,
    RegistryError,
    describe,
    docker_config_credentials,
)
from piceli.artifacts.scan import ScanPolicy

LEDGER_REVISION = "piceli.copy-ledger.v1"
REPORT_REVISION = "piceli.copy-set.v1"
SET_PLAN_REVISION = "piceli.copy-set-plan.v1"
#: Most items in one set (and so in one ledger).
MAX_ITEMS = 500
#: Retries kept in a report.
MAX_RETRIES = 100
#: Largest ``--set`` file.
MAX_SET_BYTES = 1024 * 1024
#: A failed copy with one of these reasons is tried again as a whole. Other
#: reasons (a refused tag, bad credentials, a digest mismatch) are permanent.
#: 0.96.0: the longest error text a failed item keeps.
MAX_DETAIL = 400
TRANSIENT_REASONS = frozenset({"registry-unreachable"})
#: Reasons that are transient only with one of :data:`~piceli.artifacts.registry.RETRY_STATUSES`.
STATUS_REASONS = frozenset({"registry-error", "upload-rejected"})
#: File name of the ledger in a ConfigMap (``piceli-copy-<name>``).
LEDGER_KEY = "ledger.json"
_NAME = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,60}[a-z0-9])?")


class CopySetError(ValueError):
    """A set, ledger or option is invalid. ``code`` is a registered error code."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


def validate_name(name: str) -> str:
    """A ledger name: lower case letters, digits, ``.`` and ``-`` (a DNS-safe name)."""
    if not isinstance(name, str) or _NAME.fullmatch(name) is None:
        raise CopySetError(
            "copy-invalid",
            "a ledger name is 1 to 62 characters of a-z, 0-9, '.' and '-'",
        )
    return name


def ledger_records(document: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The per-item records of a ledger document (``{}`` for a new or odd one)."""
    found = document.get("items")
    if not isinstance(found, dict):
        return {}
    return {key: dict(value) for key, value in found.items() if isinstance(value, dict)}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --------------------------------------------------------------------- items
@dataclass(frozen=True)
class CopyItem:
    """One copy: ``source`` by digest to ``target``, as ``artifacts copy`` takes
    them (``oci://host/repo@sha256:…``, ``oci://host/repo[:tag]``).

    The credentials are paths of a private credentials file or a Docker
    config; never the secrets themselves.
    """

    source: str
    target: str
    source_credentials: str | None = None
    target_credentials: str | None = None

    def __post_init__(self) -> None:
        for value in (self.source, self.target):
            if not isinstance(value, str) or not value or len(value) > 1024:
                raise CopySetError("copy-invalid", "a copy needs a source and a target")
        for value in (self.source_credentials, self.target_credentials):
            if value is not None and (not isinstance(value, str) or len(value) > 4096):
                raise CopySetError("copy-invalid", "invalid credentials path")

    @property
    def key(self) -> str:
        """A stable id of ``(source, target)``: the ledger key."""
        text = canonical([self.source, self.target])
        return hashlib.sha256(text).hexdigest()[:20]

    def to_dict(self) -> dict[str, str]:
        value = {"source": self.source, "target": self.target}
        if self.source_credentials is not None:
            value["source_credentials"] = self.source_credentials
        if self.target_credentials is not None:
            value["target_credentials"] = self.target_credentials
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CopyItem:
        if not isinstance(value, Mapping):
            raise CopySetError("copy-invalid", "a copy item is an object")
        unknown = set(value) - {
            "source",
            "target",
            "source_credentials",
            "target_credentials",
        }
        if unknown:
            raise CopySetError("copy-invalid", "unknown key in a copy item")
        return cls(
            value.get("source"),  # type: ignore[arg-type]
            value.get("target"),  # type: ignore[arg-type]
            value.get("source_credentials"),
            value.get("target_credentials"),
        )


@dataclass(frozen=True)
class CopyOptions:
    """What every copy of a set shares (part of each item's plan)."""

    referrers: bool = True
    move_tag: bool = False
    timeout: float = 1800.0
    source_ca_file: Path | None = None
    ca_file: Path | None = None
    #: 0.94.0: the scan of each source (``None``: the default policy).
    scan: ScanPolicy | None = None

    def to_dict(self) -> dict[str, Any]:
        """The part a cluster Job needs (CA files are local paths: not sent)."""
        return {
            "referrers": self.referrers,
            "move_tag": self.move_tag,
            "timeout": self.timeout,
            **({"scan": self.scan.to_dict()} if self.scan is not None else {}),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CopyOptions:
        try:
            return cls(
                referrers=bool(value.get("referrers", True)),
                move_tag=bool(value.get("move_tag", False)),
                timeout=float(value.get("timeout", 1800.0)),
                scan=ScanPolicy.from_table(value["scan"]) if "scan" in value else None,
            )
        except (TypeError, ValueError, AttributeError):
            raise CopySetError("copy-invalid", "invalid copy options") from None


def load_set_file(path: Path) -> list[CopyItem]:
    """The items of a ``--set`` file.

    JSON (a list, or ``{"copy": [...]}``) or TOML (``[[copy]]`` tables); each
    entry is ``{from, to}`` with optional ``from_credentials`` and
    ``credentials`` (paths, relative to the file).

    :raises CopySetError: ``copy-invalid``.
    """
    try:
        if path.stat().st_size > MAX_SET_BYTES:
            raise CopySetError("copy-invalid", "the --set file is too large")
        raw = path.read_bytes()
        document = (
            tomllib.loads(raw.decode())
            if path.suffix.lower() == ".toml"
            else json.loads(raw)
        )
    except (OSError, ValueError, UnicodeDecodeError) as error:
        if isinstance(error, CopySetError):
            raise
        raise CopySetError("copy-invalid", "cannot read the --set file") from None
    entries = document.get("copy") if isinstance(document, dict) else document
    if not isinstance(entries, list) or not entries:
        raise CopySetError("copy-invalid", "the --set file lists no copies")
    base = path.absolute().parent
    items = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) - {
            "from",
            "to",
            "from_credentials",
            "credentials",
        }:
            raise CopySetError("copy-invalid", "each copy is {from, to}")

        def located(name: str, entry: dict[str, Any] = entry) -> str | None:
            value = entry.get(name)
            if value is None:
                return None
            if not isinstance(value, str):
                raise CopySetError("copy-invalid", "invalid credentials path")
            return str((base / value).absolute() if value else value)

        items.append(
            CopyItem(
                str(entry.get("from") or ""),
                str(entry.get("to") or ""),
                located("from_credentials"),
                located("credentials"),
            )
        )
    return check_items(items)


def check_items(items: Sequence[CopyItem]) -> list[CopyItem]:
    """The items, when there are 1 to :data:`MAX_ITEMS` and none repeats."""
    found = list(items)
    if not found or len(found) > MAX_ITEMS:
        raise CopySetError(
            "copy-invalid", f"a set holds 1 to {MAX_ITEMS} copies, not {len(found)}"
        )
    if len({item.key for item in found}) != len(found):
        raise CopySetError("copy-invalid", "a copy is listed twice")
    return found


# ---------------------------------------------------------------------- plan
@dataclass(frozen=True)
class CopySetPlan:
    """The plans of every item. Its digest is what ``--approve`` names.

    A set of one has the digest of its one :class:`CopyPlan`, so an approval
    of a single ``--from``/``--to`` copy is the same as before 0.83.0.
    """

    plans: tuple[CopyPlan, ...]

    @classmethod
    def parse(cls, items: Sequence[CopyItem], options: CopyOptions) -> CopySetPlan:
        """No network.

        :raises CopyError: ``copy-invalid`` from an item's plan.
        :raises CopySetError: ``copy-invalid`` (no items, a repeat).
        """
        plans = tuple(
            CopyPlan.parse(
                item.source,
                item.target,
                referrers=options.referrers,
                move_tag=options.move_tag,
                scan=options.scan,
            )
            for item in check_items(items)
        )
        return cls(plans)

    @property
    def digest(self) -> str:
        if len(self.plans) == 1:
            return self.plans[0].digest
        return digest(
            canonical(
                {
                    "revision": SET_PLAN_REVISION,
                    "plans": [plan.digest for plan in self.plans],
                }
            )
        )

    def preview(self) -> dict[str, Any]:
        if len(self.plans) == 1:
            return self.plans[0].preview()
        return {
            "revision": SET_PLAN_REVISION,
            "count": len(self.plans),
            "items": [plan.preview() for plan in self.plans],
            "plan_digest": self.digest,
        }


# -------------------------------------------------------------------- ledger
class LedgerStore(Protocol):
    """Where a ledger lives, by name. ``load`` gives ``{}`` for a new name."""

    def load(self, name: str) -> dict[str, Any]: ...

    def save(self, name: str, data: dict[str, Any]) -> None: ...


class MemoryLedger:
    """A ledger kept in memory: a run with no ``--ledger`` and a test seam."""

    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}

    def load(self, name: str) -> dict[str, Any]:
        return json.loads(json.dumps(self.documents.get(name) or {}))

    def save(self, name: str, data: dict[str, Any]) -> None:
        self.documents[name] = json.loads(json.dumps(data))


class FileLedger:
    """``<directory>/<name>.json``, written atomically with mode 0600."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _path(self, name: str) -> Path:
        return self.directory / f"{validate_name(name)}.json"

    def load(self, name: str) -> dict[str, Any]:
        try:
            value = json.loads(self._path(name).read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            raise CopySetError("copy-invalid", "cannot read the ledger file") from None
        return value if isinstance(value, dict) else {}

    def save(self, name: str, data: dict[str, Any]) -> None:
        path = self._path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
        try:
            with os.fdopen(handle, "w") as stream:
                json.dump(data, stream, sort_keys=True, indent=2)
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise


class ConfigMapLedger:
    """The ledger as key ``ledger.json`` of ConfigMap ``piceli-copy-<name>``.

    ``kube`` is a Kubernetes ``ApiClient`` for an explicit kubeconfig, as for
    :class:`piceli.gitops.state.ConfigMapChannel`, which does the reads and
    the conflict-safe writes. The ConfigMap is not deleted by Piceli: it is
    the record a rerun continues from.
    """

    def __init__(self, kube: Any, namespace: str) -> None:
        from piceli.gitops.state import ConfigMapChannel

        self.channel = ConfigMapChannel(kube, namespace)

    @staticmethod
    def configmap(name: str) -> str:
        return f"piceli-copy-{validate_name(name)}"

    def load(self, name: str) -> dict[str, Any]:
        current = self.channel._read(self.configmap(name)) or {}
        text = (current.get("data") or {}).get(LEDGER_KEY)
        try:
            value = json.loads(text) if text else {}
        except ValueError:
            raise CopySetError("copy-invalid", "cannot read the ledger") from None
        return value if isinstance(value, dict) else {}

    def save(self, name: str, data: dict[str, Any]) -> None:
        self.put(name, {LEDGER_KEY: json.dumps(data, sort_keys=True)})

    def put(self, name: str, keys: Mapping[str, str]) -> None:
        """Set ``keys`` of the ConfigMap, leaving its other keys."""
        self.channel._update(self.configmap(name), lambda data: {**data, **keys})


# -------------------------------------------------------------------- report
@dataclass
class ItemResult:
    """How one item ended in a run."""

    key: str
    source: str
    target: str
    state: str  # done | failed | pending
    attempts: int = 0
    reason: str | None = None
    receipt_digest: str | None = None
    skipped: bool = False
    receipt: dict[str, Any] | None = field(default=None, repr=False)
    #: 0.96.0: what the failed check compared (the error's text), if known.
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "key": self.key,
            "source": self.source,
            "target": self.target,
            "state": self.state,
            "attempts": self.attempts,
            "reason": self.reason,
            "receipt_digest": self.receipt_digest,
            "skipped": self.skipped,
        }
        if self.receipt is not None:
            value["receipt"] = self.receipt
        if self.detail:
            value["detail"] = self.detail
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ItemResult:
        return cls(
            key=str(value["key"]),
            source=str(value["source"]),
            target=str(value["target"]),
            state=str(value["state"]),
            attempts=int(value.get("attempts") or 0),
            reason=value.get("reason"),
            receipt_digest=value.get("receipt_digest"),
            skipped=bool(value.get("skipped")),
            receipt=value.get("receipt"),
            detail=value.get("detail"),
        )


@dataclass
class CopyReport:
    """The result of a set (``piceli.copy-set.v1``)."""

    ledger: str
    items: list[ItemResult] = field(default_factory=list)
    retries: list[dict[str, Any]] = field(default_factory=list)
    job: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return bool(self.items) and all(item.state == "done" for item in self.items)

    @property
    def failed(self) -> str | None:
        """The reason of the first failed item, else ``None``."""
        for item in self.items:
            if item.state == "failed":
                return item.reason or "copy-failed"
        return None if self.ok else "copy-failed"

    @property
    def failed_detail(self) -> str | None:
        """0.96.0: the text of the first failed item's error, else ``None``."""
        for item in self.items:
            if item.state == "failed":
                return item.detail
        return None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "revision": REPORT_REVISION,
            "ledger": self.ledger,
            "ok": self.ok,
            "failed": self.failed,
            "items": [item.to_dict() for item in self.items],
            "retries": self.retries[-MAX_RETRIES:],
        }
        if self.job is not None:
            value["job"] = self.job
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CopyReport:
        if value.get("revision") != REPORT_REVISION:
            raise CopySetError("copy-invalid", "not a copy-set report")
        return cls(
            ledger=str(value["ledger"]),
            items=[ItemResult.from_dict(item) for item in value["items"]],
            retries=list(value.get("retries") or []),
            job=value.get("job"),
        )


# ------------------------------------------------------------------- running
def is_transient(receipt: Mapping[str, Any]) -> bool:
    """Whether a failed copy receipt names a reason trying again may cure."""
    reason = receipt.get("reason")
    if reason in TRANSIENT_REASONS:
        return True
    error = receipt.get("error")
    status = error.get("status") if isinstance(error, Mapping) else None
    return reason in STATUS_REASONS and status in RETRY_STATUSES


def pause(
    attempt: int,
    base: float,
    cap: float,
    jitter: Callable[[float, float], float] = random.uniform,
) -> float:
    """Seconds before retry ``attempt`` (1 for the first): ``min(cap, base *
    2**(attempt-1))``, half of it fixed and half random."""
    delay = min(cap, base * 2 ** (attempt - 1))
    return delay / 2 + jitter(0.0, delay / 2)


def load_credentials(path: str | None, registry: str) -> RegistryCredentials | None:
    """A private credentials file, or a Docker ``config.json`` (told apart by
    its keys); ``None`` for no path."""
    if path is None:
        return None
    target = Path(path).absolute()
    try:
        head = json.loads(target.read_bytes()[:65537])
    except (OSError, ValueError):
        raise DeliveryInputError("invalid-credentials-file") from None
    if isinstance(head, dict) and {"auths", "credHelpers", "credsStore"} & set(head):
        return docker_config_credentials(target, registry)
    try:
        return RegistryCredentials.load(target)
    except DeliveryInputError:
        raise
    except (OSError, ValueError):
        raise DeliveryInputError("invalid-credentials-file") from None


def prepare_copies(
    items: Sequence[CopyItem],
    options: CopyOptions,
    *,
    source_credentials: RegistryCredentials | None = None,
    target_credentials: RegistryCredentials | None = None,
    **seams: Any,
) -> Callable[[CopyItem], dict[str, Any]]:
    """The default ``copy`` of :func:`run_copy_set`: every plan and credential
    is read now (nothing is sent), so an invalid input fails before a copy
    starts. ``source_credentials`` and ``target_credentials`` apply to every
    item (a cluster Job reads them from its environment) instead of each
    item's files. ``seams`` go to :class:`Copier` (``client_factory``).

    :raises CopyError: ``copy-invalid``.
    :raises DeliveryInputError: an unreadable credentials file.
    """
    prepared: dict[str, tuple[CopyPlan, Copier]] = {}
    for item in items:
        plan = CopyPlan.parse(
            item.source,
            item.target,
            referrers=options.referrers,
            move_tag=options.move_tag,
            scan=options.scan,
        )
        copier = Copier(
            source_credentials=source_credentials
            or load_credentials(
                item.source_credentials, plan.source.repository.registry
            ),
            credentials=target_credentials
            or load_credentials(item.target_credentials, plan.target.registry),
            source_ca_file=options.source_ca_file,
            ca_file=options.ca_file,
            timeout=options.timeout,
            **seams,
        )
        prepared[item.key] = (plan, copier)

    def copy(item: CopyItem) -> dict[str, Any]:
        plan, copier = prepared[item.key]
        return copier.copy(plan, CopyGrant(plan.digest, time.time() + options.timeout))

    return copy


#: The receipt keys a ledger keeps of a done copy (0.87.0): enough to say what
#: was copied where (no blob or referrer lists, so 500 items fit a ConfigMap).
LEDGER_RECEIPT_KEYS = (
    "source", "target", "digest", "media_type", "platforms", "pull_ref",
    "previous", "result", "state", "started_at", "finished_at", "seconds",
)  # fmt: skip


def ledger_receipt(receipt: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The part of a done copy's receipt its ledger record keeps (0.87.0)."""
    if not isinstance(receipt, Mapping):
        return None
    kept = {key: receipt[key] for key in LEDGER_RECEIPT_KEYS if key in receipt}
    referrers = receipt.get("referrers")
    if isinstance(referrers, list):
        # Each referrer's short text fields (subject, digest, result).
        kept["referrers"] = [
            {str(k): v for k, v in item.items() if isinstance(v, str) and len(v) <= 200}
            for item in referrers[:16]
            if isinstance(item, Mapping)
        ]
    return kept


def resumed_receipt(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """0.87.0: a done item's receipt from its ledger record, marked
    ``from_ledger`` (a copy before 0.87.0 kept none: ``None``)."""
    kept = record.get("receipt")
    return {**kept, "from_ledger": True} if isinstance(kept, Mapping) else None


def _record(
    state: str,
    attempts: int,
    reason: str | None,
    receipt_digest: str | None,
    receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    found = {
        "state": state,
        "attempts": attempts,
        "reason": reason,
        "receipt_digest": receipt_digest,
        "at": _now(),
    }
    kept = ledger_receipt(receipt)
    if kept is not None:
        found["receipt"] = kept
    return found


def run_copy_set(
    items: Sequence[CopyItem],
    *,
    ledger: str,
    store: LedgerStore,
    attempts: int = 5,
    backoff_base: float = 2.0,
    backoff_cap: float = 60.0,
    sleep: Callable[[float], None] = time.sleep,
    copy: Callable[[CopyItem], dict[str, Any]] | None = None,
    options: CopyOptions | None = None,
    jitter: Callable[[float, float], float] = random.uniform,
    say: Callable[[str], None] = lambda _line: None,
) -> CopyReport:
    """Copy ``items`` in order, resuming from ``ledger``.

    An item the ledger names ``done`` is skipped. Otherwise it is copied; a
    failure with a transient reason (:func:`is_transient`) is tried again as a
    whole after :func:`pause` seconds, up to ``attempts`` times, and any other
    reason stops the set at once (the later items stay ``pending``). The
    ledger is saved after every failed attempt and after every item.

    :param copy: ``item -> copy receipt``; default :func:`prepare_copies`
        (the real copy). A test seam.
    :raises CopySetError: ``copy-invalid`` (``attempts`` under 1, a bad name,
        no items, a repeated item).
    """
    validate_name(ledger)
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
        raise CopySetError("copy-invalid", "--attempts is at least 1")
    found = check_items(items)
    run = copy or prepare_copies(found, options or CopyOptions())
    saved = ledger_records(store.load(ledger))
    # Only this set's items stay in the ledger: it never grows past MAX_ITEMS.
    records = {item.key: saved[item.key] for item in found if item.key in saved}

    def persist() -> None:
        store.save(
            ledger,
            {"revision": LEDGER_REVISION, "updated_at": _now(), "items": records},
        )

    report = CopyReport(ledger)
    stopped = False
    for item in found:
        result = ItemResult(item.key, item.source, item.target, "pending")
        report.items.append(result)
        record = records.get(item.key) or {}
        if record.get("state") == "done":
            result.state, result.skipped = "done", True
            result.attempts = int(record.get("attempts") or 0)
            result.receipt_digest = record.get("receipt_digest")
            # 0.87.0: a resumed copy reports what the done one copied.
            result.receipt = resumed_receipt(record)
            continue
        if stopped:
            continue
        total = int(record.get("attempts") or 0)
        for number in range(1, attempts + 1):
            total += 1
            receipt, transient = _try(run, item)
            result.attempts = total
            if receipt.get("state") == "copied":
                result.state, result.reason = "done", None
                result.receipt = receipt
                result.receipt_digest = digest(canonical(receipt))
                records[item.key] = _record(
                    "done", total, None, result.receipt_digest, receipt
                )
                persist()
                say(f"copy {item.key}: done after {total} attempt(s)")
                break
            reason = str(receipt.get("reason") or "copy-failed")
            result.state, result.reason, result.receipt = "failed", reason, receipt
            result.detail = _detail(receipt)
            records[item.key] = _record("failed", total, reason, None)
            if result.detail:
                records[item.key]["detail"] = result.detail
            persist()
            if not (transient or is_transient(receipt)) or number == attempts:
                stopped = True
                said = f": {result.detail}" if result.detail else ""
                say(f"copy {item.key}: failed [{reason}]{said}")
                break
            wait = pause(number, backoff_base, backoff_cap, jitter)
            report.retries.append(
                {
                    "key": item.key,
                    "attempt": number,
                    "reason": reason,
                    "pause": round(wait, 3),
                }
            )
            say(f"copy {item.key}: [{reason}], again in {wait:.0f}s")
            sleep(wait)
    del report.retries[:-MAX_RETRIES]
    persist()
    return report


def _try(
    run: Callable[[CopyItem], dict[str, Any]], item: CopyItem
) -> tuple[dict[str, Any], bool]:
    """One copy of ``item``: its receipt, and whether a failure is transient.
    A raised refusal becomes a failed receipt with its code as ``reason``."""
    try:
        return run(item), False
    except RegistryError as error:
        return _failed(error.reason, error), error.transient
    except (CopyError, DeliveryInputError, CopySetError) as error:
        return _failed(error.code, error), False


def _failed(reason: str, error: BaseException) -> dict[str, Any]:
    """A failed receipt; 0.96.0: with the error's text when it says more."""
    found: dict[str, Any] = {"state": "failed", "reason": reason}
    text = str(error)
    if text and text != reason:
        found["detail"] = text[:MAX_DETAIL]
    return found


def _detail(receipt: Mapping[str, Any]) -> str | None:
    """The text a failed receipt keeps of its error (0.96.0), else ``None``."""
    for key in ("detail", "message"):
        found = receipt.get(key)
        if isinstance(found, str) and found:
            return found[:MAX_DETAIL]
    error = receipt.get("error")
    found = describe(error) if isinstance(error, Mapping) else ""
    return found[:MAX_DETAIL] or None


__all__ = [
    "LEDGER_KEY",
    "LEDGER_REVISION",
    "MAX_ITEMS",
    "REPORT_REVISION",
    "ConfigMapLedger",
    "CopyItem",
    "CopyOptions",
    "CopyReport",
    "CopySetError",
    "CopySetPlan",
    "FileLedger",
    "ItemResult",
    "LedgerStore",
    "MemoryLedger",
    "is_transient",
    "ledger_records",
    "load_credentials",
    "load_set_file",
    "pause",
    "prepare_copies",
    "run_copy_set",
    "validate_name",
]
