"""Reading ``cargo test`` output: which tests failed, in which target.

``cargo test --no-fail-fast`` runs every test binary and, after each one
that failed, prints ``error: test failed, to rerun pass `-p NAME --lib```.
The ``test NAME ... FAILED`` lines before it are that target's failed tests,
and each ``---- NAME stdout ----`` section is a test's failure output.

Importing this module is side-effect free.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_RUNNING = re.compile(r"^\s+(Running|Doc-tests)\s")
_FAILED = re.compile(r"^test (.+?) \.\.\. FAILED\s*$")
_RERUN = re.compile(r"to rerun pass `([^`]+)`")
_SECTION = re.compile(r"^---- (.+?) stdout ----\s*$")

#: Lines of a test's failure output kept in results and the flaky registry.
TAIL_LINES = 40


@dataclass
class FailedTarget:
    """One test target that failed: its cargo arguments (``-p a --lib``)
    and the names of its failed tests (empty when it failed without naming
    one: a crash or a build error)."""

    target: str
    tests: list[str] = field(default_factory=list)


@dataclass
class CargoReport:
    """The failed targets of one ``cargo test`` run, and each failed test's
    output (by target and name)."""

    failed: list[FailedTarget] = field(default_factory=list)
    output: dict[tuple[str, str], str] = field(default_factory=dict)

    @property
    def retryable(self) -> bool:
        """Every failure names its tests, so they can run again by name."""
        return bool(self.failed) and all(item.tests for item in self.failed)


def parse(text: str) -> CargoReport:
    """The failures ``cargo test`` reported in ``text``."""
    report = CargoReport()
    pending: list[str] = []
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in text.splitlines():
        if _RUNNING.match(line):
            pending, sections, current = [], {}, None
            continue
        failed = _FAILED.match(line)
        if failed:
            pending.append(failed.group(1))
            current = None
            continue
        section = _SECTION.match(line)
        if section:
            current = sections.setdefault(section.group(1), [])
            continue
        rerun = _RERUN.search(line)
        if rerun:
            target = rerun.group(1)
            report.failed.append(FailedTarget(target, list(dict.fromkeys(pending))))
            for name in pending:
                body = "\n".join(sections.get(name, [])).strip("\n")
                report.output[(target, name)] = _tail(body)
            pending, sections, current = [], {}, None
            continue
        if line.strip() == "failures:" or line.startswith("test result:"):
            current = None
            continue
        if current is not None:
            current.append(line)
    return report


def _tail(text: str, lines: int = TAIL_LINES) -> str:
    return "\n".join(text.splitlines()[-lines:])


def split_args(args: tuple[str, ...]) -> tuple[list[str], list[str]]:
    """``(cargo arguments, test binary arguments)``: split at ``--``."""
    if "--" in args:
        at = args.index("--")
        return list(args[:at]), list(args[at + 1 :])
    return list(args), []


def retry_argv(
    target: FailedTarget, args: tuple[str, ...], names: list[str]
) -> list[str]:
    """``cargo test <target> <cargo args> -- <binary args> --exact NAMES``."""
    cargo, binary = split_args(args)
    return [
        "cargo",
        "test",
        *target.target.split(),
        *cargo,
        "--",
        *binary,
        "--exact",
        *names,
    ]
