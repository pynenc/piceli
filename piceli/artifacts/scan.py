"""Scan images for known vulnerabilities before they are pushed (0.94.0).

Every image Piceli builds or mirrors is scanned with `Grype
<https://github.com/anchore/grype>`_ (Apache 2.0), pinned here by version and
by the sha256 of each release tarball:

* a host build scans each image's OCI archive (``oci-archive:<path>``, one
  per platform) after it is packaged, before it is pushed or recorded
  (:func:`scan_build`, called by :meth:`HostBuildSpec.run`, which the local
  ``build-spec run`` and the cluster builder share);
* a mirror scans the copy in the target registry, before the mirror is
  recorded as ready; ``piceli artifacts copy`` scans the source digest before
  it copies anything (:func:`scan_registry`, ``registry:<ref>@<digest>``, one
  scan per platform manifest of an index).

**Policy.** A finding of severity *Critical* whose fix state is ``fixed``
(the advisory names fixed versions) fails with ``image-vulnerable``, unless a
waiver names it (its id or a related id) and has not expired. Everything
else is counted by severity. A waiver is ``{id, reason, until}``; after
``until`` (a ``YYYY-MM-DD`` date, inclusive) it no longer applies and the
error says it expired.

The policy is a ``[scan]`` table: in ``host-build.toml`` for a build, in a
file of its own for ``artifacts copy --scan-policy FILE`` and for mirrors
(``PICELI_SCAN_POLICY=FILE``)::

    [scan]
    enabled = true
    ignore = [
      { id = "CVE-2026-0001", reason = "the parser is never called", until = "2027-01-31" },
    ]

Turning scanning off is explicit (``enabled = false``, ``--no-scan``) and is
recorded as ``"scan": "off"`` in the receipt (and a build's provenance).

**Install.** Grype is downloaded once per cache into
``<cache>/toolchains/grype/<version>-<os>-<arch>`` under a file lock with a
done marker; a tarball whose sha256 is not the pinned one is refused
(``image-scanner-checksum``). Its vulnerability database lives in
``<cache>/grype-db`` (``GRYPE_DB_CACHE_DIR``) and Grype updates it; a
database that cannot be fetched (or is stale) fails the scan
(``image-scan-failed``): scanning fails closed, it never skips.

``PICELI_SCANNER`` names an executable used instead of the pinned Grype (a
machine with its own copy; the test suite's stand-in). Registry credentials
reach Grype only through its environment (``GRYPE_REGISTRY_AUTH_*``); they
are never printed, and its output is redacted of them.

Importing this module runs nothing.
"""

from __future__ import annotations

import datetime as _dt
import fcntl
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from piceli.artifacts.build_spec import BuildSpecError, platform_slug

GRYPE_VERSION = "0.122.1"
#: The sha256 of each release tarball (the release's checksums file).
GRYPE_SHA256: Mapping[tuple[str, str], str] = {
    ("darwin", "amd64"): (
        "8063aef11b52e6da342068aa6980e92f40b28849eff990c8375a0a8e6b6dfbc5"
    ),
    ("darwin", "arm64"): (
        "8bbb3c01695d1e954875cf9f7403c5d3cdb652caf763867955fad520e3cd6c00"
    ),
    ("linux", "amd64"): (
        "f1ba7e2917ee1395618de20a98da0aa7c0fb1788927797651047c50502a37538"
    ),
    ("linux", "arm64"): (
        "819e5be3d9516432fedb7b6b56df7306c3a16607606dfc05826eefbb00e471da"
    ),
}
GRYPE_URL = (
    "https://github.com/anchore/grype/releases/download/"
    "v{version}/grype_{version}_{system}_{arch}.tar.gz"
)
#: An executable used instead of the pinned Grype.
SCANNER_ENV = "PICELI_SCANNER"
#: A policy file (a ``[scan]`` table) for mirrors.
POLICY_ENV = "PICELI_SCAN_POLICY"
#: Where mirrors and copies keep Grype and its database.
CACHE_ENV = "PICELI_SCAN_CACHE"
REPORT_SCHEMA = "piceli.image-scan.v1"
SEVERITIES = ("critical", "high", "medium", "low", "negligible", "unknown")
#: What fails a scan: a Critical finding with a fixed version.
FAIL_ON = "critical-fixed"
MAX_WAIVERS = 256
MAX_REPORT_BYTES = 256 * 1024 * 1024
MAX_SCANNER_BYTES = 256 * 1024 * 1024
#: Findings kept in a ``.scan.json`` (Critical and High) and in an error.
MAX_LISTED = 500
SCAN_SECONDS = 1800.0
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_TEXT = 256


