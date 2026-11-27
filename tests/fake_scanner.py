"""A stand-in for Grype (0.94.0): the test suite never downloads the scanner
or its vulnerability database.

:func:`write_scanner` writes an executable shell script that prints a fixed
Grype JSON report (``-o json``) and records each call's arguments in
``calls.log`` beside it. ``tests/conftest.py`` points ``PICELI_SCANNER`` at
one that reports no finding for the whole session; a test that wants
findings writes its own and sets ``PICELI_SCANNER`` with ``monkeypatch``.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

DB_BUILT = "2026-11-26T04:12:34Z"


def match(
    ident: str,
    severity: str,
    package: str = "openssl",
    installed: str = "3.0.1",
    fixed: tuple[str, ...] = (),
    state: str | None = None,
    related: tuple[str, ...] = (),
) -> dict[str, Any]:
    """One Grype match (the fields Piceli reads, and a few it does not)."""
    return {
        "vulnerability": {
            "id": ident,
            "dataSource": f"https://nvd.nist.gov/vuln/detail/{ident}",
            "namespace": "debian:distro:debian:12",
            "severity": severity,
            "urls": [],
            "fix": {
                "versions": list(fixed),
                "state": state or ("fixed" if fixed else "not-fixed"),
            },
        },
        "relatedVulnerabilities": [{"id": item} for item in related],
        "matchDetails": [{"type": "exact-indirect-match", "matcher": "dpkg-matcher"}],
        "artifact": {
            "id": "a1b2c3",
            "name": package,
            "version": installed,
            "type": "deb",
            "purl": f"pkg:deb/debian/{package}@{installed}",
        },
    }


def report(*matches: dict[str, Any], db_built: str | None = DB_BUILT) -> dict[str, Any]:
    """A Grype JSON report holding ``matches``."""
    descriptor: dict[str, Any] = {"name": "grype", "version": "0.122.1"}
    if db_built is not None:
        descriptor["db"] = {"status": {"built": db_built, "valid": True}}
    return {
        "matches": list(matches),
        "source": {"type": "image", "target": {"userInput": "example"}},
        "distro": {"name": "debian", "version": "12"},
        "descriptor": descriptor,
    }


def write_scanner(
    directory: Path,
    document: dict[str, Any] | None = None,
    *,
    exit_code: int = 0,
    stderr: str = "",
) -> Path:
    """An executable that answers like Grype with ``document`` (default: no
    finding); ``exit_code`` and ``stderr`` make it fail."""
    directory.mkdir(parents=True, exist_ok=True)
    body = directory / "report.json"
    body.write_text(json.dumps(document if document is not None else report()))
    status = directory / "status.json"
    status.write_text(json.dumps({"built": DB_BUILT, "valid": True}))
    log = directory / "calls.log"
    script = directory / "grype"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> {shlex.quote(str(log))}\n'
        f'if [ "$1" = "db" ]; then cat {shlex.quote(str(status))}; exit 0; fi\n'
        + (f"printf %s {shlex.quote(stderr)} >&2\n" if stderr else "")
        + (f"exit {exit_code}\n" if exit_code else f"cat {shlex.quote(str(body))}\n")
    )
    script.chmod(0o755)
    return script


def calls(scanner: Path) -> list[str]:
    """The argument lines of every call to the scanner at ``scanner``."""
    log = scanner.parent / "calls.log"
    return log.read_text().splitlines() if log.exists() else []