class ScanError(BuildSpecError):
    """A scan refused an image or could not run. ``code`` is registered:
    ``image-vulnerable``, ``image-scan-failed``, ``image-scanner-checksum``,
    ``scan-policy-invalid``. ``details`` (``image-vulnerable``) lists each
    image, its digest and its findings; never a path or a credential."""

    def __init__(
        self, code: str, message: str, details: Mapping[str, Any] | None = None
    ) -> None:
        super().__init__(code, message)
        self.details: dict[str, Any] = dict(details or {})


def _policy_error(message: str) -> ScanError:
    return ScanError("scan-policy-invalid", message)


# ------------------------------------------------------------------ policy


@dataclass(frozen=True)
class Waiver:
    """A known finding accepted until a date (inclusive)."""

    id: str
    reason: str
    until: str

    def expired(self, today: _dt.date) -> bool:
        return today > _dt.date.fromisoformat(self.until)

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "reason": self.reason, "until": self.until}


@dataclass(frozen=True)
class ScanPolicy:
    """``[scan]``: whether to scan, and the waivers."""

    enabled: bool = True
    waivers: tuple[Waiver, ...] = ()

    @property
    def default(self) -> bool:
        return self.enabled and not self.waivers

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "ignore": [item.to_dict() for item in self.waivers],
        }

    @classmethod
    def off(cls) -> ScanPolicy:
        return cls(enabled=False)

    @classmethod
    def from_table(cls, value: Any, where: str = "[scan]") -> ScanPolicy:
        """Parse a ``[scan]`` table.

        :raises ScanError: ``scan-policy-invalid``.
        """
        if not isinstance(value, Mapping) or not set(value) <= {"enabled", "ignore"}:
            raise _policy_error(f"{where}: expected fields ['enabled', 'ignore']")
        enabled = value.get("enabled", True)
        if not isinstance(enabled, bool):
            raise _policy_error(f"{where}.enabled must be true or false")
        items = value.get("ignore", [])
        if not isinstance(items, list) or len(items) > MAX_WAIVERS:
            raise _policy_error(f"{where}.ignore must list at most {MAX_WAIVERS}")
        waivers: list[Waiver] = []
        for item in items:
            if not isinstance(item, Mapping) or set(item) != {"id", "reason", "until"}:
                raise _policy_error(
                    f"{where}.ignore: each entry is {{ id, reason, until }}"
                )
            ident, reason, until = item["id"], item["reason"], item["until"]
            if not isinstance(ident, str) or not _ID.fullmatch(ident):
                raise _policy_error(f"{where}.ignore: invalid id")
            if (
                not isinstance(reason, str)
                or not reason.strip()
                or len(reason) > 500
                or not reason.isprintable()
            ):
                raise _policy_error(f"{where}.ignore {ident}: give a short reason")
            if isinstance(until, _dt.date):  # a TOML date literal
                until = until.isoformat()
            if not isinstance(until, str) or not _DATE.fullmatch(until):
                raise _policy_error(f"{where}.ignore {ident}: until is YYYY-MM-DD")
            try:
                _dt.date.fromisoformat(until)
            except ValueError:
                raise _policy_error(
                    f"{where}.ignore {ident}: until is not a date"
                ) from None
            if any(other.id == ident for other in waivers):
                raise _policy_error(f"{where}.ignore: {ident} is listed twice")
            waivers.append(Waiver(ident, reason, until))
        return cls(enabled, tuple(sorted(waivers, key=lambda item: item.id)))

    @classmethod
    def load(cls, path: Path) -> ScanPolicy:
        """A policy file: a TOML document with a ``[scan]`` table.

        :raises ScanError: ``scan-policy-invalid``.
        """
        import tomllib

        try:
            document = tomllib.loads(path.read_text())
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            raise _policy_error("cannot read the scan policy file") from None
        if set(document) != {"scan"}:
            raise _policy_error("a scan policy file holds one [scan] table")
        return cls.from_table(document["scan"])

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> ScanPolicy:
        """The policy ``PICELI_SCAN_POLICY`` names (mirrors), else the default."""
        value = (environ if environ is not None else os.environ).get(POLICY_ENV)
        return cls.load(Path(value)) if value else cls()


def parse_policy(value: Any, where: str = "[scan]") -> ScanPolicy:
    """The ``[scan]`` table of a ``host-build.toml``."""
    return ScanPolicy.from_table(value, where)


def _today() -> _dt.date:
    return _dt.datetime.now(_dt.UTC).date()


# ------------------------------------------------------------------ report


def _text(value: Any) -> str:
    text = value if isinstance(value, str) else ("" if value is None else str(value))
    text = "".join(char for char in text if char.isprintable())
    return text[:_TEXT]


@dataclass(frozen=True)
class Finding:
    """One vulnerability of one package."""

    id: str
    severity: str
    package: str
    installed: str
    fixed_in: tuple[str, ...] = ()
    fix_state: str = "unknown"
    related: tuple[str, ...] = ()

    @property
    def fixable(self) -> bool:
        return self.fix_state == "fixed" and bool(self.fixed_in)

    @property
    def blocking(self) -> bool:
        """What the policy fails on (before waivers)."""
        return self.severity == "critical" and self.fixable

    def public(
        self, waived: bool = False, expired: str | None = None
    ) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "id": self.id,
            "severity": self.severity,
            "package": self.package,
            "installed": self.installed,
            "fixed_in": list(self.fixed_in),
            "waived": waived,
        }
        if expired is not None:
            entry["waiver_expired"] = expired
        return entry


@dataclass(frozen=True)
class Report:
    """What one scan found."""

    findings: tuple[Finding, ...]
    version: str | None = None
    db_built: str | None = None

    def counts(self) -> dict[str, int]:
        found = dict.fromkeys(SEVERITIES, 0)
        for item in self.findings:
            found[item.severity] += 1
        return found

    @classmethod
    def parse(cls, document: Any) -> Report:
        """A Grype JSON report (``-o json``); duplicate matches of one
        vulnerability in one package count once.

        :raises ScanError: ``image-scan-failed`` for a document that is not one.
        """
        if not isinstance(document, Mapping) or not isinstance(
            document.get("matches", []), list
        ):
            raise ScanError("image-scan-failed", "the scanner's report is invalid")
        found: dict[tuple[str, str, str], Finding] = {}
        for match in document.get("matches") or []:
            if not isinstance(match, Mapping):
                continue
            vulnerability = match.get("vulnerability") or {}
            artifact = match.get("artifact") or {}
            if not isinstance(vulnerability, Mapping) or not isinstance(
                artifact, Mapping
            ):
                continue
            ident = _text(vulnerability.get("id"))
            if not ident:
                continue
            severity = _text(vulnerability.get("severity")).lower()
            fix = vulnerability.get("fix") or {}
            fix = fix if isinstance(fix, Mapping) else {}
            versions = fix.get("versions") or []
            related = match.get("relatedVulnerabilities") or []
            item = Finding(
                ident,
                severity if severity in SEVERITIES else "unknown",
                _text(artifact.get("name")),
                _text(artifact.get("version")),
                tuple(
                    _text(version)
                    for version in (versions if isinstance(versions, list) else [])
                )[:16],
                _text(fix.get("state")) or "unknown",
                tuple(
                    _text(other.get("id"))
                    for other in (related if isinstance(related, list) else [])
                    if isinstance(other, Mapping) and other.get("id")
                )[:16],
            )
            found.setdefault((item.id, item.package, item.installed), item)
        descriptor = document.get("descriptor") or {}
        descriptor = descriptor if isinstance(descriptor, Mapping) else {}
        return cls(
            tuple(sorted(found.values(), key=_order)),
            _text(descriptor.get("version")) or None,
            _db_built(descriptor.get("db")),
        )


def _order(item: Finding) -> tuple[int, str, str, str]:
    return (SEVERITIES.index(item.severity), item.id, item.package, item.installed)


def _db_built(value: Any) -> str | None:
    """The database build time of a descriptor's ``db`` (or ``db status``)."""
    if not isinstance(value, Mapping):
        return None
    status = value.get("status")
    if isinstance(status, Mapping) and status.get("built"):
        return _text(status["built"])
    return _text(value["built"]) if value.get("built") else None


@dataclass(frozen=True)
class Verdict:
    """A report judged by a policy."""

    report: Report
    blocking: tuple[tuple[Finding, str | None], ...]  # (finding, expired until)
    waived: tuple[Finding, ...]

    @property
    def passed(self) -> bool:
        return not self.blocking

    def summary(self, pinned: bool = True) -> dict[str, Any]:
        """The receipt's entry for one scanned image."""
        return {
            "scanner": "grype",
            "version": self.report.version or GRYPE_VERSION,
            "pinned": pinned,
            "db_built": self.report.db_built,
            "fail_on": FAIL_ON,
            "critical_fixable": len(self.blocking),
            "counts": self.report.counts(),
            "waived": sorted({item.id for item in self.waived}),
        }

    def listed(self) -> list[dict[str, Any]]:
        """Critical and High findings, with whether a waiver covers them."""
        waived = {id(item) for item in self.waived}
        expired = {id(item): until for item, until in self.blocking}
        return [
            item.public(id(item) in waived, expired.get(id(item)))
            for item in self.report.findings
            if item.severity in {"critical", "high"}
        ][:MAX_LISTED]

    def failing(self) -> list[dict[str, Any]]:
        return [item.public(False, until) for item, until in self.blocking[:MAX_LISTED]]


def judge(report: Report, policy: ScanPolicy, today: _dt.date | None = None) -> Verdict:
    """Apply ``policy`` to ``report``."""
    today = today or _today()
    waivers = {item.id: item for item in policy.waivers}
    blocking: list[tuple[Finding, str | None]] = []
    waived: list[Finding] = []
    for item in report.findings:
        if not item.blocking:
            continue
        names = [waivers[name] for name in (item.id, *item.related) if name in waivers]
        live = [waiver for waiver in names if not waiver.expired(today)]
        if live:
            waived.append(item)
        else:
            blocking.append((item, names[0].until if names else None))
    return Verdict(report, tuple(blocking), tuple(waived))


def vulnerable(failures: Sequence[Mapping[str, Any]]) -> ScanError:
    """``image-vulnerable`` for ``failures`` (``{image, digest, platform,
    findings}`` each); the message names the first findings."""
    parts = []
    for failure in failures:
        names = []
        for item in failure["findings"][:5]:
            text = (
                f"{item['id']} ({item['package']} {item['installed']}, fixed in "
                f"{', '.join(item['fixed_in'])})"
            )
            if item.get("waiver_expired"):
                text += f", waiver expired {item['waiver_expired']}"
            names.append(text)
        more = len(failure["findings"]) - len(names)
        parts.append(
            f"{failure['image']} ({str(failure['digest'])[:19]}…): "
            f"{len(failure['findings'])} fixable critical vulnerabilit"
            f"{'y' if len(failure['findings']) == 1 else 'ies'}: "
            + "; ".join(names)
            + (f"; {more} more" if more > 0 else "")
        )
    return ScanError(
        "image-vulnerable",
        " | ".join(parts),
        {"fail_on": FAIL_ON, "images": [dict(item) for item in failures]},
    )


# ------------------------------------------------------------------ install


def default_cache_dir() -> Path:
    """``PICELI_SCAN_CACHE``, else ``$XDG_CACHE_HOME/piceli/scan``
    (``~/.cache/…`` without it): mirrors and copies; a host build uses its
    own cache directory."""
    if os.environ.get(CACHE_ENV):
        return Path(os.environ[CACHE_ENV])
    root = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(root) / "piceli" / "scan"


def install_grype(
    cache: Path,
    *,
    system: str | None = None,
    arch: str | None = None,
    opener: Callable[[str], Any] | None = None,
    checksums: Mapping[tuple[str, str], str] = GRYPE_SHA256,
    say: Callable[[str], None] = lambda _line: None,
) -> Path:
    """The pinned Grype, installed once into ``<cache>/toolchains``.

    :raises ScanError: ``image-scanner-checksum`` (a tarball that is not the
        pinned one: nothing is installed), ``image-scan-failed``.
    """
    from piceli.dev import toolchains

    if system is None or arch is None:
        system, arch = toolchains.host_platform()
    expected = checksums.get((system, arch))
    if expected is None:
        raise ScanError(
            "image-scan-failed",
            f"grype {GRYPE_VERSION} has no build for {system}/{arch}",
        )
    key = f"{GRYPE_VERSION}-{system}-{arch}"
    final = cache / "toolchains" / "grype" / key
    url = GRYPE_URL.format(version=GRYPE_VERSION, system=system, arch=arch)

    def install() -> dict[str, Any]:
        say(f"[piceli] scan: installing grype {GRYPE_VERSION}")
        scratch = final.with_name(f".{final.name}.{os.getpid()}")
        shutil.rmtree(scratch, ignore_errors=True)
        scratch.mkdir(parents=True)
        try:
            archive = scratch / "archive.tar.gz"
            _, got = toolchains._fetch(opener, url, archive)
            if got != expected:
                raise ScanError(
                    "image-scanner-checksum",
                    f"{url.rsplit('/', 1)[1]}: sha256 {got} is not the pinned {expected}",
                )
            tree = scratch / "tree"
            tree.mkdir()
            _extract_binary(archive, tree / "grype")
            shutil.rmtree(final, ignore_errors=True)
            os.replace(tree, final)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        return {"version": GRYPE_VERSION, "url": url, "sha256": expected}

    try:
        toolchains._once(cache, "grype", key, install, final)
    except toolchains.ToolchainError as error:
        raise ScanError("image-scan-failed", str(error)) from None
    return final / "grype"


def _extract_binary(archive: Path, into: Path) -> None:
    """Write the tarball's top-level ``grype`` file (only it) to ``into``."""
    try:
        with tarfile.open(archive) as tar:
            member = next(
                (
                    item
                    for item in tar.getmembers()
                    if item.name.lstrip("./") == "grype" and item.isfile()
                ),
                None,
            )
            if member is None or member.size > MAX_SCANNER_BYTES:
                raise ScanError("image-scan-failed", "the grype tarball has no grype")
            source = tar.extractfile(member)
            assert source is not None
            with source, into.open("wb") as sink:
                shutil.copyfileobj(source, sink)
    except (OSError, tarfile.TarError):
        raise ScanError("image-scan-failed", "cannot read the grype tarball") from None
    into.chmod(0o755)


# ------------------------------------------------------------------ run

#: ``(argv, environment, cwd, timeout) -> (exit code, stdout, stderr)``.
ScanRunner = Callable[
    [list[str], dict[str, str], Path, float], tuple[int, bytes, bytes]
]


def _default_runner(
    argv: list[str], environment: dict[str, str], cwd: Path, timeout: float
) -> tuple[int, bytes, bytes]:
    try:
        done = subprocess.run(
            argv,
            env=environment,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ScanError("image-scan-failed", "the scan timed out") from None
    except OSError:
        raise ScanError("image-scan-failed", "cannot start the scanner") from None
    return done.returncode, done.stdout, done.stderr


@dataclass
class Scanner:
    """Runs Grype with its database in ``<cache>/grype-db``.

    ``runner`` and ``environ`` are test seams; ``PICELI_SCANNER`` in
    ``environ`` names an executable used instead of the pinned Grype.
    """

    cache: Path
    runner: ScanRunner = _default_runner
    environ: Mapping[str, str] | None = None
    opener: Callable[[str], Any] | None = None
    say: Callable[[str], None] = lambda _line: None
    timeout: float = SCAN_SECONDS
    _executable: Path | None = field(default=None, init=False, repr=False)

    @property
    def pinned(self) -> bool:
        return not self._environ().get(SCANNER_ENV)

    def _environ(self) -> Mapping[str, str]:
        return self.environ if self.environ is not None else os.environ

    def executable(self) -> Path:
        if self._executable is None:
            override = self._environ().get(SCANNER_ENV)
            self._executable = (
                Path(override)
                if override
                else install_grype(self.cache, opener=self.opener, say=self.say)
            )
        return self._executable

    def scan(
        self,
        source: str,
        extra: Mapping[str, str] | None = None,
        secrets: Iterable[str] = (),
    ) -> Report:
        """Scan ``source`` (``oci-archive:<path>``, ``registry:<ref>``).

        :raises ScanError: ``image-scan-failed`` (no database, a scanner
            failure, an unreadable report).
        """
        hidden = [value for value in secrets if value]
        executable = str(self.executable())
        database = self.cache / "grype-db"
        database.mkdir(parents=True, exist_ok=True)
        lock = os.open(self.cache / ".grype-db.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)  # one database update at a time
            with tempfile.TemporaryDirectory(prefix="piceli-scan-") as home:
                environment = self._environment(Path(home), database, extra)
                code, out, err = self.runner(
                    [executable, source, "-o", "json"],
                    environment,
                    Path(home),
                    self.timeout,
                )
                if code != 0:
                    raise ScanError("image-scan-failed", _failure(err, hidden))
                if len(out) > MAX_REPORT_BYTES:
                    raise ScanError("image-scan-failed", "the scan report is too large")
                try:
                    report = Report.parse(json.loads(out))
                except ValueError as error:
                    if isinstance(error, ScanError):
                        raise
                    raise ScanError(
                        "image-scan-failed", "the scanner's report is not JSON"
                    ) from None
                if report.db_built is None:
                    report = Report(
                        report.findings,
                        report.version,
                        self._db_status(executable, environment, Path(home)),
                    )
        finally:
            os.close(lock)
        return report

    def _db_status(
        self, executable: str, environment: dict[str, str], cwd: Path
    ) -> str | None:
        code, out, _ = self.runner(
            [executable, "db", "status", "-o", "json"], environment, cwd, 120.0
        )
        if code != 0:
            return None
        try:
            return _db_built(json.loads(out))
        except ValueError:
            return None

    def _environment(
        self, home: Path, database: Path, extra: Mapping[str, str] | None
    ) -> dict[str, str]:
        """The caller's environment without its own Grype settings (they
        could turn the database check off), plus ours. ``HOME`` is a private
        directory, so no user config file changes the result."""
        environment = {
            key: value
            for key, value in self._environ().items()
            if not key.startswith("GRYPE_") and key != SCANNER_ENV
        }
        environment.update(
            {
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(home),
                "GRYPE_DB_CACHE_DIR": str(database),
                "GRYPE_DB_AUTO_UPDATE": "true",
                "GRYPE_DB_VALIDATE_AGE": "true",
                "GRYPE_CHECK_FOR_APP_UPDATE": "false",
            }
        )
        environment.update(extra or {})
        return environment


def _failure(stderr: bytes, secrets: Sequence[str]) -> str:
    """A short, redacted reason from the scanner's error output."""
    text = stderr.decode("utf-8", "replace")
    for secret in secrets:
        text = text.replace(secret, "***")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    last = _text(lines[-1]) if lines else "no output"
    if re.search(r"\b(db|database)\b", text.lower()):
        return (
            "the vulnerability database could not be fetched or is stale "
            f"(the scan needs HTTPS to the database server): {last}"
        )
    return f"the scanner failed: {last}"


# ------------------------------------------------------------------ builds


def scan_build(
    policy: ScanPolicy | None,
    outputs: dict[str, Any],
    output_dir: Path,
    cache: Path,
    say: Callable[[str], None] | None = None,
    *,
    scanner: Scanner | None = None,
    today: _dt.date | None = None,
) -> dict[str, Any]:
    """Scan each image archive of a host build; returns the receipt's fields.

    Writes ``attestations/<image>.scan.json`` and records it (path, sha256)
    in the image's entry, like the SBOM. Returns ``{"scans": {image:
    summary}}``, or ``{"scan": "off"}`` (also written into each provenance).

    :raises ScanError: ``image-vulnerable`` (after every image is scanned),
        ``image-scan-failed``, ``image-scanner-checksum``.
    """
    from piceli.artifacts import attestations

    say = say or (lambda _line: None)
    policy = policy or ScanPolicy()
    images: dict[str, Any] = outputs["images"]
    if not policy.enabled:
        say("[piceli] scan: off ([scan] enabled = false)")
        for entry in images.values():
            _provenance_off(output_dir, entry)
        return {"scan": "off"}
    scanner = scanner or Scanner(cache, say=say)
    scans: dict[str, Any] = {}
    failures: list[dict[str, Any]] = []
    for name, entry in images.items():
        archive = (output_dir / entry["archive"]).absolute()
        began = time.monotonic()
        verdict = judge(scanner.scan(f"oci-archive:{archive}"), policy, today)
        summary = verdict.summary(scanner.pinned)
        written = attestations.write(
            output_dir / "attestations" / f"{name}.scan.json",
            {
                "schema": REPORT_SCHEMA,
                "image": name,
                "subject": {"ref": entry.get("ref"), "digest": entry["digest"]},
                "platform": entry.get("platform"),
                **summary,
                "waivers": [item.to_dict() for item in policy.waivers],
                "findings": verdict.listed(),
            },
        )
        entry["scan"] = {
            "path": f"attestations/{written['path']}",
            "sha256": written["sha256"],
        }
        scans[name] = summary
        say(_line(name, summary, time.monotonic() - began))
        if not verdict.passed:
            failures.append(
                {
                    "image": name,
                    "digest": entry["digest"],
                    "platform": entry.get("platform"),
                    "findings": verdict.failing(),
                }
            )
    if failures:
        error = vulnerable(failures)
        say(f"[piceli] scan: image-vulnerable: {error}")
        raise error
    return {"scans": scans}


def _line(name: str, summary: Mapping[str, Any], seconds: float) -> str:
    counts = summary["counts"]
    shown = ", ".join(f"{counts[key]} {key}" for key in SEVERITIES if counts[key])
    waived = summary["waived"]
    return (
        f"[piceli] scan {name}: {summary['critical_fixable']} fixable critical"
        f" ({shown or 'no findings'})"
        + (f", waived {', '.join(waived)}" if waived else "")
        + f" in {seconds:.1f}s (db {summary['db_built'] or 'unknown'})"
    )


def _provenance_off(output_dir: Path, entry: dict[str, Any]) -> None:
    """Record ``"scan": "off"`` in an image's provenance (and its sha256)."""
    from piceli.artifacts import attestations

    found = entry.get("provenance")
    if not isinstance(found, Mapping):
        return
    path = output_dir / found["path"]
    statement = json.loads(path.read_text())
    statement["predicate"]["buildDefinition"]["externalParameters"]["scan"] = "off"
    written = attestations.write(path, statement)
    entry["provenance"] = {**found, "sha256": written["sha256"]}


def combined(receipts: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    """The scan fields of a multi-platform receipt: ``scans`` keyed like its
    images (``<image>/<os>-<arch>``), or ``"scan": "off"``."""
    if any(receipt.get("scan") == "off" for receipt in receipts.values()):
        return {"scan": "off"}
    scans = {
        f"{name}/{platform_slug(platform)}": summary
        for platform, receipt in receipts.items()
        for name, summary in (receipt.get("scans") or {}).items()
    }
    return {"scans": scans} if scans else {}


# ------------------------------------------------------------------ registries


def registry_source(
    endpoint: Any, repository: str, digest: str
) -> tuple[str, dict[str, str], tuple[str, ...]]:
    """Grype's source for ``repository@digest`` on ``endpoint`` (a
    :class:`~piceli.artifacts.registry.RegistryEndpoint`), the environment
    that reaches it (TLS, CA, credentials) and the secret values in it."""
    from piceli.artifacts.registry import RegistryCredentials, host_port

    authority = host_port(endpoint.host, endpoint.port)
    environment: dict[str, str] = {}
    if endpoint.use_tls is False:
        environment["GRYPE_REGISTRY_INSECURE_USE_HTTP"] = "true"
    if isinstance(endpoint.ca_file, Path):
        environment["GRYPE_REGISTRY_CA_CERT"] = str(endpoint.ca_file)
    secrets: list[str] = []
    credentials = endpoint.credentials
    token = endpoint.auth_token if isinstance(endpoint.auth_token, str) else None
    if isinstance(credentials, RegistryCredentials):
        if credentials.token is not None:
            token = credentials.token
        elif credentials.username is not None and credentials.password is not None:
            environment["GRYPE_REGISTRY_AUTH_USERNAME"] = credentials.username
            environment["GRYPE_REGISTRY_AUTH_PASSWORD"] = credentials.password
            secrets.append(credentials.password)
    if token is not None:
        environment["GRYPE_REGISTRY_AUTH_TOKEN"] = token
        secrets.append(token)
    if secrets:
        environment["GRYPE_REGISTRY_AUTH_AUTHORITY"] = authority
    return f"registry:{authority}/{repository}@{digest}", environment, tuple(secrets)


def platform_manifests(
    client: Any,
    repository: str,
    digest: str,
    only: Iterable[str] | None = None,
    present: Callable[[str], bool] | None = None,
) -> list[tuple[str, str]]:
    """``(platform, digest)`` of each image manifest of ``digest``: its
    children for an index (attestation entries, ``unknown/unknown``, are not
    images), else itself. ``only``: the children to keep; ``present``: keep
    a child only when it says so (a registry holding some platforms)."""
    from piceli.artifacts.image_manifest import INDEX_TYPES

    body, header = client.get_manifest(repository, digest)
    try:
        document = json.loads(body)
    except ValueError:
        raise ScanError("image-scan-failed", "the manifest is not JSON") from None
    media = (document.get("mediaType") if isinstance(document, dict) else None) or (
        header.split(";")[0].strip() if isinstance(header, str) else ""
    )
    if media not in INDEX_TYPES:
        return [("image", digest)]
    wanted = set(only) if only is not None else None
    found = []
    for entry in document.get("manifests") or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("digest"), str):
            continue
        described = entry.get("platform") or {}
        platform = "/".join(
            str(described[key])
            for key in ("os", "architecture", "variant")
            if isinstance(described, dict) and described.get(key)
        )
        if not platform or platform.startswith("unknown"):
            continue
        if wanted is not None and entry["digest"] not in wanted:
            continue
        if present is not None and not present(entry["digest"]):
            continue
        found.append((platform, entry["digest"]))
    return found


def scan_registry(
    policy: ScanPolicy | None,
    client: Any,
    repository: str,
    digest: str,
    *,
    image: str,
    cache: Path | None = None,
    only: Iterable[str] | None = None,
    present: Callable[[str], bool] | None = None,
    scanner: Scanner | None = None,
    say: Callable[[str], None] | None = None,
    today: _dt.date | None = None,
) -> dict[str, Any] | str:
    """Scan ``repository@digest`` where ``client`` (a
    :class:`~piceli.artifacts.registry.StreamedOciRegistryClient`) reaches
    it, every platform of an index; returns the receipt's ``scan`` (a summary
    with ``platforms``, or ``"off"``).

    :raises ScanError: ``image-vulnerable``, ``image-scan-failed``,
        ``image-scanner-checksum``.
    """
    say = say or (lambda _line: None)
    policy = policy or ScanPolicy()
    if not policy.enabled:
        say(f"[piceli] scan {image}: off")
        return "off"
    scanner = scanner or Scanner(cache or default_cache_dir(), say=say)
    endpoint = client.endpoint
    platforms: dict[str, Any] = {}
    failures: list[dict[str, Any]] = []
    total: dict[str, int] = dict.fromkeys(SEVERITIES, 0)
    waived: set[str] = set()
    first: dict[str, Any] | None = None
    for platform, child in platform_manifests(
        client, repository, digest, only, present
    ):
        source, environment, secrets = registry_source(endpoint, repository, child)
        began = time.monotonic()
        verdict = judge(scanner.scan(source, environment, secrets), policy, today)
        summary = verdict.summary(scanner.pinned)
        first = first or summary
        say(_line(f"{image} {platform}", summary, time.monotonic() - began))
        platforms[platform] = {
            "digest": child,
            "critical_fixable": summary["critical_fixable"],
            "counts": summary["counts"],
        }
        for key, count in summary["counts"].items():
            total[key] += count
        waived.update(summary["waived"])
        if not verdict.passed:
            failures.append(
                {
                    "image": image,
                    "digest": child,
                    "platform": platform,
                    "findings": verdict.failing(),
                }
            )
    if failures:
        raise vulnerable(failures)
    if first is None:
        raise ScanError("image-scan-failed", f"{image}: no platform image to scan")
    return {
        **first,
        "digest": digest,
        "critical_fixable": 0,
        "counts": total,
        "waived": sorted(waived),
        "platforms": platforms,
    }
